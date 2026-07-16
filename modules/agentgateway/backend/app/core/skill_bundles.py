"""Skill bundle upload, extraction, parsing, and registration (change:
add-skill-bundle-upload).

A skill is a ``CapabilityItem(type="skill")`` row — the single source of truth
consumed by ``/api/planner/skills``, ``/api/capabilities`` and the planner prompt
injection. This module adds an upload path: a ``.zip`` is safely unpacked and its
members are archived **as an extracted file tree** into object storage under the
``skill-bundles`` bucket at key prefix ``<slug>/v<version>/<member>``, so runtime
can fetch any single file by path. ``SKILL.md`` YAML frontmatter (``name`` /
``description``) is parsed and upserted onto the capability row; bundle locator
metadata (prefix, version, sha256, file_count, …) rides on the existing
``config`` JSON column — no new table, no migration.

Design constraints (design.md):
  - D1: CapabilityItem stays the source of truth; config holds only small scalars
    (no full file list, no body) — the file list is listed on demand via
    ``object_storage.list_prefix``.
  - D2: store the extracted tree (not the raw zip), versioned by prefix.
  - D3: parsing is pure + defended (zip-slip / zip-bomb); IO/orchestration split.
  - D5: same-name upload bumps a monotonic ``version`` and keeps only the latest
    (active) tree — old version prefix is swept after a successful write.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from app.core import object_storage
from app.core import database as _db
from app.models.db import CapabilityItem

_BUCKET = object_storage.BUCKET_SKILL_BUNDLES

# zip-bomb / oversize guards (design D3).
MAX_ZIP_BYTES = 10 * 1024 * 1024          # 10 MiB compressed upload ceiling
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # 50 MiB total inflated
MAX_FILE_BYTES = 20 * 1024 * 1024          # 20 MiB per single member inflated
MAX_ENTRIES = 2000                         # member count ceiling

# CapabilityItem column widths (db.py).
_NAME_MAX = 200
_DESC_MAX = 2000


class SkillBundleError(ValueError):
    """Raised for any invalid/malicious bundle; mapped to HTTP 400 by the route."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(name: str) -> str:
    """Filesystem/key-safe slug for a skill name. Appends a short hash of the
    original name whenever any unsafe (e.g. CJK) chars were stripped, so distinct
    names never collide to the same prefix even when their ASCII remainder ties."""
    import hashlib

    stripped = re.sub(r"[^A-Za-z0-9._-]", "-", name.strip())
    stripped = re.sub(r"-{2,}", "-", stripped).strip("-.")
    lossy = stripped != name.strip()
    if not stripped:
        return "skill-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    base = stripped[:100]
    if lossy:
        base = f"{base}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"
    return base[:120]


def _skill_root(slug: str) -> str:
    """Object-key prefix covering ALL versions of a skill (delete sweep)."""
    return f"{slug}/"


def _version_prefix(slug: str, version: int) -> str:
    """Object-key prefix for one version's extracted tree."""
    return f"{slug}/v{version}/"


def _is_safe_relpath(path: str) -> bool:
    """Reject zip-slip: absolute paths, ``..`` traversal, drive letters."""
    if not path or path.startswith("/") or path.startswith("\\"):
        return False
    if ".." in path.replace("\\", "/").split("/"):
        return False
    if re.match(r"^[A-Za-z]:", path):  # windows drive
        return False
    norm = posixpath.normpath(path)
    if norm.startswith("..") or norm.startswith("/"):
        return False
    return True


def _parse_frontmatter(text: str) -> dict:
    """Parse leading ``---\\n…\\n---`` YAML frontmatter; prefer yaml, fall back to
    a restricted ``key: value`` scanner so the path works without pyyaml."""
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    block = "\n".join(lines[1:end])
    try:
        import yaml

        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        out: dict = {}
        for ln in block.splitlines():
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if ":" not in ln or ln[0] in (" ", "\t"):
                continue
            k, _, v = ln.partition(":")
            out[k.strip()] = v.strip().strip("'\"")
        return out


def _locate_skill_md(members: List[str]) -> str:
    """Pick the single shallowest ``SKILL.md`` (case-sensitive). Raise on 0 or >1."""
    candidates = [m for m in members if posixpath.basename(m) == "SKILL.md"]
    if not candidates:
        raise SkillBundleError("技能包内未找到 SKILL.md")
    candidates.sort(key=lambda m: (m.count("/"), m))
    min_depth = candidates[0].count("/")
    shallow = [m for m in candidates if m.count("/") == min_depth]
    if len(shallow) > 1:
        raise SkillBundleError(
            f"技能包内存在多个同层级 SKILL.md（{len(shallow)} 个），请拆分为单技能包"
        )
    return shallow[0]


