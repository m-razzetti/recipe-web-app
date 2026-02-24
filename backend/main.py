from fastapi import FastAPI, UploadFile, Form, HTTPException, Request, Response, Body, File
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import dropbox
import os
import io
import re
import secrets
import json
from datetime import datetime, timedelta
from collections import OrderedDict
import mimetypes
from pypdf import PdfReader
from PIL import Image
from pillow_heif import register_heif_opener

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://recipes.razzetti.org"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=100)

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
RECIPES_INDEX_PATH = f"{RECIPES_ROOT}/.index.json"
register_heif_opener()
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")

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

def filename_to_title(filename: str) -> str:
    base = re.sub(r"\.[A-Za-z0-9]+$", "", filename or "").strip()
    base = re.sub(r"[_\-]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base

def normalize_plain_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def text_to_markdown(raw_text: str, fallback_title: str = "") -> str:
    text = normalize_plain_text(raw_text)
    if not text:
        return ""

    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line != ""]
    if not lines:
        return ""

    section_titles = {
        "ingredients": "Ingredients",
        "instructions": "Instructions",
        "directions": "Instructions",
        "steps": "Steps",
        "step": "Steps",
        "method": "Instructions",
        "notes": "Notes",
        "yield": "Yield",
        "servings": "Servings",
        "prep time": "Prep Time",
        "cook time": "Cook Time",
        "total time": "Total Time",
    }
    section_names = set(section_titles.keys())
    bullet_like = re.compile(r"^([\-*•]\s+|\d+[\.\)]\s+)")
    trailing_colon_header = re.compile(r"^[A-Za-z][A-Za-z0-9 /&'\-]{1,40}:$")

    def clean_key(value: str) -> str:
        return value.lower().rstrip(":").strip()

    def normalize_title(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def looks_like_title(value: str) -> bool:
        v = value.strip()
        if not v:
            return False
        if bullet_like.match(v):
            return False
        if clean_key(v) in section_names:
            return False
        if trailing_colon_header.match(v):
            return False
        if len(v) > 90:
            return False
        if len(v.split()) > 16:
            return False
        return True

    md_lines = []

    title_idx = next((i for i, line in enumerate(lines[:3]) if looks_like_title(line)), 0)
    title = lines[title_idx] if looks_like_title(lines[title_idx]) else (fallback_title.strip() or lines[0])
    normalized_title = normalize_title(title)

    if title:
        md_lines.append(f"# {title}")
        md_lines.append("")

    current_section = ""
    for line in lines:
        if normalize_title(line) == normalized_title:
            continue

        lower = clean_key(line)
        if lower in section_names:
            md_lines.append(f"## {section_titles[lower]}")
            md_lines.append("")
            current_section = section_titles[lower].lower()
            continue

        if trailing_colon_header.match(line):
            key = clean_key(line)
            if key in section_names:
                md_lines.append(f"## {section_titles[key]}")
                md_lines.append("")
                current_section = section_titles[key].lower()
            else:
                md_lines.append(f"### {line.rstrip(':')}")
                md_lines.append("")
            continue

        if ":" in line:
            key, value = [p.strip() for p in line.split(":", 1)]
            key_l = key.lower()
            if key_l in {"yield", "servings", "prep time", "cook time", "total time"} and value:
                md_lines.append(f"**{section_titles.get(key_l, key.title())}:** {value}")
                continue

        if bullet_like.match(line):
            normalized = re.sub(r"^([\-*•]\s+|\d+[\.\)]\s+)", "- ", line).strip()
            md_lines.append(normalized)
        else:
            # In common list-heavy sections, plain lines are usually list items.
            if current_section in {"ingredients", "instructions", "steps", "notes"}:
                md_lines.append(f"- {line}")
            else:
                md_lines.append(line)

    markdown = "\n".join(md_lines).strip()
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown + "\n"

def extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    parts = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            parts.append(page_text)
    return "\n\n".join(parts).strip()

def is_image_file(name: str) -> bool:
    return name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"))

def normalize_photo_upload(filename: str, content_type: str | None, content: bytes) -> tuple[str, bytes]:
    lower = (filename or "").lower()
    heic_like = lower.endswith((".heic", ".heif")) or (content_type or "").lower() in {"image/heic", "image/heif"}
    if not heic_like:
        return filename, content

    try:
        with Image.open(io.BytesIO(content)) as img:
            # JPEG output guarantees broad browser compatibility.
            rgb = img.convert("RGB")
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=90, optimize=True)
            stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename or "photo").strip() or "photo"
            return f"{stem}.jpg", out.getvalue()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"HEIC conversion failed: {exc}")

