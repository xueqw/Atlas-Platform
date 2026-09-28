"""Upload supported documents from a folder through the Atlas HTTP API.

Run from the api directory while the API is available on port 8000:
    python -m scripts.upload_research "/path/to/documents" "Research Library"
"""

import sys
import time
from pathlib import Path

import httpx


API = "http://localhost:8000"
EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json"}
MAX_MB = 10


def main() -> None:
    folder = Path(sys.argv[1])
    knowledge_base_name = sys.argv[2] if len(sys.argv) > 2 else "Research Library"
    files = sorted(path for path in folder.rglob("*") if path.suffix.lower() in EXTENSIONS and path.is_file())
    print(f"Found {len(files)} candidate files for {knowledge_base_name}.\n")

    with httpx.Client(base_url=API, timeout=600, trust_env=False) as client:
        knowledge_base = client.post(
            "/api/knowledge-bases",
            json={"name": knowledge_base_name, "description": "Research documents uploaded in bulk"},
        ).json()
        knowledge_base_id = knowledge_base["id"]
        print(f"Created knowledge base: {knowledge_base_id}\n")

        uploaded = skipped = failed = total_chunks = 0
        for index, path in enumerate(files, 1):
            size_mb = path.stat().st_size / 1024 / 1024
            if size_mb > MAX_MB:
                print(f"[{index}/{len(files)}] SKIP (>{MAX_MB} MB; {size_mb:.1f} MB) {path.name}")
                skipped += 1
                continue
            started = time.perf_counter()
            try:
                with path.open("rb") as handle:
                    response = client.post(
                        f"/api/knowledge-bases/{knowledge_base_id}/documents",
                        files={"file": (path.name, handle, "application/octet-stream")},
                    )
                if response.status_code == 201:
                    data = response.json()
                    total_chunks += data["chunk_count"]
                    uploaded += 1
                    print(f"[{index}/{len(files)}] OK chunks={data['chunk_count']:4} {int(time.perf_counter() - started)}s {path.name[:50]}")
                else:
                    failed += 1
                    print(f"[{index}/{len(files)}] FAILED HTTP {response.status_code} {response.text[:80]} {path.name[:50]}")
            except Exception as exc:
                failed += 1
                print(f"[{index}/{len(files)}] ERROR {exc} {path.name[:50]}")

        print(
            f"\nComplete: {uploaded} uploaded, {skipped} skipped, {failed} failed, "
            f"{total_chunks} chunks, knowledge base {knowledge_base_id}"
        )


if __name__ == "__main__":
    main()
