import json
import httpx

BASE = "http://localhost:8000/api"

with httpx.Client(base_url=BASE, timeout=30, trust_env=False) as client:
    knowledge = client.post("/knowledge-bases", json={"name": "验收知识库", "description": "自动化测试"})
    knowledge.raise_for_status()
    knowledge_id = knowledge.json()["id"]

    upload = client.post(
        f"/knowledge-bases/{knowledge_id}/documents",
        files={"file": ("service.txt", "星河企业版支持私有知识库和运行审计，服务时间是工作日九点到十八点。", "text/plain")},
    )
    upload.raise_for_status()
    assert upload.json()["chunk_count"] == 1

    agent = client.post(
        "/agents",
        json={
            "name": "验收客服",
            "description": "回答产品问题",
            "system_prompt": "你是产品客服，只根据知识库回答。",
            "model": "gpt-4.1-mini",
            "knowledge_base_id": knowledge_id,
        },
    )
    agent.raise_for_status()
    agent_id = agent.json()["id"]

    conversation = client.post("/conversations", json={"title": "RAG 迁移验收"})
    conversation.raise_for_status()
    conversation_id = conversation.json()["id"]

    stream = client.post(
        f"/conversations/{conversation_id}/messages/stream",
        json={"content": "企业版支持什么？", "agent_id": agent_id},
    )
    stream.raise_for_status()
    events = stream.text
    detail = client.get(f"/conversations/{conversation_id}").json()

    assert '"type": "sources"' in events, events
    assert '"type": "done"' in events, events
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"], detail
    sources = json.loads(detail["messages"][1]["sources"])
    assert sources and sources[0]["document"] == "service.txt", sources

    client.delete(f"/conversations/{conversation_id}").raise_for_status()
    client.delete(f"/agents/{agent_id}").raise_for_status()
    client.delete(f"/knowledge-bases/{knowledge_id}").raise_for_status()

print(json.dumps({"agent_crud": True, "knowledge_upload": True, "retrieval_sources": len(sources), "stream_completed": True}, ensure_ascii=False))