def normalize_recipe_item(item: dict) -> dict:
    name = str(item.get("name", "")).strip()
    if not name:
        return {}
    tags = item.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    cover = item.get("cover")
    if cover is not None:
        cover = str(cover).strip() or None
    return {
        "name": name,
        "tags": normalize_tags(" ".join(str(t) for t in tags)),
        "cover": cover,
    }

def sort_recipes(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda r: r["name"].lower())

def load_recipe_index() -> list[dict] | None:
    try:
        _, res = dbx.files_download(RECIPES_INDEX_PATH)
    except dropbox.exceptions.ApiError:
        return None

    try:
        raw = json.loads(res.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None

    if not isinstance(raw, list):
        return None

    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = normalize_recipe_item(item)
        if entry:
            normalized.append(entry)
    return sort_recipes(normalized)

def save_recipe_index(items: list[dict]):
    payload = json.dumps(sort_recipes(items), separators=(",", ":")).encode("utf-8")
    dbx.files_upload(
        payload,
        RECIPES_INDEX_PATH,
        mode=dropbox.files.WriteMode.overwrite,
    )

def list_recipe_root_entries():
    result = dbx.files_list_folder(RECIPES_ROOT)
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries

def build_recipe_index_from_dropbox() -> list[dict]:
    entries = list_recipe_root_entries()
    folder_names = {e.name for e in entries if isinstance(e, dropbox.files.FolderMetadata)}
    recipes = []

    for entry in entries:
        if not entry.name.endswith(".md"):
            continue

        name = entry.name[:-3]
        _, res = dbx.files_download(recipe_md_path(name))
        md = res.content.decode("utf-8")

        cover = None
        if name in folder_names:
            try:
                folder = dbx.files_list_folder(recipe_folder(name))
                for f in folder.entries:
                    if is_image_file(f.name):
                        cover = f.name
                        break
            except dropbox.exceptions.ApiError:
                pass

        recipes.append({
            "name": name,
            "tags": extract_tags(md),
            "cover": cover,
        })

    return sort_recipes(recipes)

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
def list_recipes(
    request: Request,
    response: Response,
    offset: int = 0,
    limit: int = 60,
    q: str = "",
):
    require_auth(request)

    recipes = get_cached_recipes()
    if recipes is None:
        recipes = load_recipe_index()
        if recipes is None:
            recipes = build_recipe_index_from_dropbox()
            save_recipe_index(recipes)
        set_cached_recipes(recipes)

    if q.strip():
        needle = q.strip().lower()
        filtered = [
            r for r in recipes
            if needle in r["name"].lower() or any(needle in t for t in r["tags"])
        ]
    else:
        filtered = recipes

    safe_offset = max(0, offset)
    safe_limit = min(max(1, limit), 200)
    page = filtered[safe_offset:safe_offset + safe_limit]

    next_offset = safe_offset + len(page)
    response.headers["X-Total-Count"] = str(len(filtered))
    response.headers["X-Next-Offset"] = str(next_offset) if next_offset < len(filtered) else ""
    return page

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

@app.post("/api/import/convert")
async def convert_import_file(
    request: Request,
    source_file: UploadFile = File(...),
):
    require_auth(request)

    filename = source_file.filename or "Imported Recipe"
    lower_name = filename.lower()
    content = await source_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    try:
        if lower_name.endswith(".pdf") or source_file.content_type == "application/pdf":
            raw_text = extract_pdf_text(content)
            if not raw_text:
                raise HTTPException(status_code=400, detail="Could not extract text from PDF")
        elif lower_name.endswith(".txt") or lower_name.endswith(".md") or source_file.content_type in {"text/plain", "text/markdown"}:
            raw_text = content.decode("utf-8", errors="replace")
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type. Use .txt, .md, or .pdf")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Import conversion failed: {exc}")

    suggested_name = filename_to_title(filename) or "Imported Recipe"
    markdown = text_to_markdown(raw_text, fallback_title=suggested_name)
    if not markdown.strip():
        raise HTTPException(status_code=400, detail="No usable content found")

    return {"name": suggested_name, "markdown": markdown}

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
    index_items = load_recipe_index() or []
    old_entry = next((r for r in index_items if r["name"] == (old_name if is_rename else name)), None)

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

    saved_cover = old_entry["cover"] if old_entry else None
    if photo:
        try:
            dbx.files_create_folder_v2(recipe_folder(name))
        except:
            pass

        content = await photo.read()
        upload_name, upload_content = normalize_photo_upload(
            photo.filename or "photo",
            photo.content_type,
            content,
        )
        dbx.files_upload(
            upload_content,
            f"{recipe_folder(name)}/{upload_name}",
            mode=dropbox.files.WriteMode.overwrite,
        )
        saved_cover = upload_name

    if is_rename:
        try:
            dbx.files_delete_v2(recipe_md_path(old_name))
        except dropbox.exceptions.ApiError:
            pass
        invalidate_for_recipe_change()
    else:
        invalidate_for_recipe_change(name)
    set_cached_recipe_md(name, final_md)

    if is_rename:
        index_items = [r for r in index_items if r["name"] != old_name]
    index_items = [r for r in index_items if r["name"] != name]
    index_items.append({
        "name": name,
        "tags": tag_list,
        "cover": saved_cover,
    })
    save_recipe_index(index_items)
    set_cached_recipes(sort_recipes(index_items))
    return {"status": "ok"}

