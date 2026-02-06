from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import dropbox
import os
import io

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


# ---------- Helpers ----------

def recipe_md_path(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}.md"


def recipe_folder(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}"


# ---------- List recipes ----------

@app.get("/api/recipes")
def list_recipes():
    try:
        result = dbx.files_list_folder(RECIPES_ROOT)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    names = []

    for entry in result.entries:
        if entry.name.endswith(".md"):
            names.append(entry.name[:-3])  # strip .md

    return sorted(names)


# ---------- Get recipe markdown ----------

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str):
    path = recipe_md_path(name)

    try:
        _, res = dbx.files_download(path)
        return res.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404, detail="Recipe not found")


# ---------- Save recipe + optional photo ----------

@app.post("/api/recipes")
async def save_recipe(
    name: str = Form(...),
    markdown: str = Form(...),
    photo: UploadFile | None = None,
):
    md_path = recipe_md_path(name)
    folder_path = recipe_folder(name)

    # Save markdown
    dbx.files_upload(
        markdown.encode("utf-8"),
        md_path,
        mode=dropbox.files.WriteMode.overwrite,
    )

    # Save photo if present
    if photo:
        try:
            dbx.files_create_folder_v2(folder_path)
        except Exception:
            pass  # folder may already exist

        # Remove existing images
        try:
            result = dbx.files_list_folder(folder_path)
            for entry in result.entries:
                if entry.name.lower().endswith(IMAGE_EXTS):
                    dbx.files_delete_v2(entry.path_lower)
        except Exception:
            pass

        img_path = f"{folder_path}/{photo.filename}"
        content = await photo.read()

        dbx.files_upload(
            content,
            img_path,
            mode=dropbox.files.WriteMode.overwrite,
        )

        # Append image reference to markdown if missing
        _, res = dbx.files_download(md_path)
        md_text = res.content.decode("utf-8")

        image_line = f"\n\n![{photo.filename}]({photo.filename})\n"
        if image_line.strip() not in md_text:
            md_text += image_line

            dbx.files_upload(
                md_text.encode("utf-8"),
                md_path,
                mode=dropbox.files.WriteMode.overwrite,
            )

    return {"status": "ok"}


# ---------- Serve recipe photos ----------

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


# ---------- Recipe cover image (for tiles) ----------

@app.get("/api/recipes/{name}/cover")
def get_recipe_cover(name: str):
    folder = recipe_folder(name)

    try:
        result = dbx.files_list_folder(folder)
    except Exception:
        return {"url": None}

    for entry in result.entries:
        if entry.name.lower().endswith(IMAGE_EXTS):
            return {
                "url": f"/api/photos/{name}/{entry.name}"
            }

    return {"url": None}
