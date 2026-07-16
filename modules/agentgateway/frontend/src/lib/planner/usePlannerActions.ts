"use client";
// Action layer for the Planner page (change: refactor-planner-page-state).
//
// Business actions (start/open/switch/send/apply/replan/model/skill/a2ui/delete/
// attachments) built on top of usePlannerRuntime. Every action reads/writes
// session state THROUGH the runtime store — no bare WebSocket or sessionStorage
// access here (that's the runtime layer's job). Logic is moved verbatim from the
// former inline handlers in page.tsx; behavior is preserved exactly.
//
// Action handlers are intentionally NOT memoized (recreated each render, like the
// originals) so they always close over fresh state; the runtime ops they call are
// ref-backed and stable.
import { useState } from "react";
import type { useRouter } from "next/navigation";
import { toast } from "sonner";
import { getApiBase } from "@/lib/runtime-env";
import { stripMemoryUpdate } from "@/lib/sanitize";
import { api } from "@/lib/api";
import type { PlanFileItem } from "@/components/planner/PlanFileViewer";
import type { AttachmentMeta } from "@/lib/types";
import { type Message, type PlannerStage, type PlannerMemory, isAdvanceChoice } from "@/lib/planner-session";
import { clearSession, loadSession, canStartNewWorking } from "./runtime-core";
import type { PlannerRuntimeStore } from "./usePlannerRuntime";

type Router = ReturnType<typeof useRouter>;

interface ActionsConfig {
  runtime: PlannerRuntimeStore;
  router: Router;
  models: Array<{ model_id: string; display_name: string; provider: string }>;
}

export interface PlannerActions {
  // Action-transient UI state (driven by the actions themselves).
  applying: boolean;
  deleteTarget: { cid: string; title: string } | null;
  setDeleteTarget: (t: { cid: string; title: string } | null) => void;
  replanLandingOpen: boolean;
  setReplanLandingOpen: (open: boolean) => void;
  // Session lifecycle.
  startNewSession: (cancelled?: { current: boolean }) => Promise<boolean>;
  openExistingSession: (conversationId: string, cancelled?: { current: boolean }) => Promise<boolean>;
  switchToSession: (conversationId: string) => void;
  restoreFromCache: () => boolean;
  // Turn dispatch.
  sendPlannerTurn: (content: string, atts?: AttachmentMeta[], capabilityContext?: string) => boolean;
  // Per-session controls.
  handleModelChange: (modelId: string) => void;
  handleSkillToggle: (skillName: string, attach: boolean) => void;
  handleA2UISubmit: (choiceId: string, freeText?: string) => void;
  handleAttachmentsChange: (atts: AttachmentMeta[]) => void;
  // Apply / replan / navigation.
  handleApply: () => Promise<void>;
  handleConfirmProposal: (proposalId: number) => Promise<void>;
  handleCompileDraft: (draftAgentId: number) => Promise<void>;
  handleReplanApply: (landing: "save_new_version" | "override_draft" | "partial") => Promise<void>;
  handleOpenDag: () => void;
  handleDeleteSession: () => Promise<void>;
}

