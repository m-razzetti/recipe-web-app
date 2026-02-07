from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import dropbox
import os
import io
import re
import time
from typing import Dict, List

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

dbx = dropbox.Dropbox(
    oauth2_refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
    app_key=os.environ["DROPBOX_APP_KEY"],
    app_secret=os.environ["DROPBOX_APP_SECRET"],
)

RECIPES_ROOT = "/recipes"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# ---------- In-memory cache ----------
# name -> { tags: [...], cover: "file.jpg" | None }
RECIPE_CACHE: Dict[str, Dict] = {}


# ---------- Helpers ----------

def recipe_md_path(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}.md"


def recipe_folder(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}"


def extract_tags(markdown: str) -> List[str]:
    match = re.search(r"^Tags:\s*(.+)$", markdown, re.MULTILINE)
    if not match:
        return []
    return [t.strip() for t in match.group(1).split(",") if t.strip()]


def strip_existing_tags(markdown: str) -> str:
    return re.sub(r"^Tags:.*\n+", "", markdown, flags=re.MULTILINE)


def find_cover_image(name: str) -> str | None:
    try:
        result = dbx.files_list_folder(recipe_folder(name))
    except Exception:
        return None

    for entry in result.entries:
        if entry.name.lower().endswith(IMAGE_EXTS):
            return entry.name

    return None


# ---------- Cache management ----------

def build_recipe_cache():
    global RECIPE_CACHE
    RECIPE_CACHE.clear()

    start = time.perf_counter()

    result = dbx.files_list_folder(RECIPES_ROOT)

    for entry in result.entries:
        if not entry.name.endswith(".md"):
            continue

        name = entry.name[:-3]

        try:
            _, res = dbx.files_download(recipe_md_path(name))
            md = res.content.decode("utf-8")
        except Exception:
            continue

        RECIPE_CACHE[name] = {
            "tags": extract_tags(md),
            "cover": find_cover_image(name),
        }

    elapsed = (time.perf_counter() - start) * 1000
    print(f"[cache] Loaded {len(RECIPE_CACHE)} recipes in {elapsed:.2f} ms")


@app.on_event("startup")
def startup_event():
    build_recipe_cache()


# ---------- API ----------

@app.get("/api/recipes")
def list_recipes():
    start = time.perf_counter()

    data = [
        {
            "name": name,
            "tags": meta["tags"],
            "cover": meta["cover"],
        }
        for name, meta in RECIPE_CACHE.items()
    ]

    elapsed = (time.perf_counter() - start) * 1000
    print(f"[perf] /api/recipes served in {elapsed:.2f} ms")

    return data


@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str):
    try:
        _, res = dbx.files_download(recipe_md_path(name))
        return res.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404, detail="Recipe not found")


@app.post("/api/recipes")
async def save_recipe(
    name: str = Form(...),
    markdown: str = Form(...),
    tags: str = Form(""),
    photo: UploadFile | None = None,
):
    md_path = recipe_md_path(name)
    folder_path = recipe_folder(name)

    clean_md = strip_existing_tags(markdown)
    tag_line = f"Tags: {tags.strip()}\n\n" if tags.strip() else ""
    final_md = tag_line + clean_md.lstrip()

    # Save markdown
    dbx.files_upload(
        final_md.encode("utf-8"),
        md_path,
        mode=dropbox.files.WriteMode.overwrite,
    )

    # Save photo if provided
    if photo:
        try:
            dbx.files_create_folder_v2(folder_path)
        except Exception:
            pass

        img_path = f"{folder_path}/{photo.filename}"
        content = await photo.read()

        dbx.files_upload(
            content,
            img_path,
            mode=dropbox.files.WriteMode.overwrite,
        )

    # Update cache for this recipe only
    RECIPE_CACHE[name] = {
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "cover": find_cover_image(name),
    }

    return {"status": "ok"}


@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str):
    path = f"{RECIPES_ROOT}/{recipe}/{filename}"
    try:
        _, res = dbx.files_download(path)
        return StreamingResponse(
            io.BytesIO(res.content),
            media_type="image/jpeg",
        )
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404)
