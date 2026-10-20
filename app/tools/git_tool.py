import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_git(args: list[str]) -> dict:
    """Run a git command in the repository root."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


def git_status() -> str:
    res = run_git(["status", "--short"])
    return res["stdout"] or "Working tree clean."


def git_diff() -> str:
    res = run_git(["diff"])
    return res["stdout"][:4000] or "No uncommitted changes."


def git_commit_and_push(message: str, branch: str = "main") -> dict:
    """Stage changes, commit, and push to remote."""
    add_res = run_git(["add", "."])
    if not add_res["ok"]:
        return {"ok": False, "step": "git add", "detail": add_res["stderr"]}

    commit_res = run_git(["commit", "-m", message])
    # returncode 1 might mean nothing to commit
    if not commit_res["ok"] and "nothing to commit" not in commit_res["stdout"].lower():
        return {"ok": False, "step": "git commit", "detail": commit_res["stderr"]}

    push_res = run_git(["push", "origin", branch])
    return {
        "ok": push_res["ok"],
        "step": "git push",
        "detail": push_res["stdout"] or push_res["stderr"],
    }


def git_pull(branch: str = "main") -> dict:
    """Pull latest changes from remote origin."""
    pull_res = run_git(["pull", "origin", branch])
    return {
        "ok": pull_res["ok"],
        "step": "git pull",
        "detail": pull_res["stdout"] or pull_res["stderr"],
    }
