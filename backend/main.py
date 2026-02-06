from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import dropbox
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
    """
    Remove markdown image lines like:
    ![alt](file.jpg)
    """
    return re.sub(r"\n*\!\[.*?\]\(.*?\)\n*", "\n\n", markdown).strip() + "\n"


# ---------- List recipes ----------

@app.get("/api/recipes")
def list_recipes():
    result = dbx.files_list_folder(RECIPES_ROOT)
    return sorted(
        entry.name[:-3]
        for entry in result.entries
        if entry.name.endswith(".md")
    )


# ---------- Get recipe markdown ----------

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str):
    try:
        _, res = dbx.files_download(recipe_md_path(name))
        return res.content.decode("utf-8")
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404, detail="Recipe not found")


# ---------- Save / Edit recipe + optional photo ----------

@app.post("/api/recipes")
async def save_recipe(
    name: str = Form(...),
    markdown: str = Form(...),
    photo: UploadFile | None = None,
):
    md_path = recipe_md_path(name)
    folder_path = recipe_folder(name)

    # If replacing photo, remove old image folder + image refs
    if photo:
        try:
            dbx.files_delete_v2(folder_path)
        except dropbox.exceptions.ApiError:
            pass  # folder may not exist

        # Strip old image markdown
        markdown = strip_image_lines(markdown)

    # Save markdown (base version)
    dbx.files_upload(
        markdown.encode("utf-8"),
        md_path,
        mode=dropbox.files.WriteMode.overwrite,
    )

    # Save new photo if present
    if photo:
        dbx.files_create_folder_v2(folder_path)

        img_path = f"{folder_path}/{photo.filename}"
        content = await photo.read()

        dbx.files_upload(
            content,
            img_path,
            mode=dropbox.files.WriteMode.overwrite,
        )

        # Append new image reference
        image_line = f"\n\n![{photo.filename}]({photo.filename})\n"

        _, res = dbx.files_download(md_path)
        updated_md = res.content.decode("utf-8") + image_line

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
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404)

    try:
        dbx.files_delete_v2(recipe_folder(name))
    except dropbox.exceptions.ApiError:
        pass

    return {"status": "deleted"}


# ---------- Serve recipe photos ----------

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str):
    try:
        _, res = dbx.files_download(f"{RECIPES_ROOT}/{recipe}/{filename}")
        return StreamingResponse(io.BytesIO(res.content), media_type="image/jpeg")
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404)
