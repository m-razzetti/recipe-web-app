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
            names.append(entry.name[:-3])

    return sorted(names)


# ---------- Get recipe markdown ----------

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str):
    try:
        _, res = dbx.files_download(recipe_md_path(name))
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

    # Handle photo
    if photo:
        try:
            result = dbx.files_list_folder(folder_path)
            for entry in result.entries:
                if entry.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    dbx.files_delete_v2(f"{folder_path}/{entry.name}")
        except Exception:
            dbx.files_create_folder_v2(folder_path)

        content = await photo.read()
        img_path = f"{folder_path}/{photo.filename}"

        dbx.files_upload(
            content,
            img_path,
            mode=dropbox.files.WriteMode.overwrite,
        )

        # Append image reference if missing
        _, res = dbx.files_download(md_path)
        text = res.content.decode("utf-8")

        image_line = f"![{photo.filename}]({photo.filename})"
        if image_line not in text:
            text += f"\n\n{image_line}\n"

        dbx.files_upload(
            text.encode("utf-8"),
            md_path,
            mode=dropbox.files.WriteMode.overwrite,
        )

    return {"status": "ok"}


# ---------- Serve photos ----------

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str):
    try:
        _, res = dbx.files_download(f"{RECIPES_ROOT}/{recipe}/{filename}")
        return StreamingResponse(io.BytesIO(res.content), media_type="image/jpeg")
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404)


# ---------- Get cover image for tiles ----------

@app.get("/api/recipes/{name}/cover")
def get_recipe_cover(name: str):
    folder = recipe_folder(name)

    try:
        result = dbx.files_list_folder(folder)
    except dropbox.exceptions.ApiError:
        return {"url": None}

    for entry in result.entries:
        if entry.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return {"url": f"/photos/{name}/{entry.name}"}

    return {"url": None}
