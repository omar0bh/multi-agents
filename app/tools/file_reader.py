from pathlib import Path
import os
from app.tools.project_scaffold import get_desktop_dir

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def read_text_file(path: str, max_chars: int = 20000) -> str:
    file_path = Path(path).expanduser().resolve()

    if not file_path.is_file():
        raise FileNotFoundError(str(file_path))

    return file_path.read_text(
        encoding="utf-8",
        errors="replace",
    )[:max_chars]


def find_file(target: str) -> Path | None:
    """Find a file by exact path, relative to repo root, or by searching project files."""
    cleaned = target.strip().strip("'\"`")
    if not cleaned:
        return None

    # Check direct path (absolute or relative to current cwd)
    direct = Path(cleaned).expanduser()
    if direct.is_file():
        return direct.resolve()

    # Check relative to PROJECT_ROOT
    in_repo = (PROJECT_ROOT / cleaned).resolve()
    if in_repo.is_file():
        return in_repo

    # Check User home directory (e.g. Desktop, Downloads, OneDrive Desktop)
    home = Path.home()
    for sub in ("", "OneDrive/سطح المكتب", "OneDrive/Desktop", "Desktop", "Bureau", "Downloads", "Documents"):
        candidate = (home / sub / cleaned).resolve()
        if candidate.is_file():
            return candidate

    # Search project tree for exact name match (shallow first)
    target_name = Path(cleaned).name.lower()
    for root, _, files in os.walk(PROJECT_ROOT):
        # Skip .git and .venv
        if ".git" in root or ".venv" in root or "__pycache__" in root:
            continue
        for f in files:
            if f.lower() == target_name:
                return (Path(root) / f).resolve()

    return None


def list_directory(folder: str = ".") -> list[str]:
    p = Path(folder).expanduser().resolve()
    if not p.exists():
        p = (PROJECT_ROOT / folder).resolve()
    if not p.is_dir():
        return []
    items = []
    for item in p.iterdir():
        if item.name.startswith((".", "__")):
            continue
        prefix = "📁" if item.is_dir() else "📄"
        items.append(f"{prefix} {item.name}")
    return items[:50]


def delete_path(target: str) -> dict:
    """Safely delete a file or directory located on Desktop, Home, or project root."""
    import shutil
    cleaned = target.strip().strip("'\"`")
    if not cleaned:
        return {"ok": False, "error": "لم يتم تحديد اسم الملف أو المجلد المطلوب حذفه."}

    from app.tools.user_folders import get_known_user_folders
    known = get_known_user_folders()
    desktop = known.get("desktop", Path.home() / "Desktop")

    candidates = [
        (desktop / cleaned).resolve(),
        Path(cleaned).expanduser().resolve(),
    ]
    for k_dir in known.values():
        candidates.append((k_dir / cleaned).resolve())
    candidates.extend([
        (Path.home() / cleaned).resolve(),
        (PROJECT_ROOT / cleaned).resolve(),
    ])

    # Search in Desktop if not found directly
    target_path = None
    for p in candidates:
        if p.exists():
            target_path = p
            break

    if not target_path:
        # Shallow walk in Desktop to locate by name
        target_name = Path(cleaned).name.lower()
        for root, dirs, files in os.walk(desktop):
            rel = Path(root).relative_to(desktop)
            if len(rel.parts) > 2:
                dirs.clear()
                continue
            for d in dirs:
                if d.lower() == target_name:
                    target_path = (Path(root) / d).resolve()
                    break
            if target_path:
                break
            for f in files:
                if f.lower() == target_name:
                    target_path = (Path(root) / f).resolve()
                    break
            if target_path:
                break

    if target_path and target_path.exists():
        from app.tools.security_guard import is_path_safe_for_deletion
        is_safe, reason = is_path_safe_for_deletion(target_path)
        if not is_safe:
            return {"ok": False, "error": reason}
        try:
            import stat
            import subprocess
            is_dir = target_path.is_dir()
            if is_dir:
                def _handle_readonly(func, path, exc_info):
                    try:
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    except Exception:
                        pass
                shutil.rmtree(target_path, onerror=_handle_readonly)
                if target_path.exists():
                    # Fallback via PowerShell force remove
                    subprocess.run(["powershell", "-NoProfile", "-Command", f'Remove-Item -LiteralPath "{target_path}" -Recurse -Force'], capture_output=True)
                item_type = "المجلد"
            else:
                try:
                    target_path.unlink()
                except PermissionError:
                    os.chmod(str(target_path), stat.S_IWRITE)
                    target_path.unlink()
                item_type = "الملف"

            return {
                "ok": True,
                "path": str(target_path),
                "name": target_path.name,
                "detail": f"تم حذف {item_type} `{target_path.name}` بنجاح من حاسوبك الشخصي.\n📁 المسار المحذوف: `{target_path}`",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"تعذر العثور على `{cleaned}` على حاسوبك الشخصي لحذفه."}




