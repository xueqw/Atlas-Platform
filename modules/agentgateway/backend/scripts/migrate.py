"""Thin wrapper around alembic so contributors don't have to remember to cd
into ``backend/`` or pass ``-c`` every time. All commands run with backend/
as cwd. Usage::

    python -m scripts.migrate upgrade            # upgrade to head
    python -m scripts.migrate upgrade <rev>      # upgrade to specific rev
    python -m scripts.migrate stamp <rev>        # mark a rev as applied without DDL
    python -m scripts.migrate revision -m "..."  # generate new migration (autogenerate)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _backend_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _alembic_config():
    from alembic.config import Config

    cfg_path = _backend_dir() / "alembic.ini"
    if not cfg_path.exists():
        raise SystemExit(f"alembic.ini not found at {cfg_path}")
    return Config(str(cfg_path))


def cmd_upgrade(rev: str) -> None:
    from alembic import command

    command.upgrade(_alembic_config(), rev)


def cmd_stamp(rev: str) -> None:
    from alembic import command

    command.stamp(_alembic_config(), rev)


def cmd_revision(message: str, autogenerate: bool, rev_id: str | None) -> None:
    from alembic import command

    command.revision(
        _alembic_config(),
        message=message,
        autogenerate=autogenerate,
        rev_id=rev_id,
    )


def main(argv: list[str] | None = None) -> int:
    os.chdir(_backend_dir())

    parser = argparse.ArgumentParser(prog="scripts.migrate")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_up = sub.add_parser("upgrade", help="alembic upgrade <rev>")
    p_up.add_argument("rev", nargs="?", default="head")

    p_st = sub.add_parser("stamp", help="alembic stamp <rev>")
    p_st.add_argument("rev")

    p_rev = sub.add_parser("revision", help="alembic revision (autogenerate by default)")
    p_rev.add_argument("-m", "--message", required=True)
    p_rev.add_argument("--no-autogenerate", action="store_true")
    p_rev.add_argument("--rev-id", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "upgrade":
        cmd_upgrade(args.rev)
    elif args.cmd == "stamp":
        cmd_stamp(args.rev)
    elif args.cmd == "revision":
        cmd_revision(args.message, autogenerate=not args.no_autogenerate, rev_id=args.rev_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
