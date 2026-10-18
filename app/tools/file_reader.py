from pathlib import Path
import os

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
        return {"ok": False, "error": "No target specified."}

    home = Path.home()
    candidates = [
        (home / "OneDrive" / "سطح المكتب" / cleaned).resolve(),
        (home / "OneDrive" / "Desktop" / cleaned).resolve(),
        (home / "Desktop" / cleaned).resolve(),
        (home / "Bureau" / cleaned).resolve(),
        (home / cleaned).resolve(),
        (PROJECT_ROOT / cleaned).resolve(),
        Path(cleaned).expanduser().resolve(),
    ]

    for p in candidates:
        if p.exists():
            from app.tools.security_guard import is_path_safe_for_deletion
            is_safe, reason = is_path_safe_for_deletion(p)
            if not is_safe:
                return {"ok": False, "error": reason}
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                return {"ok": True, "path": str(p), "name": p.name}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"File or folder not found: {cleaned}"}



