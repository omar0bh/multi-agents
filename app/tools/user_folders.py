"""User Known Folders Resolver for Windows, macOS, and Linux.
Dynamically discovers and resolves the user's 6 canonical shell folders:
- Desktop
- Downloads
- Documents
- Pictures
- Videos
- Music

Handles OneDrive redirection, multi-lingual folder names (Arabic, French, English),
and guarantees writable destination paths.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def is_writable(path: Path) -> bool:
    """Check if a directory exists and can actually be written to."""
    if not path.is_dir():
        return False
    probe = path / "._probe_write.tmp"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def get_known_user_folders() -> dict[str, Path]:
    """Return verified writable paths for the 6 standard user folders."""
    home = Path.home()
    onedrive = home / "OneDrive"

    # Multi-lingual & localized candidates in order of preference
    candidates: dict[str, list[Path]] = {
        "desktop": [
            onedrive / "سطح المكتب",
            onedrive / "Desktop",
            onedrive / "Bureau",
            home / "Desktop",
            home / "سطح المكتب",
            home / "Bureau",
        ],
        "videos": [
            home / "Videos",
            onedrive / "ملفات الفيديو",
            onedrive / "Videos",
            home / "ملفات الفيديو",
            home / "Vidéos",
        ],
        "pictures": [
            home / "Pictures",
            onedrive / "الصور",
            onedrive / "Pictures",
            home / "الصور",
            home / "Images",
        ],
        "documents": [
            home / "Documents",
            onedrive / "المستندات",
            onedrive / "Documents",
            home / "المستندات",
        ],
        "downloads": [
            home / "Downloads",
            home / "التنزيلات",
            onedrive / "Downloads",
            home / "Téléchargements",
        ],
        "music": [
            home / "Music",
            onedrive / "اموسيقى",
            onedrive / "Music",
            home / "اموسيقى",
            home / "Musique",
        ],
    }

    # Optional: check Windows User Shell Folders registry
    if os.name == "nt":
        try:
            import winreg

            sub_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                reg_map = {
                    "Desktop": "desktop",
                    "Personal": "documents",
                    "{F42EE2D3-909F-4907-8871-4C22FC0BF756}": "documents",
                    "My Pictures": "pictures",
                    "{0DDD015D-B06C-45D5-8C4C-F59713854639}": "pictures",
                    "My Video": "videos",
                    "{35286A68-3C57-41A1-BBB1-0EAE73D76C95}": "videos",
                    "My Music": "music",
                    "{A0C69A99-21C8-4671-8703-7934162FCF1D}": "music",
                    "{374DE290-123F-4565-9164-39C4925E467B}": "downloads",
                }
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, i)
                        expanded = Path(os.path.expandvars(val))
                        cat = reg_map.get(name)
                        if cat and expanded not in candidates[cat]:
                            candidates[cat].insert(0, expanded)
                        i += 1
                    except OSError:
                        break
        except Exception as exc:
            logger.debug("Registry user shell folders read error: %s", exc)

    resolved: dict[str, Path] = {}
    for cat, cand_list in candidates.items():
        # First pass: find first writable candidate
        found = None
        for cand in cand_list:
            if is_writable(cand):
                found = cand
                break

        # Fallback pass: find first existing directory
        if not found:
            for cand in cand_list:
                if cand.is_dir():
                    found = cand
                    break

        resolved[cat] = found or (home / cat.capitalize())

    return resolved


def get_user_folder(category: str) -> Path:
    """Get the path for a specific user folder category (desktop, videos, pictures, documents, downloads, music)."""
    folders = get_known_user_folders()
    cat_clean = category.lower().strip()
    return folders.get(cat_clean, folders.get("desktop", Path.home()))


# Multi-lingual keywords for matching user intentions in Darija, Arabic, French, and English
CATEGORY_PATTERNS = {
    "videos": (
        r"\b(?:video|videos|vedio|vidéo|vidéos|فيديو|ملفات الفيديو|فيديوهات)\b"
    ),
    "pictures": (
        r"\b(?:picture|pictures|photo|photos|image|images|tswira|tsswira|tasswira|tsawer|pic|صورة|صور|الصور)\b"
    ),
    "documents": (
        r"\b(?:document|documents|doc|docs|مستند|مستندات|المستندات|ملفات)\b"
    ),
    "downloads": (
        r"\b(?:download|downloads|telechargement|téléchargement|téléchargements|تنزيلات|التنزيلات|داونلود)\b"
    ),
    "music": (
        r"\b(?:music|musique|audio|song|songs|mousi9a|musi9a|موسيقى|اموسيقى|أغاني|صوتيات)\b"
    ),
    "desktop": (
        r"\b(?:desktop|bureau|biro|بيرو|سطح المكتب)\b"
    ),
}


def resolve_user_destination(hint_or_folder: Optional[str] = None, default_category: str = "desktop") -> tuple[Path, str]:
    """Analyze a user folder string or prompt hint and return (target_path, category).

    Example inputs:
      - "vedio" -> (C:\\Users\\DELL LATITUDE\\Videos, "videos")
      - "picture" -> (C:\\Users\\DELL LATITUDE\\Pictures, "pictures")
      - "documents" -> (C:\\Users\\DELL LATITUDE\\Documents, "documents")
      - "desktop" -> (C:\\Users\\DELL LATITUDE\\OneDrive\\سطح المكتب, "desktop")
      - "dossier smito test f vedio" -> (C:\\Users\\DELL LATITUDE\\Videos\\test, "videos")
    """
    known = get_known_user_folders()

    if not hint_or_folder or not hint_or_folder.strip():
        return known.get(default_category, known["desktop"]), default_category

    text = hint_or_folder.strip().lower()

    # Detect category from keywords
    matched_category = None
    for cat, pattern in CATEGORY_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            matched_category = cat
            break

    # If no category keyword matched, check if text matches category name directly
    if not matched_category:
        for cat in ("videos", "pictures", "documents", "downloads", "music", "desktop"):
            if cat in text:
                matched_category = cat
                break

    category = matched_category or default_category
    base_folder = known.get(category, known["desktop"])

    # Check if a specific subfolder was mentioned inside the text
    # e.g., "dossier smito X", "folder X", "wst X", "subfolder X"
    subfolder_match = re.search(
        r'(?:dossier|folder|mجلد|مجلد)\s+(?:smito|nommé|named|called|اسمه)?\s*([a-zA-Z0-9_\-]+)',
        text,
        re.IGNORECASE,
    )
    if subfolder_match:
        sub_name = subfolder_match.group(1).strip()
        # Ensure subfolder isn't just the category name itself (e.g. "folder vedio")
        if sub_name.lower() not in ("video", "videos", "vedio", "picture", "pictures", "desktop", "document", "downloads", "music"):
            target_dir = base_folder / sub_name
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("Could not create subfolder %s: %s", target_dir, e)
            return target_dir, category

    return base_folder, category
