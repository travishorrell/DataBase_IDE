"""Column roles: extra meaning the IDE attaches to a column.

  password   values are hashed before they are written (bcrypt, Argon2id, ...)
  encrypted  values are encrypted with this connection's field key
  media      the column holds an image/video, either inside the row (BLOB) or
             as a path to a file in a media folder

Roles live in ~/.dbide/column_roles.json, keyed by connection, so the database
itself stays a plain database that any other tool can read.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from dbide.core import crypto
from dbide.core.config import app_dir


@dataclass
class ColumnRole:
    kind: str  # password | encrypted | media
    algo: str = ""  # hash or cipher key from crypto.ALGORITHMS
    storage: str = ""  # media: blob | path
    media_kind: str = "any"  # image | video | any
    folder: str = ""  # media path storage: folder override (else the default folder)

    def label(self) -> str:
        if self.kind == "password":
            return f"🔑 Password hash ({crypto.ALGORITHMS[self.algo].label})"
        if self.kind == "encrypted":
            return f"🔒 Encrypted ({crypto.ALGORITHMS[self.algo].label})"
        if self.kind == "media":
            what = {"image": "Image", "video": "Video"}.get(self.media_kind, "Media")
            return f"🖼 {what} in database (BLOB)" if self.storage == "blob" else f"📁 {what} file path"
        return ""

    def short(self) -> str:
        """Compact tag for diagrams: 'password', 'encrypted', 'image · db', 'video · file'."""
        if self.kind == "media":
            return f"{self.media_kind if self.media_kind != 'any' else 'file'} · {'db' if self.storage == 'blob' else 'file'}"
        return self.kind

    def code(self) -> str:
        """Stable string used in the designer's combo box."""
        if self.kind == "media":
            return f"media:{self.storage}:{self.media_kind}"
        return f"{self.kind}:{self.algo}"

    @classmethod
    def from_code(cls, code: str) -> "ColumnRole | None":
        if not code:
            return None
        parts = code.split(":")
        if parts[0] == "media":
            return cls("media", storage=parts[1], media_kind=parts[2] if len(parts) > 2 else "any")
        return cls(parts[0], algo=parts[1])


def role_choices() -> list[tuple[str, str]]:
    """(label, code) pairs for the designer, '' meaning a plain column."""
    out = [("(plain column)", "")]
    for a in crypto.HASH_ALGORITHMS:
        r = ColumnRole("password", a.key)
        out.append((r.label(), r.code()))
    for a in crypto.ENCRYPT_ALGORITHMS:
        r = ColumnRole("encrypted", a.key)
        out.append((r.label(), r.code()))
    for storage in ("blob", "path"):
        for mk in ("image", "video", "any"):
            r = ColumnRole("media", storage=storage, media_kind=mk)
            out.append((r.label(), r.code()))
    return out


# "Special" data types offered in every Type dropdown. Each is a real SQL type plus a role.
SMART_TYPES: list[tuple[str, str]] = [
    ("🔑 Password (hashed)", "password:argon2id"),
    ("🔒 Encrypted text", "encrypted:aes256gcm"),
    ("🖼 Image (stored in database)", "media:blob:image"),
    ("📁 Image (file on disk)", "media:path:image"),
    ("🎬 Video (file on disk)", "media:path:video"),
    ("🎬 Video (stored in database)", "media:blob:video"),
    ("📎 File (stored in database)", "media:blob:any"),
    ("📁 File (file on disk)", "media:path:any"),
]
SMART_BY_LABEL = {label: code for label, code in SMART_TYPES}
SMART_BY_CODE = {code: label for label, code in SMART_TYPES}


def smart_label(role: "ColumnRole | None") -> str | None:
    """The dropdown label for a role, if it is one of the special types."""
    if role is None:
        return None
    code = role.code()
    if code in SMART_BY_CODE:
        return SMART_BY_CODE[code]
    if role.kind == "password":
        return SMART_TYPES[0][0]
    if role.kind == "encrypted":
        return SMART_TYPES[1][0]
    return None


def from_smart(label: str) -> "ColumnRole | None":
    code = SMART_BY_LABEL.get(label.strip())
    return ColumnRole.from_code(code) if code else None


