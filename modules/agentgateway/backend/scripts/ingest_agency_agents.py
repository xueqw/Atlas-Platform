"""Import the agency-agents external expert-asset repo (T10).

Fetches markdown from Gitee → stores raw in object storage → parses/classifies →
upserts structured expert_template capabilities (with provenance). Re-runnable:
idempotent on (type, name). Run from the backend directory::

    python -m scripts.ingest_agency_agents                 # default repo/branch
    python -m scripts.ingest_agency_agents --version v1     # tag this batch

This is a MANUAL / on-demand entry point — it is intentionally NOT wired into app
startup (lifespan), so the backend never makes an outbound Gitee fetch on boot.
Requires network access; set ENABLE_MINIO=true + S3_* to store in real MinIO,
otherwise raw md lands in the local-disk object-store fallback.
"""

from __future__ import annotations

import argparse

from app.core.asset_ingestion import run_ingestion, DEFAULT_SOURCE_REPO


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingest agency-agents into expert_template index")
    p.add_argument("--repo", default=DEFAULT_SOURCE_REPO)
    p.add_argument("--version", default="", help="label for this sync batch (default: timestamp)")
    args = p.parse_args(argv)

    result = run_ingestion(source_repo=args.repo, source_version=args.version)
    print(f"ingest run #{result.get('run_id')}: {result.get('status')} — {result.get('counts')}")
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
