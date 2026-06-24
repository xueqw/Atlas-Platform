"""工具注册表：模型可调用的"连接器能力"。

每个工具 = 声明(给模型看的 JSON Schema) + 执行器(run) + write 标记(是否改外部状态)。
stream_agent 把 spec() 传给模型；模型决定调用时，dispatch() 找到对应执行器跑。
"""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors import feishu
from .models import ConnectorToken


async def _send_feishu_message(db: Session, args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "失败：消息内容为空"
    to_email = (args.get("to_email") or "").strip()
    to_mobile = (args.get("to_mobile") or "").strip()
    row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == "feishu"))
    if not row:
        return "失败：飞书未连接，请先在连接器里连接飞书"
    try:
        if to_email:  # 发给指定邮箱的同事（须在应用可用范围内）
            await feishu.send_message(to_email, text, receive_id_type="email")
            return f"已通过飞书发送给 {to_email}：{text}"
        if to_mobile:  # 手机号：先查 open_id 再发
            open_id = await feishu.lookup_open_id_by_mobile(to_mobile)
            if not open_id:
                return f"失败：手机号 {to_mobile} 在飞书通讯录里没找到对应用户（可能不在应用可用范围内）"
            await feishu.send_message(open_id, text)
            return f"已通过飞书发送给 {to_mobile}：{text}"
        if not row.open_id:
            return "失败：缺少收件人信息，请重新连接飞书"
        await feishu.send_message(row.open_id, text)  # 默认发给连接者本人
        return f"已通过飞书发送给 {row.account_name}：{text}"
    except Exception as exc:
        return f"发送失败：{exc}"


def describe_call(name: str, args: dict) -> str:
    """把一次工具调用翻译成给用户看的确认文案。"""
    if name == "send_feishu_message":
        to = args.get("to_email") or args.get("to_mobile") or "你自己"
        return f"准备通过飞书发送给【{to}】：\n「{args.get('text', '')}」"
    return f"准备执行 {name}，参数：{args}"


# 工具表：name -> {所属连接器, 模型可见声明, 执行器, 是否写操作}
TOOLS = {
    "send_feishu_message": {
        "connector": "feishu",
        "description": "通过飞书发送一条文本消息。收件人三选一：都不填=发给当前连接的用户本人（仅当用户明确说'发给我/给自己'时）；to_email=按邮箱发；to_mobile=按手机号发。注意：如果用户想发给某个具体的人（提到姓名）但没给出邮箱或手机号，不要把收件人留空（那会错发给用户自己），而应改为询问该联系人的邮箱或手机号。收件人都需在应用可用范围内。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要发送的消息正文"},
                "to_email": {"type": "string", "description": "收件人飞书邮箱"},
                "to_mobile": {"type": "string", "description": "收件人手机号，如 13800001111"},
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
        return f"未知工具：{name}"
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return f"参数解析失败：{arguments}"
    return await tool["run"](db, args)
