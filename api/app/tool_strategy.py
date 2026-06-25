"""MCP Strategy Agent（PRD M4，P0 确定性形态）。

不调 LLM：本次工具策略可由已知数据纯函数推导——
启用了哪些连接器决定 tool_specs，is_write/mcp_write_names 决定读写。
输出落进 plan_json.tool_policy 供审计，并取代 send_message 里临时的 needs_confirm 闭包。
"""


def build_tool_policy(tool_specs: list[dict], write_names: set[str],
                      connector_of: dict[str, str], requires_tools: bool) -> dict:
    """把本次可用工具整理成策略：每个工具标 connector + read/write。

    tool_specs   —— OpenAI 兼容工具声明（含静态飞书 + 动态 GitHub MCP）
    write_names  —— 写操作工具名集合（静态 is_write ∪ mcp_write_names）
    connector_of —— 静态工具名→连接器；不在表里的（GitHub MCP）默认 "github"
    requires_tools —— Strategy Agent 计划里的判断，透传
    """
    tools = []
    for spec in tool_specs:
        name = spec["function"]["name"]
        access = "write" if name in write_names else "read"
        tools.append({"name": name, "connector": connector_of.get(name, "github"), "access": access})
    return {
        "requires_tools": bool(requires_tools),
        "tools": tools,
        "write_tools": [t["name"] for t in tools if t["access"] == "write"],
    }
