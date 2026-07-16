"use client";
// Runtime layer for the Planner page (change: refactor-planner-page-state).
//
// Single source of truth for multi-session runtime: the per-conversation runtime
// Map, WebSocket connection lifecycle, connection recycling, working-set +
// concurrency gate, sessionStorage persistence, and the active-runtime → view
// projection. Decision logic lives in ./runtime-core (pure, unit-tested); this
// hook wires React state + real sockets on top. Behavior is byte-for-byte
// equivalent to the inline implementation it replaces (spec
// conversational-agent-builder: isolation, routing by cid, no recycle mid-gen).
import { useState, useRef, useEffect, useCallback } from "react";
import { toast } from "sonner";
import { getWsBase } from "@/lib/runtime-env";
import { stripMemoryUpdate } from "@/lib/sanitize";
import type { PlanFileItem } from "@/components/planner/PlanFileViewer";
import type { AttachmentMeta } from "@/lib/types";
import {
  type PlannerStage,
  type Message,
  type Proposal,
  type PlannerMemory,
  type SessionRuntime,
  type A2UIRequestData,
  type ActivityItem,
  type RunEvent,
  type ProposalResult,
  type DraftAgent,
  type CompileResult,
  type PlanState,
  blankRuntime,
  reduceWsEvent,
} from "@/lib/planner-session";
import {
  computeWorkingIds,
  workingIdsChanged,
  selectRecyclableConnections,
  saveSession,
  MAX_LIVE_CONNECTIONS,
} from "./runtime-core";

// The active-runtime projection consumed by the view layer.
export interface PlannerRuntimeView {
  messages: Message[];
  streaming: string;
  thinkingContent: string;
  thinking: boolean;
  activities: ActivityItem[];
  connected: boolean;
  proposal: Proposal | null;
  stage: PlannerStage;
  memory: PlannerMemory | null;
  files: PlanFileItem[];
  finalSummary: string | null;
  a2uiRequest: A2UIRequestData | null;
  pendingAttachments: AttachmentMeta[];
  selectedSkills: string[];
  linkedAgentId: number | null;
  mode: "create" | "replan";
  replanContext: string;
  selectedModel: string;
  // Plan-first builder loop (T7)
  runEvents: RunEvent[];
  proposalResult: ProposalResult | null;
  selectedProposalOption: string | null;
  draftAgent: DraftAgent | null;
  compileResult: CompileResult | null;
  // Plan+Loop mode (planner-plan-loop-refactor)
  planState: PlanState | null;
}

export interface PlannerRuntimeStore {
  // View projection (active runtime).
  view: PlannerRuntimeView;
  // Runtime-management state.
  activeConvId: string | null;
  workingConvIds: string[];
  sessionsRefreshKey: number;
  skillsRefreshKey: number;
  // Refs (read inside ws/async closures where state would be stale).
  runtimeRef: React.MutableRefObject<Map<string, SessionRuntime>>;
  activeConvIdRef: React.MutableRefObject<string | null>;
  workingConvIdsRef: React.MutableRefObject<string[]>;
  // Operations.
  getOrCreateRuntime: (cid: string) => SessionRuntime;
  connectWs: (convId: string) => void;
  syncViewFromRuntime: (rt: SessionRuntime) => void;
  projectIfActive: (cid: string) => void;
  recomputeWorking: () => void;
  enforceConnectionCap: () => void;
  setActive: (cid: string) => void;
  syncConvIdToUrl: (conversationId: string) => void;
  bumpSessions: () => void;
  bumpSkills: () => void;
  // Granular view setters used by actions for targeted partial updates (these
  // mirror the original inline setState calls — kept granular for behavior parity).
  setSelectedModel: (model: string) => void;
  setSelectedSkills: (skills: string[]) => void;
  setPendingAttachments: (atts: AttachmentMeta[]) => void;
  setLinkedAgentId: (id: number | null) => void;
  setStage: (stage: PlannerStage) => void;
}