def type_choices(common: list[str]) -> list[str]:
    """SQL types, then the special types (a '' entry marks the separator)."""
    return list(common) + [""] + [label for label, _ in SMART_TYPES]


def suggested_type(role: ColumnRole, driver_key: str) -> str:
    blob = {"mysql": "LONGBLOB", "postgresql": "BYTEA", "mssql": "VARBINARY(MAX)"}.get(driver_key, "BLOB")
    long_text = {"mysql": "VARCHAR(1024)", "mssql": "NVARCHAR(1024)"}.get(driver_key, "TEXT")
    short_text = {"mysql": "VARCHAR(255)", "mssql": "NVARCHAR(255)"}.get(driver_key, "TEXT")
    if role.kind == "media" and role.storage == "blob":
        return blob
    if role.kind == "media":
        return long_text
    if role.kind == "password":
        return short_text
    return {"mssql": "NVARCHAR(MAX)"}.get(driver_key, "TEXT")


_MEDIA_NAME = re.compile(r"(image|img|photo|picture|avatar|thumb|logo|video|movie|clip|media|file|attachment|cover)", re.I)
_PATH_NAME = re.compile(r"(_path|_url|_file|filename|path)$", re.I)


def guess_media(column: str, type_str: str) -> str | None:
    """'blob' / 'path' if a column without a role looks like it stores media."""
    t = type_str.upper()
    if "BLOB" in t or "BINARY" in t or t == "BYTEA":
        return "blob" if _MEDIA_NAME.search(column) or "BLOB" in t else None
    if _MEDIA_NAME.search(column) and _PATH_NAME.search(column):
        return "path"
    return None


def looks_like_password(column: str) -> bool:
    return bool(re.search(r"(^|_)(pass(word)?|pwd|passwd)(_|$)", column, re.I))


class RoleStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "column_roles.json"
        try:
            self.data: dict = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self.data = {}

    @staticmethod
    def _tkey(schema: str | None, table: str) -> str:
        return f"{schema or ''}|{table}"

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    def get(self, conn_id: str, schema: str | None, table: str) -> dict[str, ColumnRole]:
        raw = self.data.get(conn_id, {}).get(self._tkey(schema, table), {})
        return {c: ColumnRole(**r) for c, r in raw.items()}

    def all_tables(self, conn_id: str, schema: str | None) -> dict[str, dict[str, ColumnRole]]:
        out = {}
        for tkey, cols in self.data.get(conn_id, {}).items():
            s, t = tkey.split("|", 1)
            if (s or None) == schema:
                out[t] = {c: ColumnRole(**r) for c, r in cols.items()}
        return out

    def set_table(self, conn_id: str, schema: str | None, table: str, roles: dict[str, ColumnRole]) -> None:
        conn = self.data.setdefault(conn_id, {})
        if roles:
            conn[self._tkey(schema, table)] = {c: asdict(r) for c, r in roles.items()}
        else:
            conn.pop(self._tkey(schema, table), None)
        self._save()

    def drop_table(self, conn_id: str, schema: str | None, table: str) -> None:
        self.data.get(conn_id, {}).pop(self._tkey(schema, table), None)
        self._save()


_roles: RoleStore | None = None


def role_store() -> RoleStore:
    global _roles
    if _roles is None:
        _roles = RoleStore()
    return _roles


def set_role_store(r: RoleStore | None) -> None:
    global _roles
    _roles = r


# ---- applying roles to values being written -------------------------------------


def prepare_value(role: ColumnRole | None, value, conn_id: str):
    """Transform a value on its way into the database according to the column's role."""
    if role is None or value is None or value == "":
        return value
    if role.kind == "password":
        if isinstance(value, str) and crypto.detect_hash(value):
            return value  # already a hash: don't hash it twice
        return crypto.hash_password(str(value), role.algo)
    if role.kind == "encrypted":
        if crypto.is_encrypted(value):
            return value
        from dbide.core.secrets import field_key

        return crypto.encrypt_value(str(value), field_key(conn_id, create=True), role.algo)
    return value


def prepare_row(roles: dict[str, ColumnRole], values: dict, conn_id: str) -> dict:
    return {c: prepare_value(roles.get(c), v, conn_id) for c, v in values.items()}