@app.post("/api/recipes/bulk-tags")
def bulk_add_tags(
    request: Request,
    payload: dict = Body(...),
):
    require_auth(request)

    names_raw = payload.get("names", [])
    tags_raw = payload.get("tags", "")

    if not isinstance(names_raw, list):
        raise HTTPException(status_code=400, detail="names must be an array")

    names = [str(n).strip() for n in names_raw if str(n).strip()]
    names = list(dict.fromkeys(names))
    add_tags = normalize_tags(str(tags_raw))

    if not names:
        raise HTTPException(status_code=400, detail="No recipes selected")
    if not add_tags:
        raise HTTPException(status_code=400, detail="No tags provided")

    index_items = load_recipe_index() or []
    index_map = {r["name"]: r for r in index_items}

    updated = 0
    for name in names:
        try:
            _, res = dbx.files_download(recipe_md_path(name))
        except dropbox.exceptions.ApiError:
            continue

        md = res.content.decode("utf-8")
        current_tags = extract_tags(md)
        merged_tags = normalize_tags(" ".join(current_tags + add_tags))
        if merged_tags == current_tags:
            continue

        updated_md = replace_tags(md, merged_tags)
        dbx.files_upload(
            updated_md.encode("utf-8"),
            recipe_md_path(name),
            mode=dropbox.files.WriteMode.overwrite,
        )
        clear_recipe_md_cache(name)

        if name in index_map:
            index_map[name]["tags"] = merged_tags
        updated += 1

    if updated:
        updated_index = sort_recipes(list(index_map.values()))
        save_recipe_index(updated_index)
        set_cached_recipes(updated_index)
    else:
        clear_recipes_cache()

    return {"status": "ok", "updated": updated}

