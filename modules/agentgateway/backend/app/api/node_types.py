"""API endpoint to serve registered node types to the frontend palette."""

import json
from typing import List

from fastapi import APIRouter

from app.core.nodes.base import NodeRegistry
from app.models.schemas import NodeTypeResponse

# Import to trigger node registrations
import app.core.nodes.core  # noqa: F401
import app.core.nodes.extensions  # noqa: F401

router = APIRouter(tags=["node-types"])


@router.get("/node-types", response_model=List[NodeTypeResponse])
def list_node_types():
    result = []
    for node_type, node_cls in NodeRegistry.list_all().items():
        result.append(NodeTypeResponse(
            id=0,  # Not persisted yet; ID comes from DB seed
            node_type=node_cls.node_type,
            display_name=node_cls.display_name,
            category=node_cls.category,
            config_schema=node_cls.config_schema,
            input_keys=json.dumps(node_cls.input_keys),
            output_keys=json.dumps(node_cls.output_keys),
            enabled=True,
        ))
    return result
