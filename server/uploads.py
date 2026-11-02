"""Files sent into the chat.

Uploads land in <data dir>/uploads where every agent can open them with
its tools (the path is included in the message context). Images get a
model-written description at upload time; text-like files and PDFs get
a short extracted preview, so agents know what a file is without
reading it first.
"""
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from apphome import ROOT

UPLOAD_DIR = ROOT / "uploads"
MAX_SIZE = 50 * 1024 * 1024
PREVIEW_CHARS = 1500

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".py", ".js",
             ".ts", ".php", ".html", ".css", ".sh", ".sql", ".xml", ".ini", ".toml"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", name).strip() or "file"
    return name[:120]


def save(data: bytes, filename: str) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{safe_name(filename)}"
    dest.write_bytes(data)
    return dest


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
    return "binary"


def preview(path: Path) -> str:
    """A short human/agent-readable peek into the file's content."""
    kind = kind_of(path)
    try:
        if kind == "text":
            return path.read_text(errors="replace")[:PREVIEW_CHARS]
        if kind == "pdf" and shutil.which("pdftotext"):
            r = subprocess.run(["pdftotext", "-l", "3", str(path), "-"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return " ".join(r.stdout.split())[:PREVIEW_CHARS]
    except Exception:
        pass
    return ""
