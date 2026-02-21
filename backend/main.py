from fastapi import FastAPI, UploadFile, Form, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import dropbox
import os
import io
import re
import secrets
from datetime import datetime, timedelta
from collections import OrderedDict
import mimetypes

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://recipes.razzetti.org"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------
# ENV CONFIG
# --------------------------

ADMIN_USERNAME = os.environ["APP_USERNAME"]
ADMIN_PASSWORD = os.environ["APP_PASSWORD"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
DISABLE_AUTH = os.environ.get("DISABLE_AUTH", "false").lower() == "true"

SESSION_COOKIE = "recipes_session"
SESSION_DURATION_DAYS = 30

# --------------------------
# Session Store
# --------------------------

sessions = {}

def create_session():
    token = secrets.token_hex(32)
    expires = datetime.utcnow() + timedelta(days=SESSION_DURATION_DAYS)
    sessions[token] = expires
    return token

def verify_session(token: str):
    if token not in sessions:
        return False
    if sessions[token] < datetime.utcnow():
        del sessions[token]
        return False
    return True

def require_auth(request: Request):
    if DISABLE_AUTH:
        return
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not verify_session(token):
        raise HTTPException(status_code=401)

# --------------------------
# Dropbox Setup
# --------------------------

dbx = dropbox.Dropbox(
    oauth2_refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
    app_key=os.environ["DROPBOX_APP_KEY"],
    app_secret=os.environ["DROPBOX_APP_SECRET"],
)

RECIPES_ROOT = "/recipes"

# --------------------------
# Helpers
# --------------------------

def recipe_md_path(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}.md"

def recipe_folder(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}"

def normalize_tags(tag_string: str):
    if not tag_string:
        return []
    cleaned = tag_string.replace(",", " ")
    tags = [t.strip().lower() for t in cleaned.split() if t.strip()]
    return list(dict.fromkeys(tags))

def extract_tags(markdown: str):
    match = re.search(r"^Tags:\s*(.+)$", markdown, re.MULTILINE)
    if not match:
        return []
    return normalize_tags(match.group(1))

def replace_tags(markdown: str, new_tags: list[str]):
    markdown = re.sub(r"^Tags:.*\n+", "", markdown, flags=re.MULTILINE)
    if not new_tags:
        return markdown.lstrip()
    tag_line = f"Tags: {' '.join(new_tags)}\n\n"
    return tag_line + markdown.lstrip()

# --------------------------
# Cache Layer
# --------------------------

RECIPES_CACHE_TTL = timedelta(seconds=20)
RECIPE_MD_CACHE_TTL = timedelta(seconds=60)
PHOTO_CACHE_TTL = timedelta(minutes=15)
PHOTO_CACHE_MAX_ITEMS = 128

recipes_cache = {"value": None, "expires_at": datetime.min}
recipe_md_cache: dict[str, tuple[str, datetime]] = {}
photo_cache: OrderedDict[str, dict] = OrderedDict()

def cache_fresh(expires_at: datetime) -> bool:
    return datetime.utcnow() < expires_at

def get_cached_recipes():
    if recipes_cache["value"] is not None and cache_fresh(recipes_cache["expires_at"]):
        return recipes_cache["value"]
    return None

def set_cached_recipes(data):
    recipes_cache["value"] = data
    recipes_cache["expires_at"] = datetime.utcnow() + RECIPES_CACHE_TTL

def clear_recipes_cache():
    recipes_cache["value"] = None
    recipes_cache["expires_at"] = datetime.min

def get_cached_recipe_md(name: str):
    entry = recipe_md_cache.get(name)
    if not entry:
        return None
    markdown, expires_at = entry
    if cache_fresh(expires_at):
        return markdown
    recipe_md_cache.pop(name, None)
    return None

def set_cached_recipe_md(name: str, markdown: str):
    recipe_md_cache[name] = (markdown, datetime.utcnow() + RECIPE_MD_CACHE_TTL)

def clear_recipe_md_cache(name: str | None = None):
    if name is None:
        recipe_md_cache.clear()
        return
    recipe_md_cache.pop(name, None)

def get_photo_cache_headers(etag: str):
    return {
        "Cache-Control": "private, max-age=86400",
        "ETag": etag,
    }

def get_cached_photo(path: str):
    entry = photo_cache.get(path)
    if not entry:
        return None
    if not cache_fresh(entry["expires_at"]):
        photo_cache.pop(path, None)
        return None
    photo_cache.move_to_end(path)
    return entry

def set_cached_photo(path: str, content: bytes, media_type: str, etag: str):
    photo_cache[path] = {
        "content": content,
        "media_type": media_type,
        "etag": etag,
        "expires_at": datetime.utcnow() + PHOTO_CACHE_TTL,
    }
    photo_cache.move_to_end(path)
    while len(photo_cache) > PHOTO_CACHE_MAX_ITEMS:
        photo_cache.popitem(last=False)

def clear_photo_cache(recipe: str | None = None):
    if recipe is None:
        photo_cache.clear()
        return
    prefix = f"{RECIPES_ROOT}/{recipe}/"
    for key in [k for k in photo_cache if k.startswith(prefix)]:
        photo_cache.pop(key, None)

def invalidate_for_recipe_change(name: str | None = None):
    clear_recipes_cache()
    clear_photo_cache(name)
    clear_recipe_md_cache(name)

# --------------------------
# AUTH ROUTES
# --------------------------

@app.post("/api/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401)

    token = create_session()

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_DURATION_DAYS * 24 * 60 * 60,
    )

    return {"status": "ok"}

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged out"}

@app.get("/api/auth-check")
def auth_check(request: Request):
    if DISABLE_AUTH:
        return {"authenticated": True}
    token = request.cookies.get(SESSION_COOKIE)
    if token and verify_session(token):
        return {"authenticated": True}
    return {"authenticated": False}

# --------------------------
# PROTECTED ROUTES
# --------------------------

@app.get("/api/recipes")
def list_recipes(request: Request):
    require_auth(request)

    cached = get_cached_recipes()
    if cached is not None:
        return cached

    result = dbx.files_list_folder(RECIPES_ROOT)
    recipes = []

    for entry in result.entries:
        if entry.name.endswith(".md"):
            name = entry.name[:-3]
            _, res = dbx.files_download(recipe_md_path(name))
            md = res.content.decode("utf-8")

            cover = None
            try:
                folder = dbx.files_list_folder(recipe_folder(name))
                for f in folder.entries:
                    if f.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        cover = f.name
                        break
            except:
                pass

            recipes.append({
                "name": name,
                "tags": extract_tags(md),
                "cover": cover
            })

    set_cached_recipes(recipes)
    return recipes

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str, request: Request):
    require_auth(request)
    cached = get_cached_recipe_md(name)
    if cached is not None:
        return cached
    _, res = dbx.files_download(recipe_md_path(name))
    markdown = res.content.decode("utf-8")
    set_cached_recipe_md(name, markdown)
    return markdown

