"""Base node and registry for the DAG execution engine."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseNode(ABC):
    node_type: str = ""
    display_name: str = ""
    category: str = "core"  # core, knowledge, tool, flow, external
    config_schema: str = "{}"  # JSON Schema
    input_keys: List[str] = []
    output_keys: List[str] = []

    def __init__(self, config: Dict[str, Any] | None = None):
        self.config = config or {}

    @abstractmethod
    async def run(self, inputs: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        """Execute this node. Receives resolved input_keys from State, returns output key-values."""
        ...


class NodeRegistry:
    _nodes: Dict[str, type[BaseNode]] = {}

    @classmethod
    def register(cls, node_cls: type[BaseNode]):
        if not node_cls.node_type:
            raise ValueError(f"{node_cls.__name__} must define node_type")
        cls._nodes[node_cls.node_type] = node_cls
        return node_cls

    @classmethod
    def get(cls, node_type: str) -> type[BaseNode] | None:
        return cls._nodes.get(node_type)

    @classmethod
    def list_all(cls) -> Dict[str, type[BaseNode]]:
        return dict(cls._nodes)

    @classmethod
    def list_by_category(cls) -> Dict[str, List[type[BaseNode]]]:
        result: Dict[str, List[type[BaseNode]]] = {}
        for nc in cls._nodes.values():
            result.setdefault(nc.category, []).append(nc)
        return result


def register_node(cls: type[BaseNode] | None = None):
    """Decorator to register a node class in the NodeRegistry."""
    if cls is not None:
        NodeRegistry.register(cls)
        return cls
    def wrapper(c):
        NodeRegistry.register(c)
        return c
    return wrapper