@app.post("/api/recipes/bulk-delete")
def bulk_delete_recipes(
    request: Request,
    payload: dict = Body(...),
):
    require_auth(request)

    names_raw = payload.get("names", [])
    if not isinstance(names_raw, list):
        raise HTTPException(status_code=400, detail="names must be an array")

    names = [str(n).strip() for n in names_raw if str(n).strip()]
    names = list(dict.fromkeys(names))
    if not names:
        raise HTTPException(status_code=400, detail="No recipes selected")
    selected = set(names)

    deleted = 0
    for name in names:
        try:
            dbx.files_delete_v2(recipe_md_path(name))
        except dropbox.exceptions.ApiError:
            continue
        try:
            dbx.files_delete_v2(recipe_folder(name))
        except dropbox.exceptions.ApiError:
            pass
        deleted += 1

    invalidate_for_recipe_change()
    index_items = load_recipe_index()
    if index_items is not None:
        index_items = [r for r in index_items if r["name"] not in selected]
        save_recipe_index(index_items)
        set_cached_recipes(sort_recipes(index_items))

    return {"status": "ok", "deleted": deleted}

@app.delete("/api/recipes/{name}")
def delete_recipe(name: str, request: Request):
    require_auth(request)
    dbx.files_delete_v2(recipe_md_path(name))
    try:
        dbx.files_delete_v2(recipe_folder(name))
    except:
        pass
    invalidate_for_recipe_change(name)
    index_items = load_recipe_index()
    if index_items is not None:
        index_items = [r for r in index_items if r["name"] != name]
        save_recipe_index(index_items)
        set_cached_recipes(sort_recipes(index_items))
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
    index_items = load_recipe_index()
    if index_items is not None:
        changed = False
        for item in index_items:
            if item["name"] == name and item.get("cover") == filename:
                item["cover"] = None
                changed = True
                break
        if changed:
            save_recipe_index(index_items)
            set_cached_recipes(sort_recipes(index_items))
    return {"status": "photo deleted"}

# --------------------------
# DELETE TAG (RESTORED)
# --------------------------

@app.delete("/api/tags/{tag}")
def delete_tag(tag: str, request: Request):
    require_auth(request)

    result = list_recipe_root_entries()
    index_items = load_recipe_index()
    index_by_name = {r["name"]: r for r in index_items} if index_items is not None else None

    for entry in result:
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
                if index_by_name is not None and name in index_by_name:
                    index_by_name[name]["tags"] = tags

    clear_recipes_cache()
    if index_by_name is not None:
        updated_index = sort_recipes(list(index_by_name.values()))
        save_recipe_index(updated_index)
        set_cached_recipes(updated_index)
    return {"status": "tag deleted"}

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str, request: Request):
    require_auth(request)
    path = f"{RECIPES_ROOT}/{recipe}/{filename}"
    use_thumb = request.query_params.get("thumb") == "1"
    cache_key = f"{path}?thumb=1" if use_thumb else path
    if_none_match = request.headers.get("if-none-match")

    cached = get_cached_photo(cache_key)
    if cached is not None:
        headers = get_photo_cache_headers(cached["etag"])
        if if_none_match and if_none_match == cached["etag"]:
            return Response(status_code=304, headers=headers)
        return StreamingResponse(
            io.BytesIO(cached["content"]),
            media_type=cached["media_type"],
            headers=headers,
        )

    if use_thumb:
        # Smaller card images reduce transfer and render time substantially.
        try:
            meta, res = dbx.files_get_thumbnail(
                path,
                format=dropbox.files.ThumbnailFormat.jpeg,
                size=dropbox.files.ThumbnailSize.w256h256,
            )
            content = res.content
            media_type = "image/jpeg"
        except Exception:
            meta, res = dbx.files_download(path)
            content = res.content
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    else:
        meta, res = dbx.files_download(path)
        content = res.content
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    rev = getattr(meta, "rev", "")
    etag_suffix = "-thumb" if use_thumb else ""
    etag = f"\"{rev}{etag_suffix}\""
    headers = get_photo_cache_headers(etag)
    set_cached_photo(cache_key, content, media_type, etag)

    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers=headers,
    )