@app.post("/api/recipes")
async def save_recipe(
    request: Request,
    name: str = Form(...),
    markdown: str = Form(...),
    tags: str = Form(""),
    original_name: str = Form(""),
    photo: UploadFile | None = None,
):
    require_auth(request)

    old_name = original_name.strip()
    is_rename = bool(old_name and old_name != name)

    if is_rename:
        try:
            dbx.files_move_v2(recipe_folder(old_name), recipe_folder(name))
        except dropbox.exceptions.ApiError:
            pass

    tag_list = normalize_tags(tags)
    final_md = replace_tags(markdown, tag_list)

    dbx.files_upload(
        final_md.encode("utf-8"),
        recipe_md_path(name),
        mode=dropbox.files.WriteMode.overwrite,
    )

    if photo:
        try:
            dbx.files_create_folder_v2(recipe_folder(name))
        except:
            pass

        content = await photo.read()
        dbx.files_upload(
            content,
            f"{recipe_folder(name)}/{photo.filename}",
            mode=dropbox.files.WriteMode.overwrite,
        )

    if is_rename:
        try:
            dbx.files_delete_v2(recipe_md_path(old_name))
        except dropbox.exceptions.ApiError:
            pass
        invalidate_for_recipe_change()
    else:
        invalidate_for_recipe_change(name)
    set_cached_recipe_md(name, final_md)
    return {"status": "ok"}

@app.delete("/api/recipes/{name}")
def delete_recipe(name: str, request: Request):
    require_auth(request)
    dbx.files_delete_v2(recipe_md_path(name))
    try:
        dbx.files_delete_v2(recipe_folder(name))
    except:
        pass
    invalidate_for_recipe_change(name)
    return {"status": "deleted"}

@app.delete("/api/recipes/{name}/photo/{filename}")
def delete_recipe_photo(name: str, filename: str, request: Request):
    require_auth(request)
    path = f"{recipe_folder(name)}/{filename}"
    try:
        dbx.files_delete_v2(path)
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404, detail="Photo not found")
    invalidate_for_recipe_change(name)
    return {"status": "photo deleted"}

# --------------------------
# DELETE TAG (RESTORED)
# --------------------------

@app.delete("/api/tags/{tag}")
def delete_tag(tag: str, request: Request):
    require_auth(request)

    result = dbx.files_list_folder(RECIPES_ROOT)

    for entry in result.entries:
        if entry.name.endswith(".md"):
            name = entry.name[:-3]
            _, res = dbx.files_download(recipe_md_path(name))
            md = res.content.decode("utf-8")

            tags = extract_tags(md)

            if tag in tags:
                tags.remove(tag)
                updated_md = replace_tags(md, tags)

                dbx.files_upload(
                    updated_md.encode("utf-8"),
                    recipe_md_path(name),
                    mode=dropbox.files.WriteMode.overwrite,
                )
                clear_recipe_md_cache(name)

    clear_recipes_cache()
    return {"status": "tag deleted"}

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str, request: Request):
    require_auth(request)
    path = f"{RECIPES_ROOT}/{recipe}/{filename}"
    if_none_match = request.headers.get("if-none-match")

    cached = get_cached_photo(path)
    if cached is not None:
        headers = get_photo_cache_headers(cached["etag"])
        if if_none_match and if_none_match == cached["etag"]:
            return Response(status_code=304, headers=headers)
        return StreamingResponse(
            io.BytesIO(cached["content"]),
            media_type=cached["media_type"],
            headers=headers,
        )

    metadata = dbx.files_get_metadata(path)
    etag = f"\"{getattr(metadata, 'rev', '')}\""
    headers = get_photo_cache_headers(etag)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers=headers)

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    _, res = dbx.files_download(path)
    content = res.content
    set_cached_photo(path, content, media_type, etag)

    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers=headers,
    )
