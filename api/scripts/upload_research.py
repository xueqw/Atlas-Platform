"""把一个文件夹下所有支持的文档,通过真实平台 HTTP 接口批量灌入一个知识库。

用法（在 api/ 目录下,确保后端已在 8000 端口运行）：
    python -m scripts.upload_research "D:\\路径\\文件夹" "知识库名"
走的是和前端完全一样的上传链路：解析 → 切块 → 转向量 → 入库。
"""
import sys
import time
from pathlib import Path

import httpx

API = "http://localhost:8000"
EXTS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json"}
MAX_MB = 10


def main() -> None:
    folder = Path(sys.argv[1])
    kb_name = sys.argv[2] if len(sys.argv) > 2 else "真实测试库"
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in EXTS and p.is_file())
    print(f"发现 {len(files)} 个候选文件,目标知识库：{kb_name}\n")

    with httpx.Client(base_url=API, timeout=600, trust_env=False) as client:
        kb = client.post("/api/knowledge-bases", json={"name": kb_name, "description": "批量灌入的真实研究资料"}).json()
        kb_id = kb["id"]
        print(f"知识库已建：{kb_id}\n")

        ok = skipped = failed = total_chunks = 0
        for i, path in enumerate(files, 1):
            size_mb = path.stat().st_size / 1024 / 1024
            if size_mb > MAX_MB:
                print(f"[{i}/{len(files)}] 跳过(>{MAX_MB}MB {size_mb:.1f}) {path.name}")
                skipped += 1
                continue
            started = time.perf_counter()
            try:
                with path.open("rb") as fh:
                    r = client.post(f"/api/knowledge-bases/{kb_id}/documents", files={"file": (path.name, fh, "application/octet-stream")})
                if r.status_code == 201:
                    data = r.json()
                    total_chunks += data["chunk_count"]
                    ok += 1
                    print(f"[{i}/{len(files)}] OK  块={data['chunk_count']:4}  {int((time.perf_counter()-started))}s  {path.name[:50]}")
                else:
                    failed += 1
                    print(f"[{i}/{len(files)}] 失败 HTTP{r.status_code} {r.text[:80]}  {path.name[:50]}")
            except Exception as exc:
                failed += 1
                print(f"[{i}/{len(files)}] 异常 {exc}  {path.name[:50]}")

        print(f"\n完成：成功 {ok}  跳过 {skipped}  失败 {failed}  总块数 {total_chunks}  知识库 {kb_id}")


if __name__ == "__main__":
    main()
