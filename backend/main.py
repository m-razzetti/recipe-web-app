from fastapi import FastAPI, UploadFile, Form, HTTPException, Request, Response, Body, File
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import dropbox
import os
import io
import re
import base64
import secrets
import json
import html as html_lib
import time
from datetime import datetime, timedelta
from collections import OrderedDict
import mimetypes
from urllib.parse import urlparse, urljoin
import requests
from pypdf import PdfReader
from PIL import Image, ImageOps, ImageFilter
import pytesseract
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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_OCR_MODEL = os.environ.get("OPENAI_OCR_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
OPENAI_RECIPE_MODEL = os.environ.get("OPENAI_RECIPE_MODEL", OPENAI_OCR_MODEL).strip() or OPENAI_OCR_MODEL
OPENAI_TIMEOUT_SEC = float(os.environ.get("OPENAI_TIMEOUT_SEC", "30"))
URL_FETCH_TIMEOUT_SEC = float(os.environ.get("URL_FETCH_TIMEOUT_SEC", "6"))
URL_IMPORT_TOTAL_TIMEOUT_SEC = float(os.environ.get("URL_IMPORT_TOTAL_TIMEOUT_SEC", "25"))

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
SHOPPING_LIST_PATH = f"{RECIPES_ROOT}/.shopping-list.json"
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

def sanitize_recipe_name(name: str, max_len: int = 120) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "")).strip()
    cleaned = cleaned.replace("/", " ").replace("\\", " ")
    cleaned = re.sub(r"[:*?\"<>|]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned or "Imported Recipe"

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

def clean_ocr_text(raw: str) -> str:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = text.replace("•", "- ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Common OCR slips in ingredient lines like "I cup" or "l tbsp".
    quantity_units = r"(cup|cups|tbsp|tsp|teaspoon|teaspoons|tablespoon|tablespoons|egg|eggs|clove|cloves|oz|lb|lbs|g|kg|ml|l)\b"
    text = re.sub(rf"(?im)^([ \t]*[-*]?[ \t]*)[Il](?=[ \t]+{quantity_units})", r"\g<1>1", text)

    return text.strip()

def score_ocr_text(text: str) -> int:
    if not text:
        return 0
    alpha = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    lines = len([ln for ln in text.splitlines() if ln.strip()])
    weird = len(re.findall(r"[^\w\s\-\.\,\:\;\(\)\/#%&']", text))
    # Favor readable, line-rich content and penalize noisy symbols.
    return (alpha * 3) + (digits * 2) + (lines * 15) - (weird * 8)

def extract_html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return html_lib.unescape(re.sub(r"\s+", " ", match.group(1))).strip()

def extract_meta_value(html: str, key: str) -> str:
    patterns = [
        rf'<meta[^>]*property=["\']{re.escape(key)}["\'][^>]*content=["\'](.*?)["\']',
        rf'<meta[^>]*content=["\'](.*?)["\'][^>]*property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]*name=["\']{re.escape(key)}["\'][^>]*content=["\'](.*?)["\']',
        rf'<meta[^>]*content=["\'](.*?)["\'][^>]*name=["\']{re.escape(key)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            return html_lib.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    return ""

def sanitize_image_filename(filename: str, default_name: str = "imported-image") -> str:
    base = (filename or "").strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")
    if not base:
        base = default_name
    return base

def extension_from_content_type(content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        return ""
    if ct == "image/jpeg":
        return ".jpg"
    ext = mimetypes.guess_extension(ct) or ""
    if ext == ".jpe":
        return ".jpg"
    return ext

def make_unique_filename(filename: str, existing_names: list[str]) -> str:
    used = set(existing_names)
    if filename not in used:
        return filename
    stem, ext = os.path.splitext(filename)
    n = 2
    while True:
        candidate = f"{stem}-{n}{ext}"
        if candidate not in used:
            return candidate
        n += 1

def download_image_from_url(image_url: str, timeout_sec: float) -> tuple[str, str | None, bytes] | None:
    target_url = (image_url or "").strip()
    if not target_url:
        return None
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"}:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    }
    try:
        resp = requests.get(target_url, headers=headers, timeout=max(2, timeout_sec))
    except requests.exceptions.SSLError:
        try:
            resp = requests.get(target_url, headers=headers, timeout=max(2, timeout_sec), verify=False)
        except requests.exceptions.RequestException:
            return None
    except requests.exceptions.RequestException:
        return None

    if not resp.ok:
        return None

    content_type = (resp.headers.get("content-type") or "").strip().lower()
    content = resp.content or b""
    if not content:
        return None
    if len(content) > 12 * 1024 * 1024:
        return None

    detected_ext = ""
    try:
        with Image.open(io.BytesIO(content)) as img:
            fmt = (img.format or "").upper()
        format_map = {
            "JPEG": ".jpg",
            "JPG": ".jpg",
            "PNG": ".png",
            "WEBP": ".webp",
            "HEIC": ".heic",
            "HEIF": ".heif",
        }
        detected_ext = format_map.get(fmt, "")
    except Exception:
        return None

    final_parsed = urlparse(resp.url or target_url)
    raw_name = os.path.basename(final_parsed.path or "") or "imported-image"
    raw_name = sanitize_image_filename(raw_name)
    stem, ext = os.path.splitext(raw_name)
    if not ext:
        ext = extension_from_content_type(content_type) or detected_ext or ".jpg"
    filename = f"{stem}{ext.lower()}"
    if not is_image_file(filename):
        filename = f"{stem}.jpg"
    return filename, content_type or None, content

def fetch_social_context(url: str, timeout_sec: float) -> dict:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    }

    if host.endswith("tiktok.com"):
        params = {"url": url}
        try:
            oembed = requests.get("https://www.tiktok.com/oembed", params=params, headers=headers, timeout=max(2, timeout_sec))
        except requests.exceptions.SSLError:
            oembed = requests.get("https://www.tiktok.com/oembed", params=params, headers=headers, timeout=max(2, timeout_sec), verify=False)
        except requests.exceptions.RequestException:
            oembed = None

        if oembed is not None and oembed.ok:
            try:
                payload = oembed.json()
            except ValueError:
                payload = {}
            title = str(payload.get("title") or "").strip()
            author = str(payload.get("author_name") or "").strip()
            image_url = str(payload.get("thumbnail_url") or "").strip()
            description = title
            if author and description:
                description = f"{description}\nCreator: {author}"
            if title or description:
                return {
                    "final_url": url,
                    "title": title,
                    "description": description,
                    "image_url": image_url,
                }

    try:
        resp = requests.get(url, headers=headers, timeout=max(2, timeout_sec))
    except requests.exceptions.SSLError:
        try:
            resp = requests.get(url, headers=headers, timeout=max(2, timeout_sec), verify=False)
        except requests.exceptions.RequestException as exc:
            raise HTTPException(status_code=400, detail=f"Could not open URL: {exc}")
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Could not open URL: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"Could not open URL (HTTP {resp.status_code})")

    html = resp.text[:300000]
    title = extract_meta_value(html, "og:title") or extract_meta_value(html, "twitter:title") or extract_html_title(html)
    description = (
        extract_meta_value(html, "og:description")
        or extract_meta_value(html, "twitter:description")
        or extract_meta_value(html, "description")
    )
    image_url = (
        extract_meta_value(html, "og:image")
        or extract_meta_value(html, "twitter:image")
    )
    if image_url:
        image_url = urljoin(resp.url, image_url)

    return {
        "final_url": resp.url,
        "title": title.strip(),
        "description": description.strip(),
        "image_url": image_url.strip(),
    }

def openai_recipe_from_social(url: str, title: str, description: str, notes: str = "", timeout_sec: float | None = None) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for URL import")

    source_text = "\n".join([
        f"URL: {url}",
        f"Title: {title or '(none)'}",
        f"Description/Captions: {description or '(none)'}",
        f"User Notes: {notes or '(none)'}",
    ])

    prompt = (
        "Convert the provided social-media recipe source text into markdown recipe format.\n"
        "Rules:\n"
        "- Use only information explicitly present in the source text.\n"
        "- Do not invent ingredient amounts or steps.\n"
        "- If details are missing, still produce a useful draft with headings.\n"
        "- Output markdown only.\n"
        "- Include a top-level title, then '## Ingredients' and '## Instructions' when possible.\n\n"
        f"{source_text}"
    )

    body = {
        "model": OPENAI_RECIPE_MODEL,
        "temperature": 0,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=body,
            timeout=(timeout_sec if timeout_sec is not None else OPENAI_TIMEOUT_SEC),
        )
    except requests.exceptions.SSLError:
        try:
            resp = requests.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=body,
                timeout=(timeout_sec if timeout_sec is not None else OPENAI_TIMEOUT_SEC),
                verify=False,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"OpenAI request failed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OpenAI request failed: {exc}")

    if not resp.ok:
        raise HTTPException(status_code=400, detail=f"OpenAI error: HTTP {resp.status_code}")

    payload = resp.json()
    markdown = extract_openai_output_text(payload).strip()
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    if not markdown:
        raise HTTPException(status_code=400, detail="No usable recipe content found from URL")
    return markdown + ("\n" if not markdown.endswith("\n") else "")

def detect_image_mime(filename: str, content_type: str | None = None) -> str:
    if content_type and content_type.startswith("image/"):
        return content_type
    lower = (filename or "").lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"

def extract_openai_output_text(payload: dict) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()

def extract_image_text_with_openai(content: bytes, filename: str, content_type: str | None = None) -> str:
    if not OPENAI_API_KEY:
        return ""

    normalized_name, normalized_content = normalize_photo_upload(filename, content_type, content)
    mime = detect_image_mime(normalized_name, content_type)
    b64 = base64.b64encode(normalized_content).decode("ascii")
    image_url = f"data:{mime};base64,{b64}"

    prompt = (
        "Extract all legible recipe text from this image. "
        "Return plain text only, preserving line breaks and section headings. "
        "Do not add commentary."
    )

    body = {
        "model": OPENAI_OCR_MODEL,
        "temperature": 0,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=body,
            timeout=OPENAI_TIMEOUT_SEC,
        )
        if not resp.ok:
            return ""
        payload = resp.json()
        return clean_ocr_text(extract_openai_output_text(payload))
    except requests.exceptions.SSLError:
        try:
            resp = requests.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=body,
                timeout=OPENAI_TIMEOUT_SEC,
                verify=False,
            )
            if not resp.ok:
                return ""
            payload = resp.json()
            return clean_ocr_text(extract_openai_output_text(payload))
        except Exception:
            return ""
    except Exception:
        return ""