def parse_bundle(data: bytes) -> dict:
    """Validate + parse a ``.zip`` in memory. No disk writes here (design D3).

    Returns ``{name, description, entrypoint, members:[(relpath, bytes)], skill_md_text}``
    """
    if len(data) > MAX_ZIP_BYTES:
        raise SkillBundleError(f"技能包超过大小上限（{MAX_ZIP_BYTES} 字节）")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillBundleError(f"无效的 zip 文件：{exc}") from exc

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ENTRIES:
        raise SkillBundleError(f"技能包条目数超过上限（{MAX_ENTRIES}）")

    total = 0
    raw: List[Tuple[str, bytes]] = []
    for info in infos:
        name = info.filename
        if not _is_safe_relpath(name):
            raise SkillBundleError(f"技能包含非法路径条目（zip-slip）：{name!r}")
        if info.file_size > MAX_FILE_BYTES:
            raise SkillBundleError(f"技能包内单文件超过上限：{name!r}")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise SkillBundleError("技能包解压总大小超过上限（疑似解压炸弹）")
        with zf.open(info) as fh:
            content = fh.read()
        if len(content) > MAX_FILE_BYTES:
            raise SkillBundleError(f"技能包内单文件超过上限：{name!r}")
        raw.append((name.replace("\\", "/"), content))

    skill_md = _locate_skill_md([p for p, _ in raw])
    base_dir = posixpath.dirname(skill_md)

    members: List[Tuple[str, bytes]] = []
    skill_md_bytes = b""
    for path, content in raw:
        if base_dir:
            if path == base_dir or not path.startswith(base_dir + "/"):
                continue
            rel = path[len(base_dir) + 1:]
        else:
            rel = path
        if not rel:
            continue
        members.append((rel, content))
        if rel == "SKILL.md":
            skill_md_bytes = content

    skill_md_text = skill_md_bytes.decode("utf-8", errors="replace")
    fm = _parse_frontmatter(skill_md_text)
    name = str(fm.get("name") or "").strip()
    if not name:
        raise SkillBundleError("SKILL.md frontmatter 缺少 name")
    description = str(fm.get("description") or "").strip()

    return {
        "name": name[:_NAME_MAX],
        "description": description[:_DESC_MAX],
        "entrypoint": "SKILL.md",
        "members": members,
        "skill_md_text": skill_md_text,
    }


# ── Registry helpers ─────────────────────────────────────────────────────────

def _find_skill(session: Session, name: str) -> Optional[CapabilityItem]:
    return session.exec(
        select(CapabilityItem).where(
            CapabilityItem.type == "skill", CapabilityItem.name == name
        )
    ).first()


def _load_config(row: CapabilityItem) -> dict:
    try:
        cfg = json.loads(row.config or "{}")
        return cfg if isinstance(cfg, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def store_and_register(data: bytes, original_filename: str = "") -> dict:
    """Parse → write the extracted tree under a new version prefix → upsert the
    CapabilityItem → sweep the superseded version."""
    import hashlib

    parsed = parse_bundle(data)
    name = parsed["name"]
    slug = _slug(name)
    sha256 = hashlib.sha256(data).hexdigest()

    with Session(_db.engine) as session:
        existing = _find_skill(session, name)
        old_cfg = _load_config(existing) if existing else {}
        prev_version = int(old_cfg.get("version") or 0)
        version = prev_version + 1
        old_prefix = (old_cfg.get("bundle") or {}).get("prefix") if existing else None

        prefix = _version_prefix(slug, version)

        try:
            for rel, content in parsed["members"]:
                ok = object_storage.put_object(_BUCKET, prefix + rel, content)
                if not ok:
                    raise SkillBundleError(f"写入对象存储失败：{rel}")
        except Exception:
            object_storage.delete_prefix(_BUCKET, prefix)
            raise

        bundle_meta = {
            "prefix": prefix,
            "original_filename": original_filename or "",
            "size": len(data),
            "sha256": sha256,
            "file_count": len(parsed["members"]),
            "uploaded_at": _utcnow_iso(),
        }
        config = {
            "source": "uploaded",
            "version": version,
            "entrypoint": parsed["entrypoint"],
            "bundle": bundle_meta,
        }
        cfg_json = json.dumps(config, ensure_ascii=False)

        if existing:
            existing.description = parsed["description"]
            existing.config = cfg_json
            existing.updated_at = datetime.now(timezone.utc)
            session.add(existing)
        else:
            session.add(CapabilityItem(
                type="skill",
                name=name,
                description=parsed["description"],
                tags="[]",
                config=cfg_json,
            ))
        session.commit()

    # Sweep the superseded version tree.
    if old_prefix and old_prefix != prefix:
        object_storage.delete_prefix(_BUCKET, old_prefix)

    return {
        "name": name,
        "description": parsed["description"],
        "version": version,
        "file_count": len(parsed["members"]),
        "source": "uploaded",
    }


# ── On-demand read family (design D3) ────────────────────────────────────────

def _bundle_prefix_for(name: str) -> Optional[str]:
    """The active version prefix for a skill, or None if it has no bundle."""
    with Session(_db.engine) as session:
        row = _find_skill(session, name)
        if row is None:
            return None
        cfg = _load_config(row)
        return (cfg.get("bundle") or {}).get("prefix") or None


def read_skill_md(name: str) -> Optional[str]:
    """Return the SKILL.md body for a skill, or None if it has no bundle."""
    prefix = _bundle_prefix_for(name)
    if not prefix:
        return None
    data = object_storage.get_object(_BUCKET, prefix + "SKILL.md")
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


def list_skill_files(name: str) -> Optional[List[str]]:
    """List member relative paths in the skill's active tree."""
    prefix = _bundle_prefix_for(name)
    if not prefix:
        return None
    return object_storage.list_prefix(_BUCKET, prefix)


def read_skill_file(name: str, relpath: str) -> Optional[bytes]:
    """Return one member's bytes by relative path (prefix-guarded)."""
    prefix = _bundle_prefix_for(name)
    if not prefix:
        return None
    rel = (relpath or "").replace("\\", "/").lstrip("/")
    if not _is_safe_relpath(rel):
        raise SkillBundleError(f"非法成员路径：{relpath!r}")
    return object_storage.get_object(_BUCKET, prefix + rel)


def delete_skill_tree(config: dict) -> None:
    """Delete ALL versions of a skill's archived tree by its root prefix."""
    prefix = (config.get("bundle") or {}).get("prefix") if isinstance(config, dict) else None
    if not prefix:
        return
    slug = prefix.split("/", 1)[0]
    if slug:
        object_storage.delete_prefix(_BUCKET, _skill_root(slug))
