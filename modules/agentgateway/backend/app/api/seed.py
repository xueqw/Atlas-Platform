import json

from sqlmodel import Session, select
from app.core.database import engine
from app.models.db import ModelRegistry, PromptTemplate, CapabilityItem
from app.models.schemas import CapabilityItemCreate


SEED_MODELS = [
    {"provider": "openai", "model_id": "gpt-4o-mini", "display_name": "GPT-4o Mini",
     "capability_tags": '["chat","fast","cheap"]', "context_window": 128000,
     "max_output_tokens": 16384, "input_price_per_1k": 0.00015, "output_price_per_1k": 0.0006},
    {"provider": "openai", "model_id": "gpt-4o", "display_name": "GPT-4o",
     "capability_tags": '["chat","reasoning","vision","complex"]', "context_window": 128000,
     "max_output_tokens": 16384, "input_price_per_1k": 0.0025, "output_price_per_1k": 0.01},
    {"provider": "anthropic", "model_id": "claude-sonnet-4-20250514", "display_name": "Claude Sonnet 4",
     "capability_tags": '["chat","reasoning","coding","complex"]', "context_window": 200000,
     "max_output_tokens": 16384, "input_price_per_1k": 0.003, "output_price_per_1k": 0.015,
     "supports_streaming": True},
    {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5",
     "capability_tags": '["chat","fast","cheap"]', "context_window": 200000,
     "max_output_tokens": 8192, "input_price_per_1k": 0.001, "output_price_per_1k": 0.005},
    {"provider": "openai", "model_id": "gpt-4.1", "display_name": "GPT-4.1",
     "capability_tags": '["chat","reasoning","coding","complex"]', "context_window": 1000000,
     "max_output_tokens": 32768, "input_price_per_1k": 0.002, "output_price_per_1k": 0.008},
    {"provider": "deepseek", "model_id": "deepseek-chat", "display_name": "DeepSeek Chat",
     "capability_tags": '["chat","cheap","chinese"]', "context_window": 128000,
     "max_output_tokens": 8192, "input_price_per_1k": 0.00014, "output_price_per_1k": 0.00028},
    {"provider": "glm", "model_id": "glm-4-flash", "display_name": "GLM-4 Flash",
     "capability_tags": '["chat","fast","cheap","chinese"]', "context_window": 128000,
     "max_output_tokens": 4096, "input_price_per_1k": 0.0, "output_price_per_1k": 0.0,
     "supports_streaming": True},
    {"provider": "openai", "model_id": "qwen-turbo", "display_name": "Qwen Turbo",
     "capability_tags": '["chat","fast","chinese"]', "context_window": 32000,
     "max_output_tokens": 8192, "input_price_per_1k": 0.0, "output_price_per_1k": 0.0,
     "supports_streaming": True},
    {"provider": "openai", "model_id": "gpt-5.4", "display_name": "GPT-5.4",
     "capability_tags": '["chat","reasoning","coding","complex"]', "context_window": 400000,
     "max_output_tokens": 32768, "input_price_per_1k": 0.0, "output_price_per_1k": 0.0,
     "supports_streaming": True},
]

SEED_TEMPLATES = [
    {
        "name": "通用客服助手",
        "category": "customer_service",
        "content": "你是一个专业的客服助手。你的职责是帮助用户解决问题，回答产品相关问题，并在无法解决时建议转人工。\n\n约束：\n- 保持友好、耐心的态度\n- 不确定答案时明确告知用户\n- 不承诺无法兑现的解决方案\n- 不泄露内部流程信息",
    },
    {
        "name": "技术文档问答",
        "category": "knowledge_qa",
        "content": "你是一个技术文档助手。你会根据提供的文档内容回答用户问题，引用原文保持准确。\n\n约束：\n- 所有回答必须基于文档内容\n- 引用时注明文档来源\n- 文档未覆盖的问题明确告知\n- 不要杜撰技术细节",
    },
    {
        "name": "代码审查助手",
        "category": "coding",
        "content": "你是一个代码审查助手。你会审查用户提交的代码，提供改进建议。\n\n约束：\n- 关注可读性、性能、安全性\n- 说明每个建议的原因\n- 区分严重问题和建议改进\n- 不要重写整个代码块",
    },
]


