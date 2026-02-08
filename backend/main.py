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

# name -> { tags, cover }
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


# ---------- Cache ----------

def build_recipe_cache():
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

    print(f"[cache] Loaded {len(RECIPE_CACHE)} recipes in {(time.perf_counter()-start)*1000:.2f} ms")


@app.on_event("startup")
def startup_event():
    build_recipe_cache()


# ---------- API ----------

@app.get("/api/recipes")
def list_recipes():
    return [
        {"name": name, "tags": data["tags"], "cover": data["cover"]}
        for name, data in RECIPE_CACHE.items()
    ]


@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str):
    try:
        _, res = dbx.files_download(recipe_md_path(name))
        return res.content.decode("utf-8")
    except Exception:
        raise HTTPException(status_code=404)


@app.post("/api/recipes")
async def save_recipe(
    name: str = Form(...),
    markdown: str = Form(...),
    tags: str = Form(""),
    photo: UploadFile | None = None,
):
    md = strip_existing_tags(markdown)
    final_md = f"Tags: {tags.strip()}\n\n{md.lstrip()}" if tags.strip() else md

    dbx.files_upload(
        final_md.encode(),
        recipe_md_path(name),
        mode=dropbox.files.WriteMode.overwrite,
    )

    if photo:
        try:
            dbx.files_create_folder_v2(recipe_folder(name))
        except Exception:
            pass

        dbx.files_upload(
            await photo.read(),
            f"{recipe_folder(name)}/{photo.filename}",
            mode=dropbox.files.WriteMode.overwrite,
        )

    RECIPE_CACHE[name] = {
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "cover": find_cover_image(name),
    }

    return {"status": "ok"}


# ---------- DELETE RECIPE ----------

@app.delete("/api/recipes/{name}")
def delete_recipe(name: str):
    if name not in RECIPE_CACHE:
        raise HTTPException(status_code=404)

    # delete markdown
    try:
        dbx.files_delete_v2(recipe_md_path(name))
    except Exception:
        pass

    # delete folder (photos)
    try:
        dbx.files_delete_v2(recipe_folder(name))
    except Exception:
        pass

    RECIPE_CACHE.pop(name, None)
    return {"status": "deleted"}


# ---------- Photos ----------

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str):
    try:
        _, res = dbx.files_download(f"{RECIPES_ROOT}/{recipe}/{filename}")
        return StreamingResponse(io.BytesIO(res.content), media_type="image/jpeg")
    except Exception:
        raise HTTPException(status_code=404)
