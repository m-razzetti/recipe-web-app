from fastapi import FastAPI, UploadFile, Form, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import dropbox
import os
import io
import re
import secrets
from datetime import datetime, timedelta

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

    return recipes

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str, request: Request):
    require_auth(request)
    _, res = dbx.files_download(recipe_md_path(name))
    return res.content.decode("utf-8")

@app.post("/api/recipes")
async def save_recipe(
    request: Request,
    name: str = Form(...),
    markdown: str = Form(...),
    tags: str = Form(""),
    photo: UploadFile | None = None,
):
    require_auth(request)

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

    return {"status": "ok"}

@app.delete("/api/recipes/{name}")
def delete_recipe(name: str, request: Request):
    require_auth(request)
    dbx.files_delete_v2(recipe_md_path(name))
    try:
        dbx.files_delete_v2(recipe_folder(name))
    except:
        pass
    return {"status": "deleted"}

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

    return {"status": "tag deleted"}

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str, request: Request):
    require_auth(request)
    path = f"{RECIPES_ROOT}/{recipe}/{filename}"
    _, res = dbx.files_download(path)
    return StreamingResponse(io.BytesIO(res.content), media_type="image/jpeg")
