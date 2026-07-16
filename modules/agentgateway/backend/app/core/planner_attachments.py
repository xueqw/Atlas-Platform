"""Attachment storage for the DeerFlow planner.

Attachments are stored through :mod:`app.core.object_storage` under the
``planner-attachments`` logical bucket, key ``{conversation_id}/attachments/
<sha1-prefix>-<safe-name>``. With MinIO disabled the disk backend maps that key
under ``data/object-store/...`` — equivalent to the previous per-session layout
and swept by the same delete path. With MinIO enabled attachments persist as
objects across instances. The traversal guard mirrors planner_files so both
modules reject the same malicious ids/filenames.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Optional

from app.core import object_storage

# Allowed mime → file kind. Images go to the vision pipeline; text/markdown is
# inlined into the user turn.
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
TEXT_MIMES = {"text/plain", "text/markdown"}
ALLOWED_MIMES = IMAGE_MIMES | TEXT_MIMES

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB per file (D7)

_BUCKET = object_storage.BUCKET_ATTACHMENTS


def _validate_cid(conversation_id: str) -> str:
    if not conversation_id or "/" in conversation_id or ".." in conversation_id:
        raise ValueError(f"invalid conversation_id: {conversation_id!r}")
    return conversation_id


def _object_key(conversation_id: str, stored_name: str) -> str:
    """Object key for a stored attachment (validated id + name)."""
    _validate_cid(conversation_id)
    if not stored_name or "/" in stored_name or ".." in stored_name:
        raise ValueError(f"invalid filename: {stored_name!r}")
    return f"{conversation_id}/attachments/{stored_name}"


def _safe_name(filename: str) -> str:
    """Strip directory components and dangerous chars from a client filename."""
    from pathlib import Path

    base = Path(filename or "").name  # drop any path component
    base = base.replace("..", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base or "file"


def kind_for_mime(mime: str) -> str:
    return "image" if mime in IMAGE_MIMES else "text"


def attachments_prefix(conversation_id: str) -> str:
    """Object-key prefix for all of a session's attachments (delete sweep)."""
    return f"{_validate_cid(conversation_id)}/attachments/"


def save_attachment(conversation_id: str, filename: str, data: bytes, mime: str) -> dict:
    """Store ``data`` for a session and return metadata.

    The stored name is ``<sha1[:12]>-<safe-name>`` so identical re-uploads
    collapse and arbitrary client names can't escape the key space.
    """
    safe = _safe_name(filename)
    digest = hashlib.sha1(data).hexdigest()[:12]
    stored = f"{digest}-{safe}"
    key = _object_key(conversation_id, stored)
    object_storage.put_object(_BUCKET, key, data, content_type=mime)
    return {
        "path": key,
        "kind": kind_for_mime(mime),
        "mime": mime,
        "name": safe,
        "size": len(data),
        "stored_name": stored,
        "preview_url": f"/api/planner/sessions/{conversation_id}/attachments/{stored}",
    }


def read_attachment_bytes(conversation_id: str, stored_name: str) -> Optional[bytes]:
    """Read a stored attachment's raw bytes, or None if missing."""
    key = _object_key(conversation_id, stored_name)
    return object_storage.get_object(_BUCKET, key)


def read_attachment_text(conversation_id: str, stored_name: str) -> Optional[str]:
    """Read a text/markdown attachment as UTF-8, or None if missing."""
    data = read_attachment_bytes(conversation_id, stored_name)
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


def attachment_data_url(conversation_id: str, stored_name: str, mime: str) -> Optional[str]:
    """Build a base64 ``data:`` URL for an image attachment, or None if missing."""
    data = read_attachment_bytes(conversation_id, stored_name)
    if data is None:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def delete_session_attachments(conversation_id: str) -> None:
    """Remove all attachment objects for a session (delete sweep)."""
    object_storage.delete_prefix(_BUCKET, attachments_prefix(conversation_id))

