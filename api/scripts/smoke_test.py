import json
import urllib.request

BASE = "http://localhost:8000/api"


def request(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8")


conversation = json.loads(request("/conversations", "POST", {"title": "迁移验收任务"}))
events = request(f"/conversations/{conversation['id']}/messages/stream", "POST", {"content": "企业版支持哪些功能？"})
detail = json.loads(request(f"/conversations/{conversation['id']}"))

assert '"type": "done"' in events, events
assert [message["role"] for message in detail["messages"]] == ["user", "assistant"], detail
assert detail["messages"][1]["content"], detail
print(json.dumps({"conversation": conversation["id"], "messages": len(detail["messages"]), "stream_completed": True}, ensure_ascii=False))