// IMPL_BODY
export function usePlannerActions({ runtime, router, models }: ActionsConfig): PlannerActions {
  const [applying, setApplying] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ cid: string; title: string } | null>(null);
  const [replanLandingOpen, setReplanLandingOpen] = useState(false);

  const {
    runtimeRef, activeConvIdRef, workingConvIdsRef,
    getOrCreateRuntime, connectWs, syncViewFromRuntime, recomputeWorking,
    enforceConnectionCap, setActive, syncConvIdToUrl, bumpSessions,
    setSelectedModel, setSelectedSkills, setPendingAttachments, setLinkedAgentId, setStage,
  } = runtime;

  // Synchronous legacy local-cache hydrate. Returns true iff it opened a session.
  // Injected into the bootstrap orchestrator (runPlannerBootstrap).
  const restoreFromCache = (): boolean => {
    const saved = loadSession();
    if (!saved || !saved.conversationId) return false;
    const rt = getOrCreateRuntime(saved.conversationId);
    rt.messages = saved.messages;
    rt.stage = saved.stage;
    rt.proposal = saved.proposal;
    rt.memory = saved.memorySnapshot;
    // Seed the monotonic counter past the largest restored id — using length
    // would collide when ids are sparse (e.g. trimmed to last 50).
    rt.msgId = saved.messages.reduce((max, m) => Math.max(max, m.id), 0);
    if (saved.selectedModel) {
      rt.model = saved.selectedModel;
      setSelectedModel(saved.selectedModel);
    }
    setActive(saved.conversationId);
    syncViewFromRuntime(rt);
    connectWs(saved.conversationId);
    return true;
  };

  // Open an existing session by conversation_id: fetch its snapshot then connect WS.
  // If a runtime already exists (e.g. revisiting a backgrounded session), we keep its
  // accumulated state and only (re)connect if needed instead of clobbering it.
  const openExistingSession = async (
    conversationId: string,
    cancelled: { current: boolean } = { current: false },
  ): Promise<boolean> => {
    const existing = runtimeRef.current.get(conversationId);
    if (existing && existing.messages.length > 0) {
      // Already hydrated in this page session — just focus it and reconnect if dropped.
      if (cancelled.current) return false;
      setActive(conversationId);
      syncConvIdToUrl(conversationId);
      syncViewFromRuntime(existing);
      if (!existing.ws || existing.ws.readyState > WebSocket.OPEN || existing.needsReconnect) {
        connectWs(conversationId);
      }
      enforceConnectionCap();
      return true;
    }
    try {
      const detail = await api.getPlannerSession(conversationId);
      if (cancelled.current) return false;
      const rt = getOrCreateRuntime(conversationId);
      // Monotonic ids from rt.msgId (D1): a runtime reaching here has no messages yet
      // (the existing-with-messages case returned above), so this seeds 1..n and any
      // later increment/hydrate stays unique.
      const hydrated: Message[] = (detail.messages || []).map((m) => ({
        id: ++rt.msgId,
        role: m.role === "user" ? "user" : "assistant",
        content: stripMemoryUpdate(m.content || ""),
      }));
      rt.messages = hydrated;
      rt.stage = (detail.stage || "clarifying") as PlannerStage;
      rt.memory = detail.memory;
      rt.proposal = null;
      rt.files = (detail.file_artifacts || []) as PlanFileItem[];
      rt.finalSummary = null;
      rt.linkedAgentId = detail.linked_agent_id ?? null;
      rt.mode = (detail.mode === "replan" ? "replan" : "create");
      rt.replanContext = detail.replan_context || "";
      rt.selectedSkills = (detail.memory as PlannerMemory & { selected_skills?: string[] })?.selected_skills || [];
      setActive(conversationId);
      syncConvIdToUrl(conversationId);
      syncViewFromRuntime(rt);
      connectWs(conversationId);
      enforceConnectionCap();
      return true;
    } catch (e) {
      if (!cancelled.current) toast.error((e as Error)?.message || "恢复会话失败");
      return false;
    }
  };

  // User picked another session in the sidebar. Never tears down the previous
  // session's connection or in-flight generation — just refocuses.
  const switchToSession = (conversationId: string) => {
    if (conversationId === activeConvIdRef.current) return;
    void openExistingSession(conversationId);
  };

  // User clicked "新建". Starts a fresh session in its own runtime; existing
  // sessions keep running in the background (subject to the connection cap).
  // `cancelled` lets the one-shot bootstrap effect abort a stale start (design
  // D3/D4). Returns true when a session was actually opened. Default sentinel
  // never cancels (manual "新建").
  const startNewSession = async (
    cancelled: { current: boolean } = { current: false },
  ): Promise<boolean> => {
    clearSession();
    try {
      const res = await fetch(`${getApiBase()}/api/planner/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: runtime.view.selectedModel }),
      });
      const data = await res.json();
      if (cancelled.current) return false;
      const convId = data.conversation_id;
      const rt = getOrCreateRuntime(convId);
      rt.stage = "clarifying";
      rt.model = runtime.view.selectedModel;
      setActive(convId);
      syncConvIdToUrl(convId);
      syncViewFromRuntime(rt);
      connectWs(convId);
      enforceConnectionCap();
      bumpSessions();
      return true;
    } catch {
      if (!cancelled.current) toast.error("无法启动新会话");
      return false;
    }
  };

  // ACTIONS_PART2

  // Send one planner turn (a user message) on the active session and prime the
  // runtime for the response. Shared by the composer and the A2UI auto-continue
  // path so both go through the same concurrency gate + reset. Returns true when
  // the turn was actually dispatched.
  const sendPlannerTurn = (content: string, atts: AttachmentMeta[] = [], capabilityContext = ""): boolean => {
    const cid = activeConvIdRef.current;
    if (!cid) return false;
    const rt = runtimeRef.current.get(cid);
    if (!rt || !rt.ws || rt.ws.readyState !== WebSocket.OPEN) return false;
    if (!content.trim() && atts.length === 0) return false;
    // Concurrency gate (design D3): block a NEW working session when the cap is
    // already reached. A session already working doesn't count against itself.
    if (!canStartNewWorking(cid, workingConvIdsRef.current)) {
      toast.error("无法 3 路以上同时对话，请等待其他会话完成");
      return false;
    }
    const plannerContent = capabilityContext ? `${content}\n\n${capabilityContext}` : content;
    rt.ws.send(JSON.stringify({ type: "message", content: plannerContent, attachments: atts }));
    rt.msgId++;
    rt.messages = [...rt.messages, { id: rt.msgId, role: "user", content, attachments: atts.length ? atts : undefined }];
    rt.thinking = true;
    rt.thinkBuf = "";
    rt.streamBuf = "";
    rt.activities = [];
    rt.runEvents = [];
    rt.pendingTriggeredSkills = null;
    rt.pendingAttachments = [];
    rt.lastActivity = Date.now();
    syncViewFromRuntime(rt);
    recomputeWorking();
    return true;
  };

  // Switch the model for the CURRENT session only. Updates the view + the session
  // runtime and tells the backend via set_model; the change takes effect on the
  // next turn. Never starts a new session (design D2).
  const handleModelChange = (modelId: string) => {
    setSelectedModel(modelId);
    const cid = activeConvIdRef.current;
    const rt = cid ? runtimeRef.current.get(cid) : null;
    if (rt) {
      rt.model = modelId;
      if (rt.ws && rt.ws.readyState === WebSocket.OPEN) {
        rt.ws.send(JSON.stringify({ type: "set_model", model_id: modelId }));
      }
    }
    const label = models.find((m) => m.model_id === modelId)?.display_name || modelId;
    toast.success(`已切换到 ${label}，下一轮生效`);
  };

  const handleSkillToggle = (skillName: string, attach: boolean) => {
    const cid = activeConvIdRef.current;
    const rt = cid ? runtimeRef.current.get(cid) : null;
    if (!rt || !rt.ws || rt.ws.readyState !== WebSocket.OPEN) {
      toast.error("会话未连接，无法挂载技能");
      return;
    }
    rt.ws.send(JSON.stringify({
      type: attach ? "skill_attached" : "skill_detached",
      skill_name: skillName,
    }));
    // Optimistic update; the ack will reconcile.
    rt.selectedSkills = attach
      ? Array.from(new Set([...rt.selectedSkills, skillName]))
      : rt.selectedSkills.filter((s) => s !== skillName);
    setSelectedSkills(rt.selectedSkills);
  };

  const handleA2UISubmit = (choiceId: string, freeText?: string) => {
    const cid = activeConvIdRef.current;
    const rt = cid ? runtimeRef.current.get(cid) : null;
    if (!rt || !rt.a2uiRequest || !rt.ws || rt.ws.readyState !== WebSocket.OPEN) return;
    const optionLabel = rt.a2uiRequest.options.find((o) => o.id === choiceId)?.label || choiceId;
    rt.ws.send(JSON.stringify({
      type: "a2ui_response",
      id: rt.a2uiRequest.id,
      choice: choiceId,
      free_text: freeText,
    }));
    rt.a2uiRequest = null;

    const userLine = freeText ? `已选择「${optionLabel}」：${freeText}` : `已选择「${optionLabel}」`;

    if (isAdvanceChoice(choiceId, optionLabel)) {
      // Proceed intent: continue the planner turn right away so the proposal /
      // JSON gets generated without forcing the user to type "创建吧".
      syncViewFromRuntime(rt); // clear the card first
      sendPlannerTurn(userLine);
      return;
    }

    // Non-advance choice (e.g. "我要修改"): record only and let the user follow
    // up — optimistically render the choice so the trail is visible.
    rt.msgId++;
    rt.messages = [...rt.messages, { id: rt.msgId, role: "user", content: userLine }];
    syncViewFromRuntime(rt);
  };

  // Keep pending attachments on the active runtime (so a session switch preserves them).
  const handleAttachmentsChange = (atts: AttachmentMeta[]) => {
    const cid = activeConvIdRef.current;
    const rt = cid ? runtimeRef.current.get(cid) : null;
    if (rt) rt.pendingAttachments = atts;
    setPendingAttachments(atts);
  };

  // ACTIONS_PART3

  const handleApply = async () => {
    // Replan sessions land onto the existing linked agent (override / new version
    // / partial), driven by a dedicated dialog — not the create path.
    if (runtime.view.mode === "replan") {
      setReplanLandingOpen(true);
      return;
    }
    const cid = activeConvIdRef.current;
    const proposal = runtime.view.proposal;
    if (!proposal) return;
    setApplying(true);
    try {
      const agentName = proposal.nodes.find((n) => n.type === "agent")?.config?.role_name as string || "新建智能体";
      const res = await fetch(`${getApiBase()}/api/planner/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proposal, agent_name: agentName, memory: runtime.view.memory || {}, conversation_id: cid || null }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail = body?.detail;
        if (detail && typeof detail === "object" && detail.error === "incomplete_proposal") {
          const missing = (detail.missing as Array<{ node_id: string; keys: string[] }>) || [];
          const summary = missing
            .map((m) => `${m.node_id}: ${m.keys.join(", ")}`)
            .join("；");
          toast.error(`方案缺少必填字段，无法创建：${summary}`);
        } else {
          toast.error(typeof detail === "string" ? detail : "创建失败");
        }
        setApplying(false);
        return;
      }
      const data = await res.json();
      // Mark this session applied + linked so revisiting it shows the "open DAG" entry.
      if (cid) {
        const rt = runtimeRef.current.get(cid);
        if (rt) {
          rt.linkedAgentId = data.id;
          rt.stage = "applied";
          if (cid === activeConvIdRef.current) {
            setLinkedAgentId(data.id);
            setStage("applied");
          }
        }
      }
      clearSession();
      router.push(`/workbench/${data.id}`);
    } catch {
      toast.error("创建失败");
      setApplying(false);
    }
  };

  // ── Plan-first builder loop (T7) ──
  // Confirm the first-class proposal (T5) → backend derives a draft agent;
  // stash it on the runtime so the panel shows the draft + compile entry.
  const handleConfirmProposal = async (proposalId: number) => {
    const cid = activeConvIdRef.current;
    try {
      const view = await api.transitionProposal(proposalId, "confirm");
      const draft = (view?.draft_agent as Record<string, unknown> | null) || null;
      if (cid) {
        const rt = runtimeRef.current.get(cid);
        if (rt) {
          rt.selectedProposalOption = String(proposalId);
          rt.draftAgent = (draft as never) || rt.draftAgent;
          if (cid === activeConvIdRef.current) syncViewFromRuntime(rt);
        }
      }
      if (!draft) toast.error("方案已确认，但未生成草案");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "确认方案失败");
    }
  };

  // Compile + dry-run a draft agent (T6); stash the structured result.
  const handleCompileDraft = async (draftAgentId: number) => {
    const cid = activeConvIdRef.current;
    try {
      const result = await api.compileDraftAgent(draftAgentId, true);
      if (cid) {
        const rt = runtimeRef.current.get(cid);
        if (rt) {
          rt.compileResult = result as never;
          if (cid === activeConvIdRef.current) syncViewFromRuntime(rt);
        }
      }
      if (!result.success) {
        toast.error(`编译未通过：${result.errors.join("；") || "见详情"}`);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "编译失败");
    }
  };

  // Land a Replan proposal onto the existing agent via one of three modes.
  const handleReplanApply = async (landing: "save_new_version" | "override_draft" | "partial") => {
    const cid = activeConvIdRef.current;
    const proposal = runtime.view.proposal;
    const linkedAgentId = runtime.view.linkedAgentId;
    if (!proposal || linkedAgentId == null) return;
    setReplanLandingOpen(false);
    setApplying(true);
    try {
      const data = await api.applyReplan({
        agent_id: linkedAgentId,
        proposal: proposal as unknown as Record<string, unknown>,
        memory: (runtime.view.memory || {}) as unknown as Record<string, unknown>,
        conversation_id: cid || null,
        landing,
        selected_node_ids: landing === "partial" ? (proposal.diff?.added || []).concat(proposal.diff?.removed || []) : [],
      });
      if (cid) {
        const rt = runtimeRef.current.get(cid);
        if (rt) {
          rt.linkedAgentId = data.id;
          rt.stage = "applied";
          if (cid === activeConvIdRef.current) {
            setLinkedAgentId(data.id);
            setStage("applied");
          }
        }
      }
      toast.success(`已落地（v${data.version}）`);
      clearSession();
      router.push(`/workbench/${data.id}`);
    } catch (e) {
      const detail = (e as Error)?.message || "";
      toast.error(detail.includes("incomplete_proposal") ? "方案缺少必填字段，无法落地" : "落地失败");
      setApplying(false);
    }
  };

  // Open the DAG / workbench page linked to an applied session.
  const handleOpenDag = () => {
    const linkedAgentId = runtime.view.linkedAgentId;
    if (linkedAgentId != null) router.push(`/workbench/${linkedAgentId}`);
  };

  const handleDeleteSession = async () => {
    if (!deleteTarget) return;
    const { cid } = deleteTarget;
    try {
      await api.deletePlannerSession(cid);
      toast.success("会话已删除");
    } catch (e) {
      const msg = (e as Error)?.message || "";
      if (msg.includes("404") || msg.toLowerCase().includes("not found")) {
        toast.error("会话已不存在，已从列表移除");
      } else {
        toast.error("删除失败，请检查网络后重试");
      }
    } finally {
      setDeleteTarget(null);
    }
    // Tear down the deleted session's connection + runtime regardless of focus.
    const rt = runtimeRef.current.get(cid);
    if (rt) {
      try { rt.ws?.close(); } catch { /* ignore */ }
      runtimeRef.current.delete(cid);
    }
    // If we deleted the active session, fall back to another one (or start fresh).
    if (cid === activeConvIdRef.current) {
      try {
        const rows = await api.listPlannerSessions();
        const next = rows.find((r) => r.conversation_id !== cid);
        if (next) {
          switchToSession(next.conversation_id);
        } else {
          await startNewSession();
        }
      } catch {
        await startNewSession();
      }
    }
    bumpSessions();
  };

  return {
    applying,
    deleteTarget,
    setDeleteTarget,
    replanLandingOpen,
    setReplanLandingOpen,
    startNewSession,
    openExistingSession,
    switchToSession,
    restoreFromCache,
    sendPlannerTurn,
    handleModelChange,
    handleSkillToggle,
    handleA2UISubmit,
    handleAttachmentsChange,
    handleApply,
    handleConfirmProposal,
    handleCompileDraft,
    handleReplanApply,
    handleOpenDag,
    handleDeleteSession,
  };
}
