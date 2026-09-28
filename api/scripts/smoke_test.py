import json
import httpx

BASE = "http://localhost:8000/api"

with httpx.Client(base_url=BASE, timeout=30, trust_env=False) as client:
    knowledge = client.post("/knowledge-bases", json={"name": "Acceptance knowledge base", "description": "Automated smoke test"})
    knowledge.raise_for_status()
    knowledge_id = knowledge.json()["id"]

    upload = client.post(
        f"/knowledge-bases/{knowledge_id}/documents",
        files={"file": ("service.txt", "The enterprise plan supports private knowledge bases and audit logs. Support hours are 9:00 AM to 6:00 PM ET on business days.", "text/plain")},
    )
    upload.raise_for_status()
    assert upload.json()["chunk_count"] == 1

    agent = client.post(
        "/agents",
        json={
            "name": "Support acceptance agent",
            "description": "Answers product questions",
            "system_prompt": "You are a product support agent. Answer only from the connected knowledge base.",
            "model": "gpt-4.1-mini",
            "knowledge_base_id": knowledge_id,
        },
    )
    agent.raise_for_status()
    agent_id = agent.json()["id"]

    conversation = client.post("/conversations", json={"title": "RAG acceptance test"})
    conversation.raise_for_status()
    conversation_id = conversation.json()["id"]

    stream = client.post(
        f"/conversations/{conversation_id}/messages/stream",
        json={"content": "What does the enterprise plan support?", "agent_id": agent_id},
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