def seed_model_registry():
    with Session(engine) as session:
        existing = session.exec(select(ModelRegistry)).all()
        existing_ids = {m.model_id for m in existing}

        if not existing:
            for item in SEED_MODELS:
                session.add(ModelRegistry(**item))
            for item in SEED_TEMPLATES:
                t = PromptTemplate(
                    name=item["name"],
                    category=item["category"],
                    content=item["content"],
                    preview=item["content"][:100],
                )
                session.add(t)
        else:
            for item in SEED_MODELS:
                if item["model_id"] not in existing_ids:
                    session.add(ModelRegistry(**item))

        session.commit()


def seed_capabilities():
    import json

    with Session(engine) as session:
        existing = session.exec(select(CapabilityItem)).all()

        if not existing:
            # Seed prompt templates as capabilities
            templates = session.exec(select(PromptTemplate)).all()
            for t in templates:
                session.add(CapabilityItem(
                    type="prompt",
                    name=t.name,
                    description=f"类别: {t.category}",
                    tags=json.dumps([t.category, "prompt"]),
                    config=json.dumps({"content": t.content, "template_id": t.id}),
                ))

            # Seed tool capabilities
            tools = [
                {
                    "name": "天气查询",
                    "description": "查询指定城市的实时天气信息",
                    "tags": ["tool", "weather"],
                    "config": {
                        "parameters": {
                            "city": {"type": "string", "description": "城市名称"}
                        }
                    },
                },
                {
                    "name": "搜索引擎",
                    "description": "联网搜索获取最新信息",
                    "tags": ["tool", "search", "web"],
                    "config": {
                        "parameters": {
                            "query": {"type": "string", "description": "搜索关键词"}
                        }
                    },
                },
            ]
            for tool in tools:
                session.add(CapabilityItem(
                    type="tool",
                    name=tool["name"],
                    description=tool["description"],
                    tags=json.dumps(tool["tags"]),
                    config=json.dumps(tool["config"]),
                ))

        # Models are single-sourced in capability_items(type=model). Seed the
        # built-in models ONLY when the library has no model entries yet (fresh
        # bootstrap). A non-empty library is never topped up — this is what makes
        # a user-deleted model stay deleted across restarts (no revival). Built-in
        # models store NO api_key/base_url: they resolve from the provider's env
        # credentials at call time (avoids persisting a truncated/broken key).
        has_model_caps = any(c.type == "model" for c in existing)
        if not has_model_caps:
            for m in session.exec(select(ModelRegistry)).all():
                session.add(CapabilityItem(
                    type="model",
                    name=m.display_name,
                    description=f"{m.provider}/{m.model_id} — 上下文: {m.context_window}, 输出: {m.max_output_tokens}",
                    tags=m.capability_tags,
                    config=json.dumps({
                        "provider": m.provider,
                        "model_id": m.model_id,
                        "model_name": m.model_id,
                        "context_window": m.context_window,
                        "max_output_tokens": m.max_output_tokens,
                        "input_price_per_1k": m.input_price_per_1k,
                        "output_price_per_1k": m.output_price_per_1k,
                        "supports_streaming": m.supports_streaming,
                        "supports_vision": m.supports_vision,
                        "is_available": m.is_available,
                    }),
                ))

        session.commit()


def seed_node_types():
    """Seed built-in node type definitions from the NodeRegistry into the database."""
    from app.core.nodes.base import NodeRegistry
    import app.core.nodes.core  # noqa: F401
    import app.core.nodes.extensions  # noqa: F401
    import app.core.nodes.agent_node  # noqa: F401

    with Session(engine) as session:
        existing = session.exec(select(NodeTypeRegistry)).all()
        existing_types = {nt.node_type for nt in existing}

        for node_type, node_cls in NodeRegistry.list_all().items():
            if node_type in existing_types:
                continue
            session.add(NodeTypeRegistry(
                node_type=node_cls.node_type,
                display_name=node_cls.display_name,
                category=node_cls.category,
                config_schema=node_cls.config_schema,
                input_keys=json.dumps(node_cls.input_keys, ensure_ascii=False),
                output_keys=json.dumps(node_cls.output_keys, ensure_ascii=False),
                enabled=True,
            ))

        session.commit()


