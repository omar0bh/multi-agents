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

IMPORTANT: On this machine, folders are stored locally (not OneDrive) after
the user stopped OneDrive backup. Registry is used as the primary source of truth.
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


def _read_registry_shell_folders() -> dict[str, Path]:
    """Read Windows User Shell Folders from registry — the most reliable source."""
    result: dict[str, Path] = {}
    if os.name != "nt":
        return result
    try:
        import winreg
        sub_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        # Registry name → our category
        reg_map = {
            "Desktop":                                    "desktop",
            "Personal":                                   "documents",
            "{F42EE2D3-909F-4907-8871-4C22FC0BF756}":    "documents",
            "My Pictures":                                "pictures",
            "{0DDD015D-B06C-45D5-8C4C-F59713854639}":    "pictures",
            "My Video":                                   "videos",
            "{35286A68-3C57-41A1-BBB1-0EAE73D76C95}":    "videos",
            "My Music":                                   "music",
            "{A0C69A99-21C8-4671-8703-7934162FCF1D}":    "music",
            "{374DE290-123F-4565-9164-39C4925E467B}":    "downloads",
        }
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, i)
                    cat = reg_map.get(name)
                    if cat and cat not in result:
                        expanded = Path(os.path.expandvars(val))
                        if expanded.is_dir():
                            result[cat] = expanded
                    i += 1
                except OSError:
                    break
    except Exception as exc:
        logger.debug("Registry shell folders read error: %s", exc)
    return result


def get_known_user_folders() -> dict[str, Path]:
    """Return verified writable paths for the 6 standard user folders.

    Priority order:
    1. Windows Registry (most reliable — reflects actual shell folder location)
    2. Standard local paths
    3. OneDrive English paths
    4. OneDrive Arabic paths (legacy)
    5. Fallback to home/CategoryName
    """
    home = Path.home()
    onedrive = home / "OneDrive"

    # Step 1: Read from Registry (most reliable)
    reg_folders = _read_registry_shell_folders()

    # Step 2: Fallback candidates per category (ordered: local first, then OneDrive)
    candidates: dict[str, list[Path]] = {
        "desktop": [
            home / "Desktop",
            onedrive / "Desktop",
            onedrive / "\u0633\u0637\u062d \u0627\u0644\u0645\u0643\u062a\u0628",
            home / "\u0633\u0637\u062d \u0627\u0644\u0645\u0643\u062a\u0628",
        ],
        "downloads": [
            home / "Downloads",
            onedrive / "Downloads",
            home / "\u0627\u0644\u062a\u0646\u0632\u064a\u0644\u0627\u062a",
        ],
        "documents": [
            home / "Documents",
            onedrive / "Documents",
            onedrive / "\u0627\u0644\u0645\u0633\u062a\u0646\u062f\u0627\u062a",
            home / "\u0627\u0644\u0645\u0633\u062a\u0646\u062f\u0627\u062a",
        ],
        "pictures": [
            home / "Pictures",
            onedrive / "Pictures",
            onedrive / "\u0627\u0644\u0635\u0648\u0631",
            home / "\u0627\u0644\u0635\u0648\u0631",
        ],
        "videos": [
            home / "Videos",
            onedrive / "Videos",
            onedrive / "\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0641\u064a\u062f\u064a\u0648",
            home / "\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0641\u064a\u062f\u064a\u0648",
        ],
        "music": [
            home / "Music",
            onedrive / "Music",
            onedrive / "\u0627\u0645\u0648\u0633\u064a\u0642\u0649",
            home / "\u0627\u0645\u0648\u0633\u064a\u0642\u0649",
        ],
    }

    resolved: dict[str, Path] = {}
    for cat in candidates:
        # Registry takes priority
        if cat in reg_folders:
            resolved[cat] = reg_folders[cat]
            logger.debug("Folder '%s' resolved from registry: %s", cat, reg_folders[cat])
            continue

        # Fallback: first writable candidate
        found = None
        for cand in candidates[cat]:
            if is_writable(cand):
                found = cand
                break

        # Fallback 2: first existing directory
        if not found:
            for cand in candidates[cat]:
                if cand.is_dir():
                    found = cand
                    break

        resolved[cat] = found or (home / cat.capitalize())
        if found:
            logger.debug("Folder '%s' resolved from filesystem: %s", cat, found)
        else:
            logger.warning("Folder '%s' not found — using synthetic fallback: %s", cat, resolved[cat])

    return resolved


def get_user_folder(category: str) -> Path:
    """Get the path for a specific user folder category."""
    folders = get_known_user_folders()
    cat_clean = category.lower().strip()
    return folders.get(cat_clean, folders.get("desktop", Path.home()))


# Multi-lingual keywords for matching user intentions in Darija, Arabic, French, and English
CATEGORY_PATTERNS = {
    "videos": (
        r"\b(?:video|videos|vedio|vedios|vidéo|vidéos|فيديو|ملفات الفيديو|فيديوهات)\b"
    ),
    "pictures": (
        r"\b(?:picture|pictures|photo|photos|image|images|tswira|tsswira|tasswira|tsawer|pic|pics|صورة|صور|الصور)\b"
    ),
    "documents": (
        r"\b(?:document|documents|doc|docs|fichier|مستند|مستندات|المستندات|ملفات)\b"
    ),
    "downloads": (
        r"\b(?:download|downloads|telechargement|téléchargement|تنزيلات|التنزيلات|داونلود)\b"
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

    Examples:
      - "vedio"       -> (C:\\Users\\DELL LATITUDE\\Videos, "videos")
      - "picture"     -> (C:\\Users\\DELL LATITUDE\\Pictures, "pictures")
      - "documents"   -> (C:\\Users\\DELL LATITUDE\\Documents, "documents")
      - "desktop"     -> (C:\\Users\\DELL LATITUDE\\Desktop, "desktop")
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

    # Direct match on category name
    if not matched_category:
        for cat in ("videos", "pictures", "documents", "downloads", "music", "desktop"):
            if cat in text:
                matched_category = cat
                break

    category = matched_category or default_category
    base_folder = known.get(category, known["desktop"])

    # Check if a specific subfolder was mentioned inside the text
    subfolder_match = re.search(
        r'(?:dossier|folder|مجلد)\s+(?:smito|nommé|named|called|اسمه)?\s*([a-zA-Z0-9_\-]+)',
        text,
        re.IGNORECASE,
    )
    if subfolder_match:
        sub_name = subfolder_match.group(1).strip()
        skip = {"video","videos","vedio","picture","pictures","desktop","document","downloads","music"}
        if sub_name.lower() not in skip:
            target_dir = base_folder / sub_name
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("Could not create subfolder %s: %s", target_dir, e)
            return target_dir, category

    return base_folder, category
