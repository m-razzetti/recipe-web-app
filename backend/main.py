from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import dropbox
import dropbox.files
import os
import io
import re

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


# ---------- Helpers ----------

def recipe_md_path(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}.md"


def recipe_folder(name: str) -> str:
    return f"{RECIPES_ROOT}/{name}"


def strip_image_lines(markdown: str) -> str:
    return re.sub(r"\n!\[.*?\]\(.*?\)\n?", "\n", markdown).strip() + "\n"


# ---------- List recipes ----------

@app.get("/api/recipes")
def list_recipes():
    result = dbx.files_list_folder(RECIPES_ROOT)
    return sorted(
        e.name[:-3] for e in result.entries if e.name.endswith(".md")
    )


# ---------- Get recipe markdown ----------

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str):
    try:
        _, res = dbx.files_download(recipe_md_path(name))
        return res.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404)


# ---------- Save / Edit / Rename recipe ----------

@app.post("/api/recipes")
async def save_recipe(
    name: str = Form(...),
    markdown: str = Form(...),
    original_name: str | None = Form(None),
    photo: UploadFile | None = None,
):
    # Rename if needed
    if original_name and original_name != name:
        old_md = recipe_md_path(original_name)
        new_md = recipe_md_path(name)
        old_folder = recipe_folder(original_name)
        new_folder = recipe_folder(name)

        # Ensure new does not already exist
        try:
            dbx.files_get_metadata(new_md)
            raise HTTPException(status_code=400, detail="Recipe name already exists")
        except dropbox.exceptions.ApiError:
            pass

        # Copy markdown
        dbx.files_copy_v2(old_md, new_md)

        # Copy image folder if present
        try:
            dbx.files_copy_v2(old_folder, new_folder)
        except Exception:
            pass

        # Delete old
        dbx.files_delete_v2(old_md)
        try:
            dbx.files_delete_v2(old_folder)
        except Exception:
            pass

    md_path = recipe_md_path(name)
    folder_path = recipe_folder(name)

    # Clean markdown if replacing image
    if photo:
        markdown = strip_image_lines(markdown)

    # Save markdown
    dbx.files_upload(
        markdown.encode("utf-8"),
        md_path,
        mode=dropbox.files.WriteMode.overwrite,
    )

    # Replace photo if present
    if photo:
        try:
            result = dbx.files_list_folder(folder_path)
            for entry in result.entries:
                dbx.files_delete_v2(entry.path_lower)
        except Exception:
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

        image_line = f"\n\n![{photo.filename}]({photo.filename})\n"
        _, res = dbx.files_download(md_path)

        updated_md = res.content.decode("utf-8").rstrip() + image_line

        dbx.files_upload(
            updated_md.encode("utf-8"),
            md_path,
            mode=dropbox.files.WriteMode.overwrite,
        )

    return {"status": "ok"}


# ---------- Delete recipe ----------

@app.delete("/api/recipes/{name}")
def delete_recipe(name: str):
    try:
        dbx.files_delete_v2(recipe_md_path(name))
    except Exception:
        pass

    try:
        dbx.files_delete_v2(recipe_folder(name))
    except Exception:
        pass

    return {"status": "deleted"}


# ---------- Serve photos ----------

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str):
    try:
        _, res = dbx.files_download(f"{RECIPES_ROOT}/{recipe}/{filename}")
        return StreamingResponse(io.BytesIO(res.content), media_type="image/jpeg")
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404)
