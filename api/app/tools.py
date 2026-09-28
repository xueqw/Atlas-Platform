"""Registry of connector tools available to agents."""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import feishu
from .models import ConnectorToken


async def _send_feishu_message(db: Session, args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "Failed: message text is empty."
    to_email = (args.get("to_email") or "").strip()
    to_mobile = (args.get("to_mobile") or "").strip()
    row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == "feishu"))
    if not row:
        return "Failed: Feishu is not connected. Connect it in Settings first."
    try:
        if to_email:  # 发给指定邮箱的同事（须在应用可用范围内）
            await feishu.send_message(to_email, text, receive_id_type="email")
            return f"Sent through Feishu to {to_email}: {text}"
        if to_mobile:  # 手机号：先查 open_id 再发
            open_id = await feishu.lookup_open_id_by_mobile(to_mobile)
            if not open_id:
                return f"Failed: no Feishu user was found for {to_mobile}."
            await feishu.send_message(open_id, text)
            return f"Sent through Feishu to {to_mobile}: {text}"
        if not row.open_id:
            return "Failed: recipient information is missing. Reconnect Feishu."
        await feishu.send_message(row.open_id, text)  # 默认发给连接者本人
        return f"Sent through Feishu to {row.account_name}: {text}"
    except Exception as exc:
        return f"Send failed: {exc}"


def describe_call(name: str, args: dict) -> str:
    """Describe a tool call in user-facing language before approval."""
    if name == "send_feishu_message":
        to = args.get("to_email") or args.get("to_mobile") or "your connected account"
        return f"Atlas is ready to send this through Feishu to {to}:\n\n{args.get('text', '')}"
    return f"Atlas is ready to run {name} with: {args}"


# 工具表：name -> {所属连接器, 模型可见声明, 执行器, 是否写操作}
TOOLS = {
    "send_feishu_message": {
        "connector": "feishu",
        "description": "Send a text message through Feishu. Use to_email or to_mobile for a specific recipient. Leave both empty only when the user explicitly asks to send to their own connected account. Ask for an email address or mobile number when a named recipient is ambiguous.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message body"},
                "to_email": {"type": "string", "description": "Recipient's Feishu email address"},
                "to_mobile": {"type": "string", "description": "Recipient's mobile number"},
            },
            "required": ["text"],
        },
        "run": _send_feishu_message,
        "write": True,  # 改外部状态，属危险操作，执行前需用户确认
    },
}


def specs(enabled_connectors: list[str] | None = None) -> list[dict]:
    """转成 OpenAI 兼容的 tools 声明。enabled_connectors 为 None 时返回全部，
    否则只返回所属连接器被勾选的工具。"""
    return [
        {"type": "function", "function": {"name": name, "description": t["description"], "parameters": t["parameters"]}}
        for name, t in TOOLS.items()
        if enabled_connectors is None or t["connector"] in enabled_connectors
    ]


def tools_of(connector: str) -> list[str]:
    return [name for name, t in TOOLS.items() if t["connector"] == connector]


def is_write(name: str) -> bool:
    return bool(TOOLS.get(name, {}).get("write"))


async def dispatch(db: Session, name: str, arguments: str) -> str:
    """执行一个工具调用。arguments 是模型给的 JSON 字符串。"""
    tool = TOOLS.get(name)
    if not tool:
        return f"Unknown tool: {name}"
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return f"Could not parse tool arguments: {arguments}"
    return await tool["run"](db, args)
