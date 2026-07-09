"""落地配置（PRD §5.7）：谁能用这个 Agent、能访问哪些资源、写操作要不要确认。

纯函数，不碰数据库/网络，方便直接单测。send_message 在真正对话前调用，
把结果用于门禁（visibility）和收窄（resource permissions）。
"""
import json

DEFAULT_DEPLOY_CONFIG = {
    "visibility": "workspace",  # private | shared | workspace | marketplace
    "shared_user_ids": [],
    "allowed_knowledge_base_ids": [],  # 空=不限制（向后兼容存量 Agent）
    "allowed_skill_ids": [],
    "allowed_connectors": [],
    "write_confirm": True,
    "api_access": False,  # 占位：需要 API Key 功能完成后才有实际入口消费它
    "call_log_enabled": True,
}


def parse_deploy_config(raw: str | None) -> dict:
    """解析存储的 JSON，缺失字段回退默认值；坏 JSON 也回退默认值，不炸对话链路。"""
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {**DEFAULT_DEPLOY_CONFIG, **data}


def check_visibility(created_by: str | None, deploy_config: dict, current_user_id: str) -> bool:
    """当前用户能否使用这个 Agent。

    历史 Agent 没有 created_by（迁移前创建），视为工作区内所有人可见，不因这次改动破坏存量行为。
    marketplace 目前等同 workspace 处理——项目内单工作区，没有跨租户市场概念。
    """
    visibility = deploy_config.get("visibility", "workspace")
    if not created_by:
        return True
    if created_by == current_user_id:
        return True
    if visibility == "private":
        return False
    if visibility == "shared":
        return current_user_id in (deploy_config.get("shared_user_ids") or [])
    return True  # workspace | marketplace


def apply_resource_permissions(
    deploy_config: dict,
    enabled_connectors: list[str],
    skill_catalog: list[dict],
    knowledge_base_id: str | None,
) -> tuple[list[str], list[dict], str | None]:
    """按落地配置收窄本次对话实际可用的连接器/Skill/知识库。允许列表为空=不限制。"""
    allowed_connectors = deploy_config.get("allowed_connectors") or []
    if allowed_connectors:
        enabled_connectors = [c for c in enabled_connectors if c in allowed_connectors]

    allowed_skill_ids = deploy_config.get("allowed_skill_ids") or []
    if allowed_skill_ids:
        skill_catalog = [s for s in skill_catalog if s["id"] in allowed_skill_ids]

    allowed_kb_ids = deploy_config.get("allowed_knowledge_base_ids") or []
    if allowed_kb_ids and knowledge_base_id and knowledge_base_id not in allowed_kb_ids:
        knowledge_base_id = None

    return enabled_connectors, skill_catalog, knowledge_base_id


# === 发布前检查清单（PRD §5.8） ===

_STATIC_WRITE_CONNECTORS = {"feishu", "github"}  # 静态已知有写操作能力的连接器


def is_high_risk_connector(provider: str) -> bool:
    """高风险 = 有写操作能力的连接器。飞书(发消息)、GitHub(写操作 MCP)静态已知；
    其余通用 MCP 连接器（高德等）按 remote_mcp._READ_ONLY_PROVIDERS 白名单判断——
    不在白名单里的视为可能有写操作，从严处理。延迟导入 remote_mcp 避免把 MCP 客户端
    依赖带进这个纯函数模块。"""
    if provider in _STATIC_WRITE_CONNECTORS:
        return True
    from .connectors import remote_mcp
    if remote_mcp.is_known(provider):
        return provider not in remote_mcp._READ_ONLY_PROVIDERS
    return False  # 未知连接器：不在启用列表里也不会被实际使用，不计入风险


def build_publish_checklist(
    kind: str,
    has_current_version: bool,
    has_passed_test: bool,
    last_eval_ok: bool | None,
    deploy_config_configured: bool,
    allowed_connectors: list[str],
) -> dict:
    """组装发布前检查清单。blocking 项任意一条不过则不能发布；warning 项不阻断，仅提示。

    kind=prompt 的 Agent 没有 run_draft_app/evaluate_draft_app 那套代码沙箱测试/批量评测机制
    （那是 kind=code 草稿专属），所以「已通过基础测试」「已有评测结果」这两项对 prompt 型
    Agent 视为始终满足——其正确性等价于「有效配置」，不因缺少代码测试基建而卡住发布。

    「敏感数据风险」本轮做成静态占位（未接入内容扫描），恒为 warning 未通过，提示人工确认。
    """
    is_prompt = kind == "prompt"
    items = [
        {"key": "has_version", "label": "有可发布的版本", "ok": has_current_version, "level": "blocking"},
        {"key": "passed_test", "label": "已通过基础测试", "ok": is_prompt or has_passed_test, "level": "blocking"},
        {"key": "has_eval", "label": "已有评测结果", "ok": is_prompt or last_eval_ok is not None, "level": "blocking"},
        {"key": "permissions_configured", "label": "已配置落地权限", "ok": deploy_config_configured, "level": "blocking"},
        {"key": "no_high_risk_connector", "label": "未绑定高风险连接器", "ok": not any(is_high_risk_connector(c) for c in allowed_connectors), "level": "warning"},
        {"key": "no_sensitive_data_risk", "label": "无敏感数据风险", "ok": False, "level": "warning"},
    ]
    can_publish = all(item["ok"] for item in items if item["level"] == "blocking")
    return {"items": items, "can_publish": can_publish}
