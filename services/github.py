"""GitHub website editing service — clone, edit, diff, commit, push."""

import difflib
import os
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser

import db


def _get_config() -> tuple[str, str, str]:
    return (
        db.get_setting("github_repo", ""),
        db.get_setting("github_token", ""),
        db.get_setting("github_branch", "main"),
    )


def _auth_url(repo: str, token: str) -> str:
    if repo.startswith("https://"):
        return repo.replace("https://", f"https://{token}@", 1)
    return f"https://{token}@github.com/{repo.lstrip('/')}.git"


def clone() -> str:
    """Clone the configured website repo to /tmp. Returns the temp dir path."""
    repo, token, branch = _get_config()
    if not repo or not token:
        raise ValueError("GitHub repo not configured. Set github_repo and github_token in portal Settings.")
    tmpdir = tempfile.mkdtemp(prefix="website_")
    url = _auth_url(repo, token)
    result = subprocess.run(
        ["git", "clone", "--depth=1", "--branch", branch, url, tmpdir],
        capture_output=True,
    )
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return tmpdir


def list_files(tmpdir: str) -> str:
    lines = []
    for root, dirs, files in os.walk(tmpdir):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        rel_root = os.path.relpath(root, tmpdir)
        for f in sorted(files):
            lines.append(os.path.join(rel_root, f) if rel_root != "." else f)
    return "\n".join(lines)


def read_file(tmpdir: str, path: str) -> str:
    full = os.path.join(tmpdir, path)
    if not os.path.isfile(full):
        return f"File not found: {path}"
    with open(full, "r", errors="replace") as fh:
        return fh.read()


def write_file(tmpdir: str, path: str, content: str) -> None:
    full = os.path.join(tmpdir, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(content)


def _strip_html(html: str) -> str:
    class _Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "head"):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style", "head"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                text = data.strip()
                if text:
                    self.parts.append(text)

    p = _Parser()
    p.feed(html)
    return "\n".join(p.parts)


def text_diff(old_content: str, new_content: str, path: str = "") -> str:
    is_html = path.lower().endswith((".html", ".htm"))
    old_lines = (_strip_html(old_content) if is_html else old_content).splitlines()
    new_lines = (_strip_html(new_content) if is_html else new_content).splitlines()
    changes = [l for l in difflib.ndiff(old_lines, new_lines) if l.startswith("- ") or l.startswith("+ ")]
    return "\n".join(changes) if changes else "No text changes."


def commit_and_push(tmpdir: str, message: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Website Bot", "GIT_AUTHOR_EMAIL": "bot@sipa.ai",
           "GIT_COMMITTER_NAME": "Website Bot", "GIT_COMMITTER_EMAIL": "bot@sipa.ai"}
    subprocess.run(["git", "add", "-A"], cwd=tmpdir, check=True, capture_output=True, env=env)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=tmpdir, capture_output=True, env=env).returncode == 0:
        return "No changes to commit."
    subprocess.run(["git", "commit", "-m", message], cwd=tmpdir, check=True, capture_output=True, env=env)
    result = subprocess.run(["git", "push"], cwd=tmpdir, capture_output=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return f"Committed and pushed: {message}"


def cleanup(tmpdir: str) -> None:
    shutil.rmtree(tmpdir, ignore_errors=True)