def extract_image_text(content: bytes, filename: str, content_type: str | None = None) -> str:
    ai_text = extract_image_text_with_openai(content, filename, content_type)
    if ai_text.strip():
        return ai_text

    _normalized_name, normalized_content = normalize_photo_upload(filename, content_type, content)
    try:
        with Image.open(io.BytesIO(normalized_content)) as img:
            normalized = ImageOps.exif_transpose(img).convert("RGB")
            gray = ImageOps.autocontrast(ImageOps.grayscale(normalized))
            sharpened = gray.filter(ImageFilter.SHARPEN)
            binary = gray.point(lambda px: 0 if px < 170 else 255, mode="1")

            variants = [normalized, gray, sharpened, binary]
            psm_modes = [6, 4, 11]
            best_text = ""
            best_score = -1

            for variant in variants:
                for psm in psm_modes:
                    config = f"--oem 1 --psm {psm}"
                    candidate = pytesseract.image_to_string(variant, lang="eng", config=config)
                    candidate = clean_ocr_text(candidate)
                    score = score_ocr_text(candidate)
                    if score > best_score:
                        best_score = score
                        best_text = candidate

            return best_text.strip()
    except pytesseract.TesseractNotFoundError:
        raise HTTPException(status_code=500, detail="OCR engine is not installed on the server")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Image OCR failed: {exc}")

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
    raw_images = item.get("images", [])
    if not isinstance(raw_images, list):
        raw_images = []
    images: list[str] = []
    for image_name in raw_images:
        clean_name = str(image_name).strip()
        if not clean_name or clean_name in images:
            continue
        if not is_image_file(clean_name):
            continue
        images.append(clean_name)
    if cover and cover not in images and is_image_file(cover):
        images.append(cover)
    if not cover and images:
        cover = images[0]
    return {
        "name": name,
        "tags": normalize_tags(" ".join(str(t) for t in tags)),
        "cover": cover,
        "images": images,
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
    has_legacy_created_at = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        if "created_at" in item:
            has_legacy_created_at = True
        entry = normalize_recipe_item(item)
        if entry:
            normalized.append(entry)

    # Remove stale index items whose markdown file no longer exists in Dropbox.
    md_names = {
        entry.name[:-3]
        for entry in list_recipe_root_entries()
        if isinstance(entry, dropbox.files.FileMetadata) and entry.name.endswith(".md")
    }
    filtered = [item for item in normalized if item["name"] in md_names]
    had_stale_entries = len(filtered) != len(normalized)
    normalized = filtered

    normalized = sort_recipes(normalized)
    if has_legacy_created_at or had_stale_entries:
        save_recipe_index(normalized)
    return normalized

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

def list_recipe_images(name: str) -> list[str]:
    try:
        result = dbx.files_list_folder(recipe_folder(name))
    except dropbox.exceptions.ApiError:
        return []

    images: list[str] = []
    while True:
        for entry in result.entries:
            if is_image_file(entry.name):
                images.append(entry.name)
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    return sorted(list(dict.fromkeys(images)), key=str.lower)

def normalize_shopping_item(item: dict | str, fallback_index: int = 0) -> dict:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return {}
        return {
            "id": f"legacy-{fallback_index}",
            "text": text,
            "checked": False,
        }
    if not isinstance(item, dict):
        return {}
    item_id = str(item.get("id", "")).strip()
    text = str(item.get("text", "")).strip()
    checked = bool(item.get("checked", False))
    if not item_id or not text:
        return {}
    return {
        "id": item_id,
        "text": text,
        "checked": checked,
    }

def load_shopping_list() -> list[dict]:
    try:
        _, res = dbx.files_download(SHOPPING_LIST_PATH)
    except dropbox.exceptions.ApiError:
        return []

    try:
        raw = json.loads(res.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []

    if isinstance(raw, dict):
        raw_items = raw.get("items", [])
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raw_items = []

    if not isinstance(raw_items, list):
        raw_items = []

    normalized: list[dict] = []
    for idx, item in enumerate(raw_items):
        clean = normalize_shopping_item(item, idx)
        if clean:
            normalized.append(clean)
    return normalized

def save_shopping_list(items: list[dict]):
    payload = json.dumps({"items": items}, separators=(",", ":")).encode("utf-8")
    dbx.files_upload(
        payload,
        SHOPPING_LIST_PATH,
        mode=dropbox.files.WriteMode.overwrite,
    )

def get_recipe_modified_dates() -> dict[str, str]:
    cached = get_cached_recipe_dates()
    if cached is not None:
        return cached

    modified = {}
    for entry in list_recipe_root_entries():
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue
        if not entry.name.endswith(".md"):
            continue
        name = entry.name[:-3]
        if hasattr(entry, "server_modified"):
            modified[name] = entry.server_modified.isoformat()
    set_cached_recipe_dates(modified)
    return modified

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
        images: list[str] = []
        if name in folder_names:
            images = list_recipe_images(name)
            if images:
                cover = images[0]

        recipes.append({
            "name": name,
            "tags": extract_tags(md),
            "cover": cover,
            "images": images,
        })

    return sort_recipes(recipes)

# --------------------------
# Cache Layer
# --------------------------

RECIPES_CACHE_TTL = timedelta(seconds=20)
RECIPE_MD_CACHE_TTL = timedelta(seconds=60)
PHOTO_CACHE_TTL = timedelta(minutes=15)
PHOTO_CACHE_MAX_ITEMS = 128
RECIPE_DATES_CACHE_TTL = timedelta(seconds=20)
SHOPPING_LIST_CACHE_TTL = timedelta(seconds=10)

recipes_cache = {"value": None, "expires_at": datetime.min}
recipe_md_cache: dict[str, tuple[str, datetime]] = {}
photo_cache: OrderedDict[str, dict] = OrderedDict()
recipe_dates_cache = {"value": None, "expires_at": datetime.min}
shopping_list_cache = {"value": None, "expires_at": datetime.min}

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

def get_cached_recipe_dates():
    if recipe_dates_cache["value"] is not None and cache_fresh(recipe_dates_cache["expires_at"]):
        return recipe_dates_cache["value"]
    return None

def set_cached_recipe_dates(data):
    recipe_dates_cache["value"] = data
    recipe_dates_cache["expires_at"] = datetime.utcnow() + RECIPE_DATES_CACHE_TTL

def clear_recipe_dates_cache():
    recipe_dates_cache["value"] = None
    recipe_dates_cache["expires_at"] = datetime.min

def get_cached_shopping_list():
    if shopping_list_cache["value"] is not None and cache_fresh(shopping_list_cache["expires_at"]):
        return shopping_list_cache["value"]
    return None

def set_cached_shopping_list(data: list[dict]):
    shopping_list_cache["value"] = data
    shopping_list_cache["expires_at"] = datetime.utcnow() + SHOPPING_LIST_CACHE_TTL

def clear_shopping_list_cache():
    shopping_list_cache["value"] = None
    shopping_list_cache["expires_at"] = datetime.min

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
    clear_recipe_dates_cache()
    clear_photo_cache(name)
    clear_recipe_md_cache(name)

# --------------------------
# AUTH ROUTES
# --------------------------

@app.post("/api/login")
def login(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
    submitted_username = (username or "").strip()
    submitted_password = (password or "").replace("\u00a0", " ")
    if submitted_password != ADMIN_PASSWORD and submitted_password.strip() == ADMIN_PASSWORD:
        submitted_password = submitted_password.strip()

    username_ok = submitted_username == ADMIN_USERNAME or submitted_username.lower() == ADMIN_USERNAME.lower()
    password_ok = submitted_password == ADMIN_PASSWORD
    if not username_ok or not password_ok:
        raise HTTPException(status_code=401)

    token = create_session()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    secure_cookie = request.url.scheme == "https" or forwarded_proto == "https"

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=secure_cookie,
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
    modified_dates = get_recipe_modified_dates()
    page_with_dates = [
        {
            **item,
            "created_at": modified_dates.get(item["name"]),
        }
        for item in page
    ]

    next_offset = safe_offset + len(page)
    response.headers["X-Total-Count"] = str(len(filtered))
    response.headers["X-Next-Offset"] = str(next_offset) if next_offset < len(filtered) else ""
    return page_with_dates

def load_recipe_markdown_or_404(name: str) -> str:
    try:
        _, res = dbx.files_download(recipe_md_path(name))
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return res.content.decode("utf-8")

@app.get("/api/recipes/{name}", response_class=PlainTextResponse)
def get_recipe(name: str, request: Request):
    require_auth(request)
    cached = get_cached_recipe_md(name)
    if cached is not None:
        return cached
    markdown = load_recipe_markdown_or_404(name)
    set_cached_recipe_md(name, markdown)
    return markdown

@app.get("/api/recipe", response_class=PlainTextResponse)
def get_recipe_by_query(request: Request, name: str):
    require_auth(request)
    cached = get_cached_recipe_md(name)
    if cached is not None:
        return cached
    markdown = load_recipe_markdown_or_404(name)
    set_cached_recipe_md(name, markdown)
    return markdown

@app.get("/api/shopping-list")
def get_shopping_list(request: Request):
    require_auth(request)
    items = get_cached_shopping_list()
    if items is None:
        items = load_shopping_list()
        set_cached_shopping_list(items)
    return {"items": items}

@app.post("/api/shopping-list/items")
def add_shopping_item(
    request: Request,
    payload: dict = Body(...),
):
    require_auth(request)
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    items = load_shopping_list()
    new_item = {
        "id": secrets.token_hex(8),
        "text": text,
        "checked": False,
    }
    items.append(new_item)
    save_shopping_list(items)
    set_cached_shopping_list(items)
    return {"status": "ok", "item": new_item}

@app.post("/api/shopping-list/items/{item_id}/toggle")
def toggle_shopping_item(item_id: str, request: Request):
    require_auth(request)
    items = load_shopping_list()
    for item in items:
        if item.get("id") == item_id:
            item["checked"] = not bool(item.get("checked", False))
            save_shopping_list(items)
            set_cached_shopping_list(items)
            return {"status": "ok", "item": item}
    raise HTTPException(status_code=404, detail="Item not found")

@app.put("/api/shopping-list/items/{item_id}")
def update_shopping_item(
    item_id: str,
    request: Request,
    payload: dict = Body(...),
):
    require_auth(request)
    new_text = str(payload.get("text", "")).strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="text is required")

    items = load_shopping_list()
    for item in items:
        if item.get("id") == item_id:
            item["text"] = new_text
            save_shopping_list(items)
            set_cached_shopping_list(items)
            return {"status": "ok", "item": item}
    raise HTTPException(status_code=404, detail="Item not found")

@app.delete("/api/shopping-list/items/{item_id}")
def delete_shopping_item(item_id: str, request: Request):
    require_auth(request)
    items = load_shopping_list()
    kept = [item for item in items if item.get("id") != item_id]
    if len(kept) == len(items):
        raise HTTPException(status_code=404, detail="Item not found")
    save_shopping_list(kept)
    set_cached_shopping_list(kept)
    return {"status": "deleted"}

@app.post("/api/shopping-list/clear-checked")
def clear_checked_shopping_items(request: Request):
    require_auth(request)
    items = load_shopping_list()
    kept = [item for item in items if not item.get("checked")]
    save_shopping_list(kept)
    set_cached_shopping_list(kept)
    return {"status": "ok", "removed": len(items) - len(kept)}

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
        elif lower_name.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")) or (source_file.content_type or "").startswith("image/"):
            raw_text = extract_image_text(content, filename, source_file.content_type)
            if not raw_text:
                raise HTTPException(status_code=400, detail="Could not extract text from image")
        elif lower_name.endswith(".txt") or lower_name.endswith(".md") or source_file.content_type in {"text/plain", "text/markdown"}:
            raw_text = content.decode("utf-8", errors="replace")
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type. Use .txt, .md, .pdf, or an image")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Import conversion failed: {exc}")

    suggested_name = filename_to_title(filename) or "Imported Recipe"
    markdown = text_to_markdown(raw_text, fallback_title=suggested_name)
    if not markdown.strip():
        raise HTTPException(status_code=400, detail="No usable content found")

    return {"name": suggested_name, "markdown": markdown}

@app.post("/api/import/url")
def convert_import_url(
    request: Request,
    url: str = Form(...),
    notes: str = Form(""),
):
    require_auth(request)
    source_url = (url or "").strip()
    if not source_url:
        raise HTTPException(status_code=400, detail="URL is required")
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    started = time.time()
    context = fetch_social_context(source_url, timeout_sec=URL_FETCH_TIMEOUT_SEC)
    elapsed = time.time() - started
    remaining = URL_IMPORT_TOTAL_TIMEOUT_SEC - elapsed
    if remaining <= 5:
        raise HTTPException(status_code=400, detail="URL import timed out before recipe extraction")

    host = (parsed.netloc or "").lower()
    if host.endswith("tiktok.com") or host.endswith("instagram.com"):
        has_any_text = bool((context.get("title") or "").strip() or (context.get("description") or "").strip())
        if not has_any_text:
            raise HTTPException(
                status_code=400,
                detail="No readable caption/metadata found for this social link. Try adding notes or importing from a screenshot."
            )

    markdown = openai_recipe_from_social(
        context.get("final_url") or source_url,
        context.get("title") or "",
        context.get("description") or "",
        notes=(notes or "").strip(),
        timeout_sec=max(4, min(12, OPENAI_TIMEOUT_SEC, remaining)),
    )

    title_candidate = filename_to_title(context.get("title") or "") or filename_to_title((parsed.path or "").strip("/").split("/")[-1])
    suggested_name = sanitize_recipe_name(title_candidate or "Imported Recipe")
    return {"name": suggested_name, "markdown": markdown, "image_url": (context.get("image_url") or "").strip()}

@app.post("/api/recipes")
async def save_recipe(
    request: Request,
    name: str = Form(...),
    markdown: str = Form(...),
    tags: str = Form(""),
    original_name: str = Form(""),
    cover: str = Form(""),
    source_image_url: str = Form(""),
    photo: UploadFile | None = None,
    photos: list[UploadFile] = File(default=[]),
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

    old_images = []
    if old_entry:
        old_images = list(old_entry.get("images") or [])
        if not old_images and old_entry.get("cover"):
            old_images = [old_entry["cover"]]
    saved_images = list(dict.fromkeys(old_images))
    saved_cover = old_entry["cover"] if old_entry else None

    upload_photos = list(photos or [])
    if photo is not None:
        upload_photos.append(photo)

    if upload_photos:
        try:
            dbx.files_create_folder_v2(recipe_folder(name))
        except:
            pass

        for upload in upload_photos:
            if not upload.filename:
                continue
            content = await upload.read()
            if not content:
                continue
            upload_name, upload_content = normalize_photo_upload(
                upload.filename or "photo",
                upload.content_type,
                content,
            )
            dbx.files_upload(
                upload_content,
                f"{recipe_folder(name)}/{upload_name}",
                mode=dropbox.files.WriteMode.overwrite,
            )
            if upload_name not in saved_images:
                saved_images.append(upload_name)

    source_image_url = (source_image_url or "").strip()
    if source_image_url and not upload_photos:
        downloaded = download_image_from_url(source_image_url, timeout_sec=URL_FETCH_TIMEOUT_SEC)
        if downloaded is not None:
            source_name, source_content_type, source_content = downloaded
            source_name = make_unique_filename(source_name, saved_images)
            normalized_name, normalized_content = normalize_photo_upload(
                source_name,
                source_content_type,
                source_content,
            )
            try:
                dbx.files_create_folder_v2(recipe_folder(name))
            except:
                pass
            dbx.files_upload(
                normalized_content,
                f"{recipe_folder(name)}/{normalized_name}",
                mode=dropbox.files.WriteMode.overwrite,
            )
            if normalized_name not in saved_images:
                saved_images.append(normalized_name)

    requested_cover = cover.strip()
    if requested_cover:
        if requested_cover not in saved_images:
            raise HTTPException(status_code=400, detail="Selected title image does not exist")
        saved_cover = requested_cover
    elif not saved_cover and saved_images:
        saved_cover = saved_images[0]
    elif saved_cover and saved_cover not in saved_images:
        saved_cover = saved_images[0] if saved_images else None

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
        "images": saved_images,
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
    try:
        dbx.files_delete_v2(recipe_md_path(name))
    except dropbox.exceptions.ApiError:
        raise HTTPException(status_code=404, detail="Recipe not found")
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

@app.delete("/api/recipe")
def delete_recipe_by_query(request: Request, name: str):
    return delete_recipe(name=name, request=request)

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
        remaining_images = list_recipe_images(name)
        changed = False
        for item in index_items:
            if item["name"] != name:
                continue
            images = list(remaining_images)
            if item.get("cover") == filename:
                item["cover"] = images[0] if images else None
                changed = True
            if len(images) != len(item.get("images") or []):
                item["images"] = images
                changed = True
                break
        if changed:
            save_recipe_index(index_items)
            set_cached_recipes(sort_recipes(index_items))
    return {"status": "photo deleted"}

@app.delete("/api/recipe/photo")
def delete_recipe_photo_by_query(request: Request, name: str, filename: str):
    return delete_recipe_photo(name=name, filename=filename, request=request)

@app.get("/api/recipes/{name}/photos")
def list_recipe_photos(name: str, request: Request):
    require_auth(request)
    photos = list_recipe_images(name)
    index_items = load_recipe_index() or []
    cover = None
    for item in index_items:
        if item.get("name") == name:
            cover = item.get("cover")
            break
    if cover and cover not in photos:
        cover = None
    return {"images": photos, "cover": cover}

@app.get("/api/recipe/photos")
def list_recipe_photos_by_query(request: Request, name: str):
    return list_recipe_photos(name=name, request=request)

@app.post("/api/recipes/{name}/cover")
def set_recipe_cover(
    name: str,
    request: Request,
    payload: dict = Body(...),
):
    require_auth(request)
    cover = str(payload.get("cover", "")).strip()
    if not cover:
        raise HTTPException(status_code=400, detail="cover is required")

    index_items = load_recipe_index() or []
    changed = False
    for item in index_items:
        if item.get("name") != name:
            continue
        images = list_recipe_images(name)
        if not images:
            images = list(dict.fromkeys(item.get("images") or []))
        if not images and item.get("cover"):
            images = [item["cover"]]
        if cover not in images:
            raise HTTPException(status_code=400, detail="cover must be one of this recipe's images")
        item["images"] = images
        item["cover"] = cover
        changed = True
        break

    if not changed:
        raise HTTPException(status_code=404, detail="Recipe not found")

    save_recipe_index(index_items)
    set_cached_recipes(sort_recipes(index_items))
    invalidate_for_recipe_change(name)
    return {"status": "ok", "cover": cover}

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

def build_photo_response(recipe: str, filename: str, request: Request):
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
            try:
                meta, res = dbx.files_download(path)
                content = res.content
                media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            except dropbox.exceptions.ApiError:
                raise HTTPException(status_code=404, detail="Photo not found")
    else:
        try:
            meta, res = dbx.files_download(path)
            content = res.content
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        except dropbox.exceptions.ApiError:
            raise HTTPException(status_code=404, detail="Photo not found")

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

@app.get("/api/photos/{recipe}/{filename}")
def get_photo(recipe: str, filename: str, request: Request):
    return build_photo_response(recipe=recipe, filename=filename, request=request)

@app.get("/api/photo")
def get_photo_by_query(request: Request, recipe: str, filename: str):
    return build_photo_response(recipe=recipe, filename=filename, request=request)
