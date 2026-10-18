import logging
import re
from pathlib import Path
from typing import Optional

import httpx
from ddgs import DDGS

from app.tools.project_scaffold import get_desktop_dir

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """Sanitize a string to be safely used as a filename."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    cleaned = cleaned.strip().replace(" ", "_").lower()
    return cleaned or "downloaded_image"


def download_web_image(
    query: str,
    folder_name: str = "vedio",
    filename: Optional[str] = None,
) -> dict:
    """Search DuckDuckGo Images for query, download the highest quality image,
    and save it into the specified folder on the user's Desktop.
    """
    if not query or not query.strip():
        return {"ok": False, "error": "Search query is empty."}

    clean_query = query.strip()
    logger.info("Searching web for image: %s (folder: %s)", clean_query, folder_name)

    # 1. Search for images
    image_candidates = []
    try:
        with DDGS() as client:
            results = client.images(clean_query, max_results=6)
            image_candidates = list(results)
    except Exception as exc:
        logger.warning("DDGS image search error: %s", exc)
        return {"ok": False, "error": f"Image search error: {exc}"}

    if not image_candidates:
        return {"ok": False, "error": f"No images found for query: '{clean_query}'"}

    # 2. Determine target directory using the 6 standard user folders
    from app.tools.user_folders import resolve_user_destination, get_user_folder
    target_dir, category = resolve_user_destination(folder_name, default_category="pictures")

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": f"Could not create folder {target_dir}: {exc}"}

    # 3. Base filename
    if filename and filename.strip():
        base_name = sanitize_filename(filename.strip())
        if not any(base_name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            base_name += ".jpg"
    else:
        base_name = f"{sanitize_filename(clean_query)}.jpg"

    # 4. Attempt downloading candidates
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    last_error = ""
    for candidate in image_candidates:
        urls_to_try = []
        if candidate.get("image"):
            urls_to_try.append(candidate["image"])
        if candidate.get("thumbnail"):
            urls_to_try.append(candidate["thumbnail"])

        for img_url in urls_to_try:
            try:
                with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as http:
                    resp = http.get(img_url)
                    if resp.status_code == 200 and len(resp.content) > 1024:
                        # Determine extension from Content-Type if possible
                        content_type = resp.headers.get("content-type", "").lower()
                        ext = ".jpg"
                        if "png" in content_type:
                            ext = ".png"
                        elif "webp" in content_type:
                            ext = ".webp"
                        elif "jpeg" in content_type:
                            ext = ".jpg"

                        if not base_name.endswith(ext):
                            file_stem = Path(base_name).stem
                            final_filename = f"{file_stem}{ext}"
                        else:
                            final_filename = base_name

                        save_path = target_dir / final_filename
                        save_path.write_bytes(resp.content)

                        # If saved to Videos and desktop/vedio folder exists, mirror it so user finds it in both places
                        try:
                            from app.tools.project_scaffold import get_desktop_dir
                            desktop_vedio = get_desktop_dir() / "vedio"
                            if desktop_vedio.is_dir() and category == "videos":
                                (desktop_vedio / final_filename).write_bytes(resp.content)
                        except Exception:
                            pass

                        logger.info("Successfully saved image to %s (%d bytes)", save_path, len(resp.content))
                        return {
                            "ok": True,
                            "path": str(save_path),
                            "filename": final_filename,
                            "folder": str(target_dir),
                            "size_bytes": len(resp.content),
                            "source_url": img_url,
                            "title": candidate.get("title", clean_query),
                        }
            except Exception as e:
                last_error = str(e)
                continue

    return {
        "ok": False,
        "error": f"Failed to download image from candidates. Last error: {last_error}",
    }
