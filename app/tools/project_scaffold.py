from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def get_desktop_dir() -> Path:
    """Find the user's actual Desktop directory across English, Arabic, and French Windows with OneDrive."""
    try:
        from app.tools.user_folders import get_known_user_folders
        known = get_known_user_folders()
        if "desktop" in known and known["desktop"].is_dir():
            return known["desktop"]
    except Exception:
        pass

    home = Path.home()
    for candidate in (
        home / "Desktop",
        home / "سطح المكتب",
        home / "Bureau",
        home / "OneDrive" / "Desktop",
        home / "OneDrive" / "سطح المكتب",
        home / "OneDrive" / "Bureau",
    ):
        if candidate.is_dir():
            return candidate
    return home


def create_project_on_desktop(
    project_name: str,
    files: dict[str, str] | None = None,
    target_location: str | None = None,
) -> dict:
    """Create a project folder in the specified user folder (Desktop, Documents, Videos, Pictures, etc.)."""
    from app.tools.user_folders import resolve_user_destination
    base_dir, category = resolve_user_destination(target_location, default_category="desktop")
    target_dir = base_dir / project_name
    try:
        target_dir.mkdir(parents=True, exist_ok=True)

        created_files = []
        if files:
            for rel_path, content in files.items():
                file_path = target_dir / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
                created_files.append(rel_path)
        else:
            # Default HTML/CSS scaffold if no files specified
            index_html = target_dir / "index.html"
            index_html.write_text(
                f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{project_name}</title>
    <link rel="stylesheet" href="style.css">
</head>
<body>
    <div class="container">
        <h1>Welcome to {project_name}</h1>
        <p>Created by your AI Employee on Telegram.</p>
    </div>
    <script src="script.js"></script>
</body>
</html>""",
                encoding="utf-8",
            )
            style_css = target_dir / "style.css"
            style_css.write_text(
                """* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: system-ui, -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #0f172a; color: #f8fafc; }
.container { text-align: center; padding: 2rem; border-radius: 12px; background: #1e293b; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
h1 { margin-bottom: 1rem; color: #38bdf8; }
p { color: #94a3b8; }""",
                encoding="utf-8",
            )
            script_js = target_dir / "script.js"
            script_js.write_text('console.log("Project initialized successfully.");', encoding="utf-8")
            created_files = ["index.html", "style.css", "script.js"]

        return {
            "ok": True,
            "path": str(target_dir),
            "files": created_files,
        }
    except Exception as exc:
        logger.exception("Failed to create project %s: %s", project_name, exc)
        return {
            "ok": False,
            "error": str(exc),
        }
