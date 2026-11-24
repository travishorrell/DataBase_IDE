"""Storing images and videos: inside the database (BLOB) or as files on disk (path).

BLOB  - the bytes live inside the row. One file to back up and share; but the
        database grows quickly and every read of the row can drag megabytes along.
path  - the file is copied into a media folder and the row stores its path.
        The database stays small and files stream fast; but the folder must be
        backed up and moved together with the database or links break.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dbide.core.config import ConnectionConfig, app_dir

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv"}


def detect_mime(data: bytes) -> str:
    h = data[:16]
    if h.startswith(b"\x89PNG"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if h.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if h.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "video/x-msvideo"
    if h.startswith(b"BM"):
        return "image/bmp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if h.startswith(b"%PDF"):
        return "application/pdf"
    if h.lstrip().startswith(b"<svg") or h.startswith(b"<?xml"):
        return "image/svg+xml"
    return "application/octet-stream"


def kind_from_name(name: str) -> str:
    ext = Path(name).suffix.lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else "file"


def kind_from_mime(mime: str) -> str:
    return mime.split("/")[0] if mime.split("/")[0] in ("image", "video") else "file"


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def base_dir(cfg: ConnectionConfig) -> Path:
    """Relative media paths are resolved against this folder."""
    if cfg.driver == "sqlite":
        return Path(cfg.database).expanduser().resolve().parent
    return app_dir() / "media"


def default_folder(cfg: ConnectionConfig) -> Path:
    if cfg.driver == "sqlite":
        db = Path(cfg.database).expanduser().resolve()
        return db.parent / f"{db.stem}_media"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", cfg.name) or "connection"
    return app_dir() / "media" / safe


def resolve(stored: str, cfg: ConnectionConfig) -> Path:
    p = Path(stored).expanduser()
    return p if p.is_absolute() else (base_dir(cfg) / p)


def store_file(src: str, cfg: ConnectionConfig, folder: str = "") -> str:
    """Copy `src` into the media folder; return the path to save in the row.

    For SQLite the path is relative to the database file, so the database and its
    `<name>_media` folder can be moved together.
    """
    src_path = Path(src)
    root = Path(folder).expanduser() if folder else default_folder(cfg)
    sub = {"image": "images", "video": "videos"}.get(kind_from_name(src_path.name), "files")
    dest_dir = root / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", src_path.stem)[:60] or "file"
    dest = dest_dir / f"{stem}_{uuid.uuid4().hex[:8]}{src_path.suffix.lower()}"
    shutil.copy2(src_path, dest)
    try:
        return dest.relative_to(base_dir(cfg)).as_posix()
    except ValueError:
        return str(dest)


def read_bytes(src: str) -> bytes:
    return Path(src).read_bytes()


@dataclass
class FolderReport:
    folder: Path
    referenced: int = 0  # rows pointing at an existing file in this folder
    missing: list[str] = field(default_factory=list)  # rows pointing at files that don't exist
    orphans: list[str] = field(default_factory=list)  # files on disk no row points at
    total_bytes: int = 0


def folder_reports(values: list[str], cfg: ConnectionConfig, media_root: Path | None = None) -> list[FolderReport]:
    """Group stored paths by the folder they live in and check them against the disk."""
    reports: dict[Path, FolderReport] = {}
    referenced_files: set[Path] = set()
    for v in values:
        if not v:
            continue
        p = resolve(str(v), cfg)
        folder = p.parent
        if media_root is not None:
            try:
                p.relative_to(media_root)
                folder = media_root
            except ValueError:
                pass
        rep = reports.setdefault(folder, FolderReport(folder))
        if p.exists():
            rep.referenced += 1
            rep.total_bytes += p.stat().st_size
            referenced_files.add(p.resolve())
        else:
            rep.missing.append(str(v))
    if media_root is not None and media_root.exists():
        rep = reports.setdefault(media_root, FolderReport(media_root))
        for dirpath, _dirs, files in os.walk(media_root):
            for f in files:
                fp = (Path(dirpath) / f).resolve()
                if fp not in referenced_files and not f.startswith("."):
                    rep.orphans.append(str(fp))
    return sorted(reports.values(), key=lambda r: -r.referenced)