def seed_hermes_rules():
    """Seed built-in Hermes rule engine rules."""
    with Session(engine) as session:
        existing = session.exec(select(HermesRule)).first()
        if existing is not None:
            return

        rules = [
            # Parameter validation rules
            HermesRule(
                rule_type="param_validation",
                node_type="p",
                condition_expr="len(config.system_prompt) >= 200",
                severity="error",
                message_template="系统提示词必须至少200个字符",
            ),
            HermesRule(
                rule_type="param_validation",
                node_type="p",
                condition_expr="len(config.system_prompt) <= 4000",
                severity="error",
                message_template="系统提示词不能超过4000个字符",
            ),
            HermesRule(
                rule_type="param_validation",
                node_type="m",
                condition_expr="config.model_name in model_registry",
                severity="error",
                message_template="模型 {config.model_name} 不在模型注册表中",
            ),
            HermesRule(
                rule_type="param_validation",
                node_type="x",
                condition_expr="config.code and len(config.code) > 0",
                severity="error",
                message_template="代码内容不能为空",
            ),
            HermesRule(
                rule_type="param_validation",
                node_type="h",
                condition_expr="config.url and config.url.startswith(('http://', 'https://'))",
                severity="error",
                message_template="URL 必须以 http:// 或 https:// 开头",
            ),
            # Connection constraint rules
            HermesRule(
                rule_type="connection_constraint",
                node_type="i",
                condition_expr="len(incoming_edges) == 0",
                severity="error",
                message_template="入口节点(I)不能有入边",
            ),
            HermesRule(
                rule_type="connection_constraint",
                node_type="o",
                condition_expr="len(outgoing_edges) == 0",
                severity="error",
                message_template="出口节点(O)不能有出边",
            ),
            HermesRule(
                rule_type="connection_constraint",
                node_type="m",
                condition_expr="has_prompt_upstream",
                severity="error",
                message_template="模型节点(M)上游必须有提示词来源(P节点)",
            ),
            HermesRule(
                rule_type="connection_constraint",
                node_type="*",
                condition_expr="no_cycles",
                severity="error",
                message_template="DAG 图中不能有循环依赖",
            ),
            HermesRule(
                rule_type="connection_constraint",
                node_type="*",
                condition_expr="no_isolated_nodes",
                severity="warning",
                message_template="存在孤立节点，建议删除或连接",
            ),
            # Build evaluation rules
            HermesRule(
                rule_type="build_evaluation",
                node_type="*",
                condition_expr="evaluation_score >= 0.6",
                severity="error",
                message_template="评估分数 {score} 低于阈值 0.6，无法发布",
            ),
            HermesRule(
                rule_type="build_evaluation",
                node_type="*",
                condition_expr="evaluation_score >= 0.8",
                severity="warning",
                message_template="评估分数 {score} 低于优秀阈值 0.8",
            ),
        ]
        for rule in rules:
            session.add(rule)
        session.commit()


