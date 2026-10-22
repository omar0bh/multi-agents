"""Intelligent intent parser for local PC file and folder operations.
Parses natural language requests in Moroccan Darija, Arabic, English, and French
using Groq LLM (fast, ~300ms) with safe structured JSON output.
NO HARDCODED FILENAMES OR FAKE FALLBACKS!
"""

import json
import logging
import os
import re
import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")


async def parse_file_operation_intent(text: str) -> dict:
    """Analyze user text to determine if it is a local PC file/folder operation.
    
    Returns:
        dict: {
            "is_file_op": bool,
            "action": str,          # copy_file, create_folder, move_file, delete_path, list_dir, run_command
            "src": str,             # source file/folder name or relative path
            "src_parent": str,      # parent folder of source if any
            "dest_folder": str,     # target destination directory
            "dest_name": str,       # new filename if renaming
            "is_folder": bool,      # True if operation targets a folder
            "label_ar": str,        # clear human-readable Arabic description
            "error": str,           # optional error if ambiguous
        }
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {"is_file_op": False}

    # Long messages (>400 chars) are almost never file ops — they are AI/chat requests.
    # Skip the LLM call entirely to avoid json_validate_failed on Groq.
    if len(cleaned) > 400:
        return {"is_file_op": False}

    lowered = cleaned.lower()

    # Quick pre-filter: check if message likely mentions files, folders, PC, or desktop operations
    indicators = (
        "copy", "kopi", "copier", "past", "paste", "coller", "colle",
        "file", "fichier", "folder", "dossier", "dossiers", "files",
        "desktop", "bureau", "documents", "downloads", "pictures", "videos",
        "create folder", "make folder", "mkdir", "dir folder", "sawb dossier",
        "create file", "make file", "dir file", "sawb file", "صاوب ملف", "انشئ ملف",
        "read", "9ra", "lire", "chof chno f", "وريني شنو كاين", "اقرا", "شوف شنو في",
        "move ", "rename ", "déplacer", "renommer",
        "delete ", "supprimer", "mse7", "khwi",
        "list ", "ls ", "شكون كاين", "أظهر الملفات",
        "انسخ", "نسخ", "انقل", "نقل", "احذف", "حذف", "انشئ مجلد", "صاوب مجلد", "دير مجلد",
        # Desktop vision & layout
        "screenshot", "screen", "chof desktop", "chof ldesktop", "chof lscreen",
        "rattab", "ratteb", "rttb", "arrange", "organise", "organiz",
        "wrinh desktop", "wrin l desktop", "aikon", "icons", "aykonat",
        "شوف الشاشة", "رتب الأيقونات", "صورة الشاشة", "أيقونات",
        # System Cleanup & Performance
        "cleanup", "clean cache", "clean up", "performance report", "system cleanup",
        "clean pc", "clean my pc", "clear cache", "cache cleanup", "high-resource",
        "run these scripts", "run the scripts", "run scripts", "execute cleanup",
        "تنظيف", "نظف", "تقرير الأداء", "فحص الأداء", "khwi cache", "n9i pc", "n9i lpc",
    )
    if not any(ind in lowered for ind in indicators):
        return {"is_file_op": False}

    # ── Fast path: system cleanup & performance commands ─────────────────────
    _cleanup_kw = (
        "system cleanup", "performance report", "clean cache", "cache cleanup",
        "clean pc", "clean my pc", "clear cache", "high-resource",
        "run these scripts", "run the scripts", "run scripts", "execute cleanup",
        "تنظيف النظام", "تقرير الأداء", "تنظيف الكاش", "فحص الأداء",
        "نظف البي سي", "نظف الحاسوب", "khwi cache", "n9i pc", "n9i lpc",
    )
    if any(kw in lowered for kw in _cleanup_kw) or (
        ("cleanup" in lowered or "clean" in lowered or "تنظيف" in lowered or "نظف" in lowered)
        and ("pc" in lowered or "cache" in lowered or "system" in lowered or "نظام" in lowered or "كاش" in lowered)
    ):
        return {
            "is_file_op": True,
            "action": "system_cleanup_and_report",
            "src": "", "dest_folder": "", "is_folder": False,
            "label_ar": "تنظيف كاش النظام وفحص الأداء وإنشاء ملفات التقرير (CSV & TXT)",
        }

    # ── Fast path: desktop vision / layout commands (no LLM needed) ──────────
    _desktop_screenshot_kw = (
        "screenshot", "chof desktop", "chof ldesktop", "chof lscreen",
        "wrinh desktop", "wrin l desktop", "شوف الشاشة", "صورة الشاشة",
    )
    _desktop_arrange_kw = (
        "rattab", "ratteb", "rttb", "arrange", "organise", "organiz",
        "رتب", "ترتيب",
    )
    _desktop_icons_kw = (
        "aikon", "icons", "aykonat", "list icons", "أيقونات",
    )
    if any(kw in lowered for kw in _desktop_screenshot_kw):
        return {
            "is_file_op": True,
            "action": "screenshot_analyze",
            "src": "", "dest_folder": "", "is_folder": False,
            "label_ar": "خذ screenshot وحلل الديسكتوب بالذكاء الاصطناعي",
        }
    if any(kw in lowered for kw in _desktop_arrange_kw) and (
        "desktop" in lowered or "icon" in lowered or "aikon" in lowered
        or "rattab" in lowered or "ratteb" in lowered or "arrange" in lowered
    ):
        return {
            "is_file_op": True,
            "action": "arrange_desktop",
            "src": "", "dest_folder": "", "is_folder": False,
            "label_ar": "رتب أيقونات الديسكتوب حسب النوع (تطبيقات، مجلدات، system)",
        }
    if any(kw in lowered for kw in _desktop_icons_kw):
        return {
            "is_file_op": True,
            "action": "get_icons",
            "src": "", "dest_folder": "", "is_folder": False,
            "label_ar": "اعرض قائمة أيقونات الديسكتوب ومواقعها",
        }

    # 1. Try Groq LLM parsing (super fast and accurate for Darija & multi-language)
    if GROQ_API_KEY:
        try:
            prompt = f"""You are a precise intent extractor for a Windows 11 PC file management assistant.
