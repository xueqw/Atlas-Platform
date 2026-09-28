"""Registry for static connector tools available to agents."""
from sqlalchemy.orm import Session


def describe_call(name: str, args: dict) -> str:
    """Describe a tool call in user-facing language before approval."""
    return f"Atlas is ready to run {name} with: {args}"


TOOLS: dict[str, dict] = {}


def specs(enabled_connectors: list[str] | None = None) -> list[dict]:
    """Return OpenAI-compatible declarations for enabled static tools."""
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
    """Execute a registered static tool call."""
    tool = TOOLS.get(name)
    if not tool:
        return f"Unknown tool: {name}"
    return await tool["run"](db, arguments)