def seed_dag_templates():
    """Seed built-in DAG templates."""
    with Session(engine) as session:
        import json
        existing = session.exec(select(DAGTemplate)).first()
        if existing is not None:
            return

        templates = [
            DAGTemplate(
                name="客服机器人",
                description="P→M→T 标准客服流程，提示词→模型→工具调用",
                category="customer_service",
                icon="headset",
                sort_order=1,
                graph_json=json.dumps({
                    "nodes": [
                        {"id": "p1", "type": "p", "position": {"x": 100, "y": 200}, "config": {
                            "role_name": "客服助手", "role_description": "专业客服，耐心解答用户问题",
                            "system_prompt": "你是一个专业的客服助手。帮助用户解决问题，回答产品相关问题。保持友好、耐心的态度。"
                        }},
                        {"id": "m1", "type": "m", "position": {"x": 400, "y": 200}, "config": {
                            "model_name": "gpt-4o-mini", "provider": "openai", "temperature": 0.7
                        }},
                        {"id": "t1", "type": "t", "position": {"x": 700, "y": 200}, "config": {
                            "tool_name": "knowledge_search", "tool_params": "{}"
                        }},
                    ],
                    "edges": [
                        {"id": "e1", "source": "p1", "target": "m1"},
                        {"id": "e2", "source": "m1", "target": "t1"},
                    ],
                }),
            ),
            DAGTemplate(
                name="RAG 知识问答",
                description="P→K→M 检索增强生成，先从知识库检索再生成回答",
                category="knowledge_qa",
                icon="book",
                sort_order=2,
                graph_json=json.dumps({
                    "nodes": [
                        {"id": "p1", "type": "p", "position": {"x": 100, "y": 200}, "config": {
                            "role_name": "知识问答助手", "role_description": "基于知识库回答用户问题",
                            "system_prompt": "你是一个知识问答助手。根据提供的文档内容回答用户问题。引用原文保持准确。文档未覆盖的问题明确告知。"
                        }},
                        {"id": "k1", "type": "k", "position": {"x": 400, "y": 200}, "config": {
                            "collection_name": "default", "top_k": 5
                        }},
                        {"id": "m1", "type": "m", "position": {"x": 700, "y": 200}, "config": {
                            "model_name": "gpt-4o-mini", "provider": "openai", "temperature": 0.3
                        }},
                    ],
                    "edges": [
                        {"id": "e1", "source": "p1", "target": "k1"},
                        {"id": "e2", "source": "k1", "target": "m1"},
                    ],
                }),
            ),
            DAGTemplate(
                name="多步推理",
                description="P→M→C→[M1|M2]→O 条件分支，根据模型输出选择不同推理路径",
                category="reasoning",
                icon="git-branch",
                sort_order=3,
                graph_json=json.dumps({
                    "nodes": [
                        {"id": "p1", "type": "p", "position": {"x": 100, "y": 200}, "config": {
                            "role_name": "推理引擎", "role_description": "多步推理分析用户问题",
                            "system_prompt": "你是一个推理引擎。分析用户问题，将其拆解为子问题并逐步推理。"
                        }},
                        {"id": "m1", "type": "m", "position": {"x": 400, "y": 200}, "config": {
                            "model_name": "gpt-4o", "provider": "openai", "temperature": 0.3
                        }},
                        {"id": "c1", "type": "c", "position": {"x": 700, "y": 200}, "config": {
                            "mode": "llm", "condition": ""
                        }},
                        {"id": "m2", "type": "m", "position": {"x": 1000, "y": 100}, "config": {
                            "model_name": "claude-sonnet-4-20250514", "provider": "anthropic", "temperature": 0.5
                        }},
                        {"id": "m3", "type": "m", "position": {"x": 1000, "y": 300}, "config": {
                            "model_name": "gpt-4o-mini", "provider": "openai", "temperature": 0.7
                        }},
                        {"id": "o1", "type": "o", "position": {"x": 1300, "y": 200}, "config": {
                            "output_mode": "text"
                        }},
                    ],
                    "edges": [
                        {"id": "e1", "source": "p1", "target": "m1"},
                        {"id": "e2", "source": "m1", "target": "c1"},
                        {"id": "e3", "source": "c1", "target": "m2", "data": {"branch": "true"}},
                        {"id": "e4", "source": "c1", "target": "m3", "data": {"branch": "false"}},
                        {"id": "e5", "source": "m2", "target": "o1"},
                        {"id": "e6", "source": "m3", "target": "o1"},
                    ],
                }),
            ),
            DAGTemplate(
                name="数据分析助手",
                description="P→X→M 代码执行流程，先生成分析代码再执行并总结结果",
                category="data_analysis",
                icon="chart",
                sort_order=4,
                graph_json=json.dumps({
                    "nodes": [
                        {"id": "p1", "type": "p", "position": {"x": 100, "y": 200}, "config": {
                            "role_name": "数据分析师", "role_description": "编写Python代码分析数据并生成报告",
                            "system_prompt": "你是一个数据分析师。根据用户需求编写Python代码进行数据分析。返回清晰的分析结果和可视化建议。"
                        }},
                        {"id": "x1", "type": "x", "position": {"x": 400, "y": 200}, "config": {
                            "language": "python", "code": "", "timeout": 30
                        }},
                        {"id": "m1", "type": "m", "position": {"x": 700, "y": 200}, "config": {
                            "model_name": "gpt-4o", "provider": "openai", "temperature": 0.3
                        }},
                    ],
                    "edges": [
                        {"id": "e1", "source": "p1", "target": "x1"},
                        {"id": "e2", "source": "x1", "target": "m1"},
                    ],
                }),
            ),
            DAGTemplate(
                name="外部 API 集成",
                description="I→P→M→H→O 结构化输入输出，模型处理后调用外部API",
                category="integration",
                icon="globe",
                sort_order=5,
                graph_json=json.dumps({
                    "nodes": [
                        {"id": "i1", "type": "i", "position": {"x": 100, "y": 200}, "config": {
                            "input_mode": "json_schema"
                        }},
                        {"id": "p1", "type": "p", "position": {"x": 300, "y": 200}, "config": {
                            "role_name": "API 编排器", "role_description": "将用户请求转换为API调用参数",
                            "system_prompt": "你是一个API编排助手。将用户的结构化请求解析为合适的API调用参数。按JSON格式返回调用计划。"
                        }},
                        {"id": "m1", "type": "m", "position": {"x": 500, "y": 200}, "config": {
                            "model_name": "gpt-4o-mini", "provider": "openai", "temperature": 0.2
                        }},
                        {"id": "h1", "type": "h", "position": {"x": 700, "y": 200}, "config": {
                            "url": "", "method": "POST", "timeout": 30
                        }},
                        {"id": "o1", "type": "o", "position": {"x": 900, "y": 200}, "config": {
                            "output_mode": "json_schema"
                        }},
                    ],
                    "edges": [
                        {"id": "e1", "source": "i1", "target": "p1"},
                        {"id": "e2", "source": "p1", "target": "m1"},
                        {"id": "e3", "source": "m1", "target": "h1"},
                        {"id": "e4", "source": "h1", "target": "o1"},
                    ],
                }),
            ),
        ]
        for t in templates:
            session.add(t)
        session.commit()


