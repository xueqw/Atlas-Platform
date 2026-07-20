"""Multi-agent adapter onto the unified AgentRuntimeService boundary."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable, Mapping
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import tools as builtin_tools
from .connectors import github_mcp, remote_mcp
from .models import AgentVersion
from .multi_agent_repository import MultiAgentRepository, OrchestrationScope
from .multi_agent_runtime import ResultEnvelope, TaskEnvelope, ToolPolicy, WorkerSpec
from .runtime_contract import (
    RuntimeErrorCategory, RuntimeFailure, RuntimeSource, RuntimeStartRequest,
    RuntimeStatus, classify_error,
)
from .runtime_graph import AtlasAgentState, RuntimeDecision, RuntimePhaseOneGraph
from .runtime_persistence import SqlAlchemyRuntimeEventRepository, SqlAlchemyRuntimeRunRepository
from .runtime_service import AgentRuntimeService, GraphLegacyAdapter, RuntimeFeatureFlags


ToolCallable = Callable[[Session, dict[str, Any]], Awaitable[Any] | Any]
ModelComplete = Callable[..., Awaitable[str]]
CancellationProbe = Callable[[], Awaitable[bool] | bool]


class WorkerExecutionCancelled(Exception):
    """A persisted orchestration cancellation observed at a worker boundary."""

    def __init__(self, message: str, *, side_effects_started: bool = False) -> None:
        super().__init__(message)
        self.side_effects_started = side_effects_started


@dataclass(frozen=True)
class TrustedTool:
    name: str
    access: str
    invoke: ToolCallable
    required_scope: frozenset[str] = frozenset()


class ServerToolRegistry:
    """Server-owned tool metadata; model/caller payloads can only narrow it."""

    def __init__(
        self,
        tools: Mapping[str, TrustedTool] | None = None,
        *,
        discovery_warnings: list[dict[str, str]] | None = None,
    ) -> None:
        self._tools = dict(tools or {})
        self._discovery_warnings = tuple(discovery_warnings or ())

    @classmethod
    def builtins(cls) -> "ServerToolRegistry":
        entries: dict[str, TrustedTool] = {}
        for name, item in builtin_tools.TOOLS.items():
            async def invoke(db: Session, arguments: dict[str, Any], *, tool_name: str = name):
                return await builtin_tools.dispatch(db, tool_name, json.dumps(arguments, ensure_ascii=False))
            entries[name] = TrustedTool(
                name=name,
                access="write" if item.get("write") else "read",
                invoke=invoke,
                required_scope=frozenset({f"tool:{name}:write"}) if item.get("write") else frozenset(),
            )
        return cls(entries)

    @classmethod
    async def discover(cls, connectors: list[str] | None = None) -> "ServerToolRegistry":
        """Build a server-owned registry from configured static and MCP tools.

        MCP metadata is accepted only from configured server connectors. Tool
        names must be unique across providers; collisions fail closed instead
        of allowing a caller/model to choose an execution target.
        """
        registry = cls.builtins()
        enabled = set(connectors or [])
        providers = list(remote_mcp.providers())

        async def register_specs(
            owner: str, specs: list[dict[str, Any]], write_names: set[str]
        ) -> None:
            for spec in specs:
                function = spec.get("function") if isinstance(spec, dict) else None
                name = str((function or {}).get("name") or "")
                if not name or name in registry._tools:
                    # Static tools take precedence; cross-provider collisions
                    # are deliberately unavailable to the orchestrator.
                    continue
                access = "write" if name in write_names else "read"
                if owner == "github":
                    async def invoke(
                        _db: Session, arguments: dict[str, Any], *, tool_name: str = name
                    ):
                        return await github_mcp.call_tool(
                            tool_name, json.dumps(arguments, ensure_ascii=False)
                        )
                else:
                    async def invoke(
                        _db: Session, arguments: dict[str, Any], *,
                        provider: str = owner, tool_name: str = name,
                    ):
                        return await remote_mcp.call_tool(
                            provider, tool_name, json.dumps(arguments, ensure_ascii=False)
                        )
                registry._tools[name] = TrustedTool(
                    name=name, access=access, invoke=invoke,
                    required_scope=(frozenset({f"tool:{name}:write"})
                                    if access == "write" else frozenset()),
                )

        if "github" in enabled and github_mcp.is_configured():
            try:
                specs, writes = await github_mcp.tool_specs()
                await register_specs("github", specs, set(writes))
            except Exception as exc:
                # An unavailable remote connector cannot become trusted.
                registry._record_discovery_warning("github", exc)
        for provider in providers:
            if provider not in enabled or not remote_mcp.is_configured(provider):
                continue
            try:
                specs, writes = await remote_mcp.tool_specs(provider)
                await register_specs(provider, specs, set(writes))
            except Exception as exc:
                registry._record_discovery_warning(provider, exc)
        return registry

    def _record_discovery_warning(self, provider: str, exc: Exception) -> None:
        # Keep the observable payload bounded and free of connector secrets.
        # Exception messages can embed URLs, tokens, or process arguments, so
        # only the server-owned provider and exception class cross this edge.
        self._discovery_warnings = (*self._discovery_warnings, {
            "provider": provider,
            "category": RuntimeErrorCategory.DEPENDENCY.value,
            "error_type": type(exc).__name__,
            "decision": "fail_closed",
        })

    @property
    def discovery_warnings(self) -> tuple[dict[str, str], ...]:
        return self._discovery_warnings

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def get(self, name: str) -> TrustedTool | None:
        return self._tools.get(name)


DEFAULT_TOOL_REGISTRY = ServerToolRegistry.builtins()


def _json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class SubagentRuntimeExecutor:
    """Execute one isolated worker through a pinned parent AgentVersion."""

    def __init__(
        self,
        db: Session,
        scope: OrchestrationScope,
        *,
        model_complete: ModelComplete,
        session_factory: sessionmaker,
        tool_registry: ServerToolRegistry = DEFAULT_TOOL_REGISTRY,
        authorizations: Mapping[str, dict[str, str]] | None = None,
        cancellation_probe: CancellationProbe | None = None,
        cancellation_poll_seconds: float = 0.05,
    ) -> None:
        self.db = db
        self.scope = scope
        self.model_complete = model_complete
        self.session_factory = session_factory
        self.tools = tool_registry
        self.authorizations = dict(authorizations or {})
        self.cancellation_probe = cancellation_probe
        self.cancellation_poll_seconds = max(0.01, cancellation_poll_seconds)
        state = MultiAgentRepository(db).get_run(scope).scheduler_state or {}
        self.parent_version_id = str(state.get("parent_agent_version_id") or "")
        self.user_grants = set(state.get("user_tool_grants") or [])
        self.orchestrator_grants = set(state.get("orchestrator_tool_grants") or [])

    def _snapshot(self) -> dict[str, Any]:
        version = self.db.scalar(select(AgentVersion).where(
            AgentVersion.id == self.parent_version_id,
            AgentVersion.agent_id == self.scope.agent_id,
        ))
        if version is None:
            raise RuntimeFailure(RuntimeErrorCategory.VALIDATION, "pinned parent AgentVersion is unavailable")
        try:
            snapshot = json.loads(version.snapshot_json)
        except json.JSONDecodeError as exc:
            raise RuntimeFailure(RuntimeErrorCategory.VALIDATION, "pinned AgentVersion snapshot is invalid") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeFailure(RuntimeErrorCategory.VALIDATION, "pinned AgentVersion snapshot is invalid")
        return snapshot

    async def __call__(self, envelope: TaskEnvelope, spec: WorkerSpec) -> ResultEnvelope:
        runtime_run_id = ""
        service: AgentRuntimeService | None = None
        try:
            await self._raise_if_cancelled()
            tool_observation = await self._execute_declared_tool(envelope, spec)
            if isinstance(tool_observation, ResultEnvelope):
                return tool_observation
            await self._raise_if_cancelled()
            snapshot = self._snapshot()
            system_prompt = str(snapshot.get("system_prompt") or snapshot.get("prompt") or "")
            model_name = str(snapshot.get("model") or "") or None

            async def model(state: AtlasAgentState) -> RuntimeDecision:
                messages = [{"role": "system", "content": system_prompt}, {
                    "role": "system",
                    "content": f"Worker role: {spec.role}\nObjective: {spec.objective}\nReturn JSON matching: {json.dumps(spec.output_schema, ensure_ascii=False)}",
                }, *state.messages]
                response = await self.model_complete(messages, model=model_name)
                return RuntimeDecision(response=response)

            graph = RuntimePhaseOneGraph(model, prefer_langgraph=False)
            service = AgentRuntimeService(
                graph,
                event_repository=SqlAlchemyRuntimeEventRepository(self.session_factory),
                run_repository=SqlAlchemyRuntimeRunRepository(self.session_factory),
                legacy_adapter=GraphLegacyAdapter(graph),
                flags=RuntimeFeatureFlags(langgraph_enabled=True, allow_legacy_fallback=False),
            )
            worker_input = json.dumps({
                "schema_version": envelope.schema_version,
                "task_id": envelope.task_id,
                "payload": envelope.payload,
                "artifact_refs": list(envelope.artifact_refs),
                "tool_observation": tool_observation,
            }, ensure_ascii=False)
            handle = await service.start(RuntimeStartRequest(
                workspace_id=envelope.workspace_id,
                user_id=envelope.user_id,
                agent_id=envelope.agent_id,
                agent_version_id=self.parent_version_id,
                source=RuntimeSource.SUBAGENT,
                input=worker_input,
                idempotency_key=f"subagent:{envelope.envelope_id}",
            ))
            runtime_run_id = handle.run_id
            handle = await self._await_with_cancellation(
                service.wait(run_id=handle.run_id, workspace_id=envelope.workspace_id),
                on_cancel=lambda: self._cancel_runtime(service, envelope, handle.run_id),
            )
            state = service.get_state(run_id=handle.run_id, workspace_id=envelope.workspace_id)
            if handle.status is not RuntimeStatus.SUCCEEDED:
                category = str((state.errors[-1] if state.errors else {}).get("category") or RuntimeErrorCategory.TERMINAL.value)
                raise RuntimeFailure(RuntimeErrorCategory(category), str((state.errors[-1] if state.errors else {}).get("message") or "worker runtime failed"))
            output = state.output or ""
            result = _json_object(output)
            if result is None:
                result = {"answer": output.strip()} if output.strip() else {}
            return self._result(envelope, "succeeded", result=result, runtime_run_id=runtime_run_id)
        except WorkerExecutionCancelled as exc:
            return self._result(
                envelope, "cancelled", error=str(exc),
                error_category=RuntimeErrorCategory.TERMINAL.value,
                runtime_run_id=runtime_run_id,
            )
        except asyncio.CancelledError:
            # The durable orchestrator may also cancel its local gather after it
            # observes the control-plane status. Always translate that process-
            # local cancellation through the unified Runtime boundary first so
            # the RuntimeRun and event ledger cannot remain "running".
            if service is not None and runtime_run_id:
                await self._cancel_runtime(service, envelope, runtime_run_id)
            return self._result(
                envelope, "cancelled", error="worker cancelled by orchestration",
                error_category=RuntimeErrorCategory.TERMINAL.value,
                runtime_run_id=runtime_run_id,
            )
        except Exception as exc:
            return self._result(
                envelope, "failed", error=str(exc),
                error_category=classify_error(exc).value,
                runtime_run_id=runtime_run_id,
            )

    async def _execute_declared_tool(self, envelope: TaskEnvelope, spec: WorkerSpec) -> Any:
        raw = envelope.payload.get("tool_call")
        if raw is None:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("arguments", {}), dict):
            raise RuntimeFailure(RuntimeErrorCategory.VALIDATION, "tool_call must be an object with object arguments")
        name = str(raw.get("name") or "")
        tool = self.tools.get(name)
        if tool is None:
            raise RuntimeFailure(RuntimeErrorCategory.PERMISSION, "tool is not in the trusted server registry")
        arguments = dict(raw.get("arguments") or {})
        # Empty grants mean deny-all; never widen them by falling back to the
        # worker's requested tool list.
        grants = self.user_grants
        orchestrator_grants = self.orchestrator_grants
        authorization = self.authorizations.get(envelope.task_id, {})
        allowed, reason = MultiAgentRepository(self.db).authorize_tool(
            self.scope,
            worker_id=spec.worker_id,
            tool_name=name,
            parameters=arguments,
            policy=ToolPolicy("auto" if tool.access == "read" else "confirm", tool.required_scope),
            user_grant=grants,
            orchestrator_grant=orchestrator_grants,
            worker_grant=set(spec.allowed_tools),
            current_authorization=set(spec.allowed_tools),
            ticket_id=authorization.get("ticket_id"),
            ticket_nonce=authorization.get("nonce"),
            resource_version=authorization.get("resource_version", "current"),
        )
        if not allowed:
            if reason == "confirmation_required":
                return self._result(envelope, "paused", error=reason, error_category=RuntimeErrorCategory.PERMISSION.value)
            raise RuntimeFailure(RuntimeErrorCategory.PERMISSION, reason)
        await self._raise_if_cancelled()
        try:
            value = tool.invoke(self.db, arguments)
            if not hasattr(value, "__await__"):
                # A synchronous tool cannot be interrupted while it owns the
                # thread. Check immediately afterwards and prevent all later
                # worker steps when a remote process cancelled the run.
                await self._raise_if_cancelled(
                    side_effects_started=tool.access == "write",
                    tool_name=name,
                )
                return value
            return await self._await_with_cancellation(
                value,
                side_effects_started=tool.access == "write",
                tool_name=name,
            )
        except WorkerExecutionCancelled:
            raise
        except Exception as exc:
            category = RuntimeErrorCategory.SIDE_EFFECT if tool.access == "write" else classify_error(exc)
            raise RuntimeFailure(category, str(exc)) from exc

    async def _is_cancelled(self) -> bool:
        if self.cancellation_probe is None:
            return False
        observed = self.cancellation_probe()
        return bool(await observed) if hasattr(observed, "__await__") else bool(observed)

    async def _raise_if_cancelled(
        self,
        *,
        side_effects_started: bool = False,
        tool_name: str = "",
    ) -> None:
        if not await self._is_cancelled():
            return
        if side_effects_started:
            self._audit_side_effect_cancellation(tool_name)
        raise WorkerExecutionCancelled(
            "orchestration cancelled by persistent control plane",
            side_effects_started=side_effects_started,
        )

    async def _await_with_cancellation(
        self,
        awaitable: Awaitable[Any],
        *,
        on_cancel: Callable[[], Awaitable[None]] | None = None,
        side_effects_started: bool = False,
        tool_name: str = "",
    ) -> Any:
        """Race an I/O await against a durable cancellation probe.

        This is cooperative cancellation: synchronous calls remain a safety
        boundary, while model/network and async tool waits are interrupted as
        soon as another process persists ``status=cancelled``.
        """
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.cancellation_poll_seconds)
                if done:
                    break
                if not await self._is_cancelled():
                    continue
                if side_effects_started:
                    self._audit_side_effect_cancellation(tool_name)
                if on_cancel is not None:
                    await on_cancel()
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise WorkerExecutionCancelled(
                    "orchestration cancelled by persistent control plane",
                    side_effects_started=side_effects_started,
                )
            return await task
        except asyncio.CancelledError:
            if on_cancel is not None:
                await on_cancel()
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise

    async def _cancel_runtime(
        self,
        service: AgentRuntimeService,
        envelope: TaskEnvelope,
        runtime_run_id: str,
    ) -> None:
        service.cancel(
            run_id=runtime_run_id,
            workspace_id=envelope.workspace_id,
            user_id=envelope.user_id,
        )
        await service.wait(run_id=runtime_run_id, workspace_id=envelope.workspace_id)

    def _audit_side_effect_cancellation(self, tool_name: str) -> None:
        MultiAgentRepository(self.db).append_audit(
            self.scope,
            action="worker.cancelled_after_side_effect_started",
            decision="cancelled",
            details={
                "tool_name": tool_name,
                "side_effect_status": "unknown_not_rolled_back",
                "subsequent_steps": "blocked",
            },
        )

    @staticmethod
    def _result(
        envelope: TaskEnvelope,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
        error_category: str = "",
        runtime_run_id: str = "",
    ) -> ResultEnvelope:
        return ResultEnvelope(
            envelope_id=str(uuid.uuid4()), task_id=envelope.task_id,
            worker_id=envelope.worker_id, status=status, result=result or {},
            error=error, error_category=error_category, runtime_run_id=runtime_run_id,
            run_id=envelope.run_id, workspace_id=envelope.workspace_id,
            user_id=envelope.user_id, agent_id=envelope.agent_id,
        )