The user speaks Moroccan Darija, English, Arabic, or French.
Analyze the user request and determine the exact local file or folder operation requested.

User instruction:
\"\"\"{cleaned}\"\"\"

Return ONLY a valid JSON object matching this schema:
{{
    "is_pc_operation": true or false,
    "action": "copy_file" | "create_folder" | "create_file" | "read_file" | "move_file" | "delete_path" | "list_dir" | "screenshot_analyze" | "arrange_desktop" | "get_icons" | "none",
    "is_folder": true or false,
    "src": "source file or folder name (e.g. 'new' or 'doc sami/new' or 'report.txt')",
    "src_parent": "parent folder of source if mentioned (e.g. 'doc sami' or 'desktop')",
    "dest_folder": "destination folder (e.g. 'chess' or 'desktop' or 'documents')",
    "dest_name": "new filename or folder name if renaming, otherwise empty string",
    "content": "text content if creating/writing a file, otherwise empty string",
    "label_ar": "short clear description in Arabic of the operation"
}}
IMPORTANT:
- If user wants to copy/paste, action is 'copy_file'.
- If user wants to create a folder/directory, action is 'create_folder'.
- If user wants to create/write a file, action is 'create_file'.
- If user wants to read/view a file, action is 'read_file'.
- If user wants to move or rename, action is 'move_file'.
- If user wants to delete/remove a file or folder, action is 'delete_path'.
- If user wants to list contents of a directory, action is 'list_dir'.
- If user says 'chof desktop', 'screenshot', 'wrinh ldesktop', 'شوف الشاشة': action is 'screenshot_analyze'.
- If user says 'rattab desktop', 'arrange icons', 'رتب الأيقونات': action is 'arrange_desktop'.
- If user says 'list icons', 'aikon', 'wrinh laykonat': action is 'get_icons'.
- NEVER make up or hallucinate filenames that the user did not mention! If no file/folder is named, leave 'src' empty.
"""
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "openai/gpt-oss-120b",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.0,
                        "response_format": {"type": "json_object"},
                    },
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()["choices"][0]["message"]["content"]
                        parsed = json.loads(data)
                    except Exception as parse_exc:
                        logger.warning("Groq response parse error: %s", parse_exc)
                        return {"is_file_op": False}
                    if parsed.get("is_pc_operation") and parsed.get("action") not in ("none", ""):

                        action = parsed.get("action")
                        src = (parsed.get("src") or "").strip()
                        src_parent = (parsed.get("src_parent") or "").strip()
                        dest_folder = (parsed.get("dest_folder") or "desktop").strip()
                        dest_name = (parsed.get("dest_name") or "").strip()
                        content = parsed.get("content") or ""
                        is_folder = bool(parsed.get("is_folder"))
                        label_ar = parsed.get("label_ar") or ""

                        # If copy action but no source was extracted at all
                        if action == "copy_file" and not src:
                            return {
                                "is_file_op": True,
                                "error": "لم أتمكن من معرفة اسم الملف أو المجلد المطلوب نسخه. يرجى توضيح الاسم والمسار."
                            }

                        return {
                            "is_file_op": True,
                            "action": action,
                            "src": src,
                            "src_parent": src_parent,
                            "dest_folder": dest_folder,
                            "dest_name": dest_name,
                            "content": content,
                            "is_folder": is_folder,
                            "label_ar": label_ar,
                        }
        except Exception as exc:
            logger.warning("Groq file intent parsing failed or timed out: %s. Using rule fallback.", exc)

    # 2. Rule-based Fallback (Strict, with ZERO fake file names)
    return _rule_based_fallback(cleaned, lowered)


def _rule_based_fallback(cleaned: str, lowered: str) -> dict:
    """Safe fallback rule parser if LLM is unavailable. Never uses hardcoded filenames."""
    # Quoted strings often contain file/folder names: e.g. "doc sami", "new", "chess"
    quotes = re.findall(r'["\']([^"\']+)["\']', cleaned)
    quotes = [q.strip() for q in quotes if q.strip()]

    # Copy intent
    if any(w in lowered for w in ("copy ", "kopi ", "copier ", "انسخ ", "نسخ ")) or (
        "copy" in lowered and any(k in lowered for k in ("folder", "file", "dossier", "past", "paste", " f ", " to ", " in "))
    ):
        if len(quotes) >= 2:
            # e.g. "doc sami" -> "new" -> "chess"
            if len(quotes) >= 3:
                src_parent = quotes[0]
                src = quotes[1]
                dest_folder = quotes[2]
            else:
                src_parent = ""
                src = quotes[0]
                dest_folder = quotes[1]
            return {
                "is_file_op": True,
                "action": "copy_file",
                "src": src,
                "src_parent": src_parent,
                "dest_folder": dest_folder,
                "dest_name": "",
                "is_folder": "." not in src,
                "label_ar": f"نسخ `{src}` إلى `{dest_folder}`",
            }

        # Look for explicit file with extension
        m_file = re.search(r'\b([A-Za-z0-9_\-]+\.[A-Za-z0-9]{1,5})\b', cleaned)
        if m_file:
            src = m_file.group(1)
            # Find destination
            m_dest = re.search(r'\b(?:past(?:e)?\s+(?:it\s+)?in|into|to|f)\b\s+(?:a\s+)?(?:new\s+)?(?:folder\s+|dossier\s+)?([A-Za-z0-9_\-]+)', cleaned, re.I)
            dest = m_dest.group(1) if m_dest else "desktop"
            return {
                "is_file_op": True,
                "action": "copy_file",
                "src": src,
                "src_parent": "",
                "dest_folder": dest,
                "dest_name": "",
                "is_folder": False,
                "label_ar": f"نسخ الملف `{src}` إلى `{dest}`",
            }

        return {
            "is_file_op": True,
            "error": "لم يتم تحديد اسم الملف أو المجلد المطلوب نسخه بدقة.",
        }

    # Create folder intent
    if any(p in lowered for p in ("create folder", "make folder", "mkdir", "dir folder", "sawb dossier", "انشئ مجلد", "صاوب مجلد")):
        folder_name = quotes[0] if quotes else ""
        if not folder_name:
            m_f = re.search(r'\b(?:folder|dossier|مجلد|mkdir)\s+([A-Za-z0-9_\-]+)', cleaned, re.I)
            if m_f:
                folder_name = m_f.group(1)
        if folder_name and folder_name.lower() not in ("folder", "dossier", "desktop", "new"):
            return {
                "is_file_op": True,
                "action": "create_folder",
                "folder": folder_name,
                "parent": "desktop",
                "label_ar": f"إنشاء المجلد `{folder_name}` على سطح المكتب",
            }

    return {"is_file_op": False}