# Import models needed by seed functions
from app.models.db import NodeTypeRegistry, HermesRule, DAGTemplate  # noqa: E402
from app.models.db import UserProfile, WorkspaceContext  # noqa: E402


def seed_planner_defaults():
    """Idempotently seed the default single-user / default-workspace rows that
    the planning_context aggregator falls back to (T1: planning-context-
    aggregation). Re-running is safe: an existing ``id="default"`` row is only
    inserted when absent, so manual edits are never clobbered."""
    with Session(engine) as session:
        if session.get(UserProfile, "default") is None:
            session.add(UserProfile(
                id="default",
                name="默认用户",
                role="",
                department="",
                industry="",
                permissions_json="[]",
                preferences_json=json.dumps(
                    {"language": "zh-CN", "interaction_style": "proposal_first"}
                ),
            ))
        if session.get(WorkspaceContext, "default") is None:
            session.add(WorkspaceContext(
                id="default",
                name="默认工作区",
                project_context="",
                connected_systems_json="[]",
                default_entities_json="[]",
                default_capability_tags_json="[]",
            ))
        session.commit()


# Sample expert_template cognitive assets (T3). Cognitive assets referenced by
# the planner — NOT executable capabilities, and NOT inlined into runtime. Only
# structured metadata lives in config; no persona / raw markdown is stored.
SEED_EXPERT_TEMPLATES = [
    {
        "name": "sales-pipeline-analyst",
        "description": "销售管道分析专家：客户分层、跟进优先级、转化漏斗与日报。",
        "tags": ["sales", "analysis", "reporting"],
        "config": {
            "domain": "sales",
            "subdomain": "pipeline-analysis",
            "deliverables": ["客户分层", "跟进优先级建议", "销售日报"],
            "workflow_hints": ["cron + report flow"],
            "recommended_modes": ["cron", "flow"],
            "recommended_capability_tags": ["sales", "analysis", "reporting"],
            "persona_summary": "面向销售团队的管道分析专家，强调结论先行、可执行建议。",
        },
    },
    {
        "name": "rag-knowledge-assistant",
        "description": "知识库问答专家：基于检索的事实问答与引用，控制幻觉。",
        "tags": ["rag", "knowledge", "qa"],
        "config": {
            "domain": "knowledge",
            "subdomain": "rag-qa",
            "deliverables": ["带引用的问答", "知识检索摘要"],
            "workflow_hints": ["retrieve then answer"],
            "recommended_modes": ["direct", "flow"],
            "recommended_capability_tags": ["rag", "knowledge", "search"],
            "persona_summary": "严谨的检索增强问答助手，先检索再作答，标注来源。",
        },
    },
]


def seed_expert_templates():
    """Idempotently seed sample ``type="expert_template"`` capabilities (T3).

    Re-running is safe: a template already present (matched by type+name) is left
    untouched; only missing ones are inserted."""
    with Session(engine) as session:
        for tpl in SEED_EXPERT_TEMPLATES:
            existing = session.exec(
                select(CapabilityItem).where(
                    CapabilityItem.type == "expert_template",
                    CapabilityItem.name == tpl["name"],
                )
            ).first()
            if existing:
                continue
            session.add(CapabilityItem(
                type="expert_template",
                name=tpl["name"],
                description=tpl["description"],
                tags=json.dumps(tpl["tags"], ensure_ascii=False),
                config=json.dumps(tpl["config"], ensure_ascii=False),
            ))
        session.commit()
