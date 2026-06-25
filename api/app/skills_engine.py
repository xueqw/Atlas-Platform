"""Skill Hub 运行时纯逻辑（PRD M3）。无 db / 无网络，便于单测。"""


def resolve_skills(catalog: list[dict], manual_ids: list[str], auto_ids: list[str]) -> list[dict]:
    """手动(强制) ∪ 自动 → 标 source、去重、仅保留 catalog 内的 id。
    手动在前，自动补后；同时命中按手动算。"""
    by_id = {s["id"]: s for s in catalog}
    out: list[dict] = []
    seen: set[str] = set()
    for sid in list(manual_ids) + list(auto_ids):
        if sid in seen or sid not in by_id:
            continue
        seen.add(sid)
        s = by_id[sid]
        out.append({
            "id": s["id"], "name": s["name"], "content": s.get("content", ""),
            "source": "manual" if sid in manual_ids else "auto",
        })
    return out


def build_skill_instructions(selected: list[dict]) -> str:
    """把选中 skill 的正文拼成一条系统消息；空列表返回 ''。"""
    if not selected:
        return ""
    blocks = [f"【{s['name']}】\n{s.get('content', '')}".strip() for s in selected]
    return "技能指引（请遵循以下方法论与输出格式）：\n\n" + "\n\n".join(blocks)
