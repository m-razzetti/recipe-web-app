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
    allow_origins=["https://recipes.razzetti.org"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def extract_tags(markdown: str):
    match = re.search(r"^Tags:\s*(.+)$", markdown, re.MULTILINE)
    if not match:
        return []
    return [t.strip() for t in match.group(1).split(",") if t.strip()]


def replace_tags(markdown: str, new_tags: list[str]):
    markdown = re.sub(r"^Tags:.*\n+", "", markdown, flags=re.MULTILINE)

    if not new_tags:
        return markdown.lstrip()

    tag_line = f"Tags: {', '.join(new_tags)}\n\n"
    return tag_line + markdown.lstrip()


# --------------------------
# List Recipes
# --------------------------

@app.get("/api/recipes")
def list_recipes():
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


# --------------------------
# Get Recipe
# --------------------------

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str):
    try:
        _, res = dbx.files_download(recipe_md_path(name))
        return res.content.decode("utf-8")
    except:
        raise HTTPException(status_code=404)


# --------------------------
# Save Recipe
# --------------------------

@app.post("/api/recipes")
async def save_recipe(
    name: str = Form(...),
    markdown: str = Form(...),
    tags: str = Form(""),
    photo: UploadFile | None = None,
):
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
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


# --------------------------
# Delete Recipe
# --------------------------

@app.delete("/api/recipes/{name}")
def delete_recipe(name: str):
    dbx.files_delete_v2(recipe_md_path(name))
    try:
        dbx.files_delete_v2(recipe_folder(name))
    except:
        pass
    return {"status": "deleted"}


# --------------------------
# DELETE TAG GLOBALLY
# --------------------------

@app.delete("/api/tags/{tag}")
def delete_tag(tag: str):
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


# --------------------------
# Serve Photos
# --------------------------

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str):
    path = f"{RECIPES_ROOT}/{recipe}/{filename}"
    _, res = dbx.files_download(path)

    return StreamingResponse(
        io.BytesIO(res.content),
        media_type="image/jpeg",
    )