export function usePlannerRuntime(): PlannerRuntimeStore {
  const [messages, setMessages] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState("");
  const [thinkingContent, setThinkingContent] = useState("");
  const [thinking, setThinking] = useState(false);
  const [activities, setActivities] = useState<ActivityItem[]>([]);
  const [connected, setConnected] = useState(false);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [stage, setStage] = useState<PlannerStage>("clarifying");
  const [selectedModel, setSelectedModel] = useState("qwen3.6-27b");
  const [memory, setMemory] = useState<PlannerMemory | null>(null);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [sessionsRefreshKey, setSessionsRefreshKey] = useState(0);
  const [skillsRefreshKey, setSkillsRefreshKey] = useState(0);
  const [files, setFiles] = useState<PlanFileItem[]>([]);
  const [finalSummary, setFinalSummary] = useState<string | null>(null);
  const [a2uiRequest, setA2uiRequest] = useState<A2UIRequestData | null>(null);
  const [pendingAttachments, setPendingAttachments] = useState<AttachmentMeta[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [linkedAgentId, setLinkedAgentId] = useState<number | null>(null);
  const [mode, setMode] = useState<"create" | "replan">("create");
  const [replanContext, setReplanContext] = useState("");
  const [workingConvIds, setWorkingConvIds] = useState<string[]>([]);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);
  const [proposalResult, setProposalResult] = useState<ProposalResult | null>(null);
  const [selectedProposalOption, setSelectedProposalOption] = useState<string | null>(null);
  const [draftAgent, setDraftAgent] = useState<DraftAgent | null>(null);
  const [compileResult, setCompileResult] = useState<CompileResult | null>(null);
  const [planState, setPlanState] = useState<PlanState | null>(null);

  // Per-conversation runtime store. The visible useState above is a projection of
  // the *active* runtime; non-active sessions accumulate silently into their own
  // runtime (and WebSocket) without touching the active view.
  const runtimeRef = useRef<Map<string, SessionRuntime>>(new Map());
  // activeConvId mirror readable inside WS closures (state would be stale there).
  const activeConvIdRef = useRef<string | null>(null);
  useEffect(() => { activeConvIdRef.current = activeConvId; }, [activeConvId]);
  // ref mirror so closures (WS handlers, send) read the latest working set.
  const workingConvIdsRef = useRef<string[]>([]);

  const getOrCreateRuntime = useCallback((cid: string): SessionRuntime => {
    let rt = runtimeRef.current.get(cid);
    if (!rt) {
      rt = blankRuntime(cid);
      rt.lastActivity = Date.now();
      runtimeRef.current.set(cid, rt);
    }
    return rt;
  }, []);

  // Push a runtime's state into the visible useState. Only ever called for the
  // active conversation so unrelated sessions can't repaint the active view.
  const syncViewFromRuntime = useCallback((rt: SessionRuntime) => {
    setMessages(rt.messages);
    setStreaming(stripMemoryUpdate(rt.streamBuf));
    setThinkingContent(rt.thinkBuf);
    setThinking(rt.thinking);
    setActivities(rt.activities);
    setConnected(rt.connected);
    setProposal(rt.proposal);
    setStage(rt.stage);
    setMemory(rt.memory);
    setFiles(rt.files);
    setFinalSummary(rt.finalSummary);
    setA2uiRequest(rt.a2uiRequest);
    setPendingAttachments(rt.pendingAttachments);
    setSelectedSkills(rt.selectedSkills);
    setLinkedAgentId(rt.linkedAgentId);
    setMode(rt.mode);
    setReplanContext(rt.replanContext);
    setRunEvents(rt.runEvents);
    setProposalResult(rt.proposalResult);
    setSelectedProposalOption(rt.selectedProposalOption);
    setDraftAgent(rt.draftAgent);
    setCompileResult(rt.compileResult);
    setPlanState(rt.planState);
    if (rt.model) setSelectedModel(rt.model);
  }, []);

  // Mirror runtime → active view if (and only if) it is the focused session.
  const projectIfActive = useCallback((cid: string) => {
    if (cid === activeConvIdRef.current) {
      const rt = runtimeRef.current.get(cid);
      if (rt) syncViewFromRuntime(rt);
    }
  }, [syncViewFromRuntime]);

  const recomputeWorking = useCallback(() => {
    const ids = computeWorkingIds(runtimeRef.current.values());
    const prev = workingConvIdsRef.current;
    const changed = workingIdsChanged(ids, prev);
    workingConvIdsRef.current = ids;
    if (changed) setWorkingConvIds(ids);
  }, []);

  // Close & recycle the least-recently-active idle connection(s) when the live
  // socket count exceeds the soft cap (design D5). Sessions mid-generation are
  // never recycled; recycled ones reconnect lazily on next focus.
  const enforceConnectionCap = useCallback(() => {
    const live = Array.from(runtimeRef.current.values()).filter(
      (r) => r.ws && r.ws.readyState === WebSocket.OPEN
    );
    const toRecycle = selectRecyclableConnections(live, activeConvIdRef.current);
    for (const r of toRecycle) {
      try { r.ws?.close(); } catch { /* ignore */ }
      r.ws = null;
      r.connected = false;
      r.needsReconnect = true;
    }
  }, []);

  // Connect a WebSocket for a conversation. The connection and all derived state
  // live in that conversation's runtime; the onmessage handler captures its own
  // `cid` so events are always routed to the originating session — never to
  // whatever session happens to be focused.
  const connectWs = useCallback((convId: string) => {
    const rt = getOrCreateRuntime(convId);
    // Avoid duplicate sockets for the same session.
    if (rt.ws && (rt.ws.readyState === WebSocket.OPEN || rt.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const ws = new WebSocket(`${getWsBase()}/api/planner/ws/${convId}`);
    rt.ws = ws;
    rt.needsReconnect = false;

    ws.onopen = () => {
      const r = runtimeRef.current.get(convId);
      if (!r || r.ws !== ws) return;
      r.connected = true;
      projectIfActive(convId);
    };
    ws.onclose = () => {
      const r = runtimeRef.current.get(convId);
      if (!r || r.ws !== ws) return;
      r.connected = false;
      projectIfActive(convId);
    };
    ws.onerror = () => {
      const r = runtimeRef.current.get(convId);
      if (!r || r.ws !== ws) return;
      r.connected = false;
      projectIfActive(convId);
      if (convId === activeConvIdRef.current) {
        toast.error("连接失败，请确认后端服务已启动");
      }
    };

    ws.onmessage = (event) => {
      // Route strictly by the captured cid; a stale/recycled runtime is dropped.
      const r = runtimeRef.current.get(convId);
      if (!r) return;
      let msg: import("@/lib/planner-session").PlannerWsEvent;
      try { msg = JSON.parse(event.data); } catch { return; }

      // Pure reduce into the *owning* runtime, then run the returned effects.
      // The active view repaints only when this event's session is focused.
      const eff = reduceWsEvent(r, msg);
      if (eff.refreshSessions) setSessionsRefreshKey((k) => k + 1);
      if (eff.refreshSkills) setSkillsRefreshKey((k) => k + 1);
      if (eff.toast && convId === activeConvIdRef.current) toast.error(eff.toast);
      if (eff.changed) projectIfActive(convId);
      // Working state may have flipped (thinking/stream started or ended).
      recomputeWorking();
    };
  }, [getOrCreateRuntime, projectIfActive, recomputeWorking]);

  // Set the active conversation (state + ref mirror).
  const setActive = useCallback((cid: string) => {
    setActiveConvId(cid);
    activeConvIdRef.current = cid;
  }, []);

  // Mirror the active conversation_id into the URL so a hard refresh always
  // resumes the same session via the existing ?session=<cid> restore path.
  // Uses history.replaceState (not router.replace) to avoid retriggering the
  // start useEffect — we want pure URL synchronization, no remount.
  const syncConvIdToUrl = useCallback((conversationId: string) => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (url.searchParams.get("session") === conversationId) return;
    url.searchParams.set("session", conversationId);
    url.searchParams.delete("from_agent");
    window.history.replaceState(null, "", url.toString());
  }, []);

  const bumpSessions = useCallback(() => setSessionsRefreshKey((k) => k + 1), []);
  const bumpSkills = useCallback(() => setSkillsRefreshKey((k) => k + 1), []);

  // Close every live connection on unmount (design 3.5).
  useEffect(() => {
    const store = runtimeRef.current;
    return () => {
      for (const rt of store.values()) {
        try { rt.ws?.close(); } catch { /* ignore */ }
        rt.ws = null;
      }
    };
  }, []);

  // Persist the active session to the legacy sessionStorage cache on change.
  useEffect(() => {
    if (!activeConvId) return;
    saveSession({
      conversationId: activeConvId,
      messages,
      stage,
      selectedModel,
      proposal,
      memorySnapshot: memory,
      updatedAt: Date.now(),
    });
  }, [activeConvId, messages, stage, selectedModel, proposal, memory]);

  return {
    view: {
      messages, streaming, thinkingContent, thinking, activities, connected,
      proposal, stage, memory, files, finalSummary, a2uiRequest,
      pendingAttachments, selectedSkills, linkedAgentId, mode, replanContext,
      selectedModel,
      runEvents, proposalResult, selectedProposalOption, draftAgent, compileResult,
      planState,
    },
    activeConvId,
    workingConvIds,
    sessionsRefreshKey,
    skillsRefreshKey,
    runtimeRef,
    activeConvIdRef,
    workingConvIdsRef,
    getOrCreateRuntime,
    connectWs,
    syncViewFromRuntime,
    projectIfActive,
    recomputeWorking,
    enforceConnectionCap,
    setActive,
    syncConvIdToUrl,
    bumpSessions,
    bumpSkills,
    setSelectedModel,
    setSelectedSkills,
    setPendingAttachments,
    setLinkedAgentId,
    setStage,
  };
}
