"use client";
import { Suspense, useState, useRef, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { ArrowLeft, Loader2, Send, Sparkles, ChevronUp, ChevronDown, FileText, FileJson, X, CheckCircle2, XCircle, Circle } from "lucide-react";
import { toast } from "sonner";
import PlannerPanel from "@/components/planner/PlannerPanel";
import SessionHistorySidebar from "@/components/planner/SessionHistorySidebar";
import { AttachmentPreviews, AttachmentButton } from "@/components/planner/AttachmentComposer";
import PlannerSelfSkills from "@/components/planner/PlannerSelfSkills";
import A2UIConfirmCard from "@/components/planner/A2UIConfirmCard";
import { getApiBase } from "@/lib/runtime-env";
import { isMultimodalModel } from "@/lib/model-caps";
import { type PlannerStage, runPlannerBootstrap } from "@/lib/planner-session";
import { usePlannerRuntime } from "@/lib/planner/usePlannerRuntime";
import { usePlannerActions } from "@/lib/planner/usePlannerActions";

// Per-conversation runtime (SessionRuntime) and the pure WS event reducer live in
// @/lib/planner-session; the multi-session runtime store / actions live in
// @/lib/planner (usePlannerRuntime + usePlannerActions). This file is the VIEW
// layer: it projects the active runtime into JSX and binds interactions to the
// action hook. It holds no WebSocket or sessionStorage access of its own.

const STAGE_LABELS: Record<PlannerStage, { label: string; desc: string }> = {
  clarifying: { label: "需求澄清", desc: "正在了解你的需求" },
  drafting: { label: "方案生成", desc: "正在规划架构方案" },
  awaiting_confirmation: { label: "等待确认", desc: "请确认方案是否符合预期" },
  ready_to_apply: { label: "可创建", desc: "方案已就绪，可创建到工作台" },
  applied: { label: "已应用", desc: "方案已应用到智能体" },
};

export default function PlannerPage() {
  return (
    <Suspense fallback={<div className="flex-1 p-6 max-w-5xl mx-auto w-full text-sm text-muted-foreground">正在加载规划页面...</div>}>
      <PlannerPageInner />
    </Suspense>
  );
}

function PlannerPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // ---- Pure-UI local state (not part of the multi-session runtime) ----
  const [input, setInput] = useState("");
  const [showThinkDetail, setShowThinkDetail] = useState(false);
  const [showDoneSteps, setShowDoneSteps] = useState(false);
  const [mobileShowPanel, setMobileShowPanel] = useState(false);
  const [models, setModels] = useState<Array<{ model_id: string; display_name: string; provider: string; configured: boolean }>>([]);
  const [platformCapabilities, setPlatformCapabilities] = useState<Array<{ id: number; type: string; name: string; description: string; config: string }>>([]);
  const [selectedPlatformCapabilityIds, setSelectedPlatformCapabilityIds] = useState<number[]>([]);
  // Proposal chip viewer state. Fetch happens in the click handler (not an
  // effect) to match PlanFileViewer and avoid set-state-in-effect.
  const [chatFile, setChatFile] = useState<{ name: string; content: string | null; loading: boolean; error: string | null } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const restoredRef = useRef(false);
  // Cancellation guard for the one-shot bootstrap effect (model load + first session).
  const bootCancelRef = useRef<{ current: boolean }>({ current: false });

  // ---- Runtime + action layers ----
  const runtime = usePlannerRuntime();
  const actions = usePlannerActions({ runtime, router, models });
  const { view, activeConvId, workingConvIds, sessionsRefreshKey, skillsRefreshKey } = runtime;
  const {
    messages, streaming, thinkingContent, thinking, activities, connected,
    proposal, stage, memory, files, finalSummary, a2uiRequest,
    pendingAttachments, selectedSkills, linkedAgentId, mode, replanContext,
    selectedModel,
    runEvents, proposalResult, draftAgent, compileResult,
    planState,
  } = view;

  const imageSupported = isMultimodalModel(selectedModel);
  const selectedModelConfigured = models.find((model) => model.model_id === selectedModel)?.configured ?? false;

  // Open a proposal.json chip from the chat: fetch its content then show a modal.
  const openProposalFile = async (filename: string) => {
    const cid = runtime.activeConvIdRef.current;
    if (!cid) return;
    setChatFile({ name: filename, content: null, loading: true, error: null });
    try {
      const res = await fetch(
        `${getApiBase()}/api/planner/sessions/${encodeURIComponent(cid)}/files/${encodeURIComponent(filename)}`
      );
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      setChatFile({ name: filename, content: data.content || "", loading: false, error: null });
    } catch (e) {
      setChatFile({ name: filename, content: null, loading: false, error: (e as Error)?.message || "读取失败" });
    }
  };

  const handleSend = () => {
    if (!input.trim() && pendingAttachments.length === 0) return;
    if (!selectedModelConfigured) {
      toast.error("当前模型渠道尚未配置，请先在能力库的模型配置中填写并测试渠道");
      return;
    }
    const selectedCapabilities = platformCapabilities.filter((item) => selectedPlatformCapabilityIds.includes(item.id));
    const capabilityContext = selectedCapabilities.length === 0 ? "" : [
      "[用户手动选择的本次规划能力]",
      ...selectedCapabilities.map((item) => `- ${item.type === "skill" ? "Skill" : "连接器"}: ${item.name}${item.description ? `（${item.description}）` : ""}`),
      "请仅在方案确有帮助时采用这些能力；没有选择的能力不要假定可用。",
    ].join("\n");
    if (actions.sendPlannerTurn(input, pendingAttachments, capabilityContext)) {
      setInput("");
    }
  };

  // PAGE_EFFECTS

  // One-shot bootstrap: load models + pick exactly one of four routes to open the
  // first session. Idempotent across a StrictMode double-mount (design D1/D3): the
  // latch is set ONLY after a route actually opens a session; a mount aborted
  // mid-`await` leaves it untouched so the next mount re-runs the same route
  // instead of falling through to fresh-start. Control flow lives in
  // runPlannerBootstrap so it is unit-testable byte-for-byte.
  useEffect(() => {
    const cancelled = { current: false };
    bootCancelRef.current = cancelled;

    (async () => {
      api.listModels().then((m) => {
        if (!cancelled.current) setModels(m.map((x) => ({ model_id: x.model_id, display_name: x.display_name, provider: x.provider, configured: x.configured })));
      }).catch(() => {});
      api.listCapabilities().then((items) => {
        if (!cancelled.current) setPlatformCapabilities(items.filter((item) => item.config.includes("atlas_platform")));
      }).catch(() => {});

      const sessionParam = searchParams.get("session");
      const fromAgentParam = searchParams.get("from_agent");

      await runPlannerBootstrap({
        fromAgentId: fromAgentParam ? parseInt(fromAgentParam, 10) : null,
        sessionParam,
        restored: restoredRef,
        cancelled,
        createSessionFromAgent: (agentId) => api.createSessionFromAgent(agentId),
        openExistingSession: (cid) => actions.openExistingSession(cid, cancelled),
        restoreFromCache: actions.restoreFromCache,
        startNewSession: () => actions.startNewSession(cancelled),
        onFromAgentError: (e) => toast.error((e as Error)?.message || "无法从已有智能体恢复规划会话"),
      });
    })();

    return () => {
      cancelled.current = true;
    };
  // Bootstrap runs once on mount. Model switching is handled per-session via
  // set_model (no remount), so selectedModel is intentionally NOT a dependency.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, streaming, thinkingContent]);

  // Refetch the model list when the window regains focus, so deleting/adding a
  // model in the capability library (another tab/route) is reflected here
  // instead of staying on the mount-time snapshot. Debounced to avoid spamming
  // the endpoint on rapid focus changes.
  useEffect(() => {
    let last = Date.now();
    const onFocus = () => {
      const now = Date.now();
      if (now - last < 3000) return;
      last = now;
      api.listModels()
        .then((m) => setModels(m.map((x) => ({ model_id: x.model_id, display_name: x.display_name, provider: x.provider, configured: x.configured }))))
        .catch(() => {});
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  const stageInfo = STAGE_LABELS[stage];
  const isSending = thinking || !!streaming;
  const platformSkills = platformCapabilities
    .filter((item) => item.type === "skill")
    .map((item) => ({ id: item.id, name: item.name, description: item.description }));
  const platformConnectors = platformCapabilities
    .filter((item) => item.type === "tool")
    .map((item) => {
      let connected = false;
      try { connected = Boolean(JSON.parse(item.config).connected); } catch { /* keep disconnected */ }
      return { id: item.id, name: item.name, description: item.description, connected };
    });
  const togglePlatformCapability = (id: number) => {
    setSelectedPlatformCapabilityIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  };

  return (
    <div
      className="flex flex-col overflow-hidden bg-background"
      style={{ height: "calc(100dvh - var(--app-header-height, 0px))" }}
    >
      {/* Header */}
      <header className="flex items-center gap-4 px-6 py-3 border-b shrink-0 bg-card/80 backdrop-blur-sm">
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => router.push("/workbench")}>
          <ArrowLeft className="w-4 h-4" />
        </Button>
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-primary" />
          <h1 className="text-sm font-semibold">{mode === "replan" ? "智能体重规划" : "智能体规划师"}</h1>
        </div>
        {mode === "replan" && (
          <Badge variant="secondary" className="text-[10px] px-1.5 py-0 h-5">
            重规划{linkedAgentId != null ? ` · #${linkedAgentId}` : ""}
          </Badge>
        )}
        <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-5">
          {stageInfo.label}
        </Badge>
        <div className="flex-1" />
        <div className={`w-1.5 h-1.5 rounded-full ${connected ? "bg-green-500" : "bg-red-400"}`} />
      </header>

      {/* Body: Sidebar + Chat + Panel */}
      <div className="flex-1 flex min-h-0 overflow-hidden">
        {/* Session History sidebar (desktop only) */}
        <div className="hidden md:flex">
          <SessionHistorySidebar
            activeConversationId={activeConvId}
            onSelect={actions.switchToSession}
            onNew={() => void actions.startNewSession()}
            onDelete={(cid, title) => actions.setDeleteTarget({ cid, title })}
            refreshKey={sessionsRefreshKey}
            workingConversationIds={workingConvIds}
          />
        </div>

        {/* Chat area */}
        <div className="flex-1 flex flex-col min-h-0 min-w-0">
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-6">
            <div className="max-w-3xl mx-auto space-y-5">
              {/* Replan baseline: read-only injected context (existing graph + metadata + evidence) */}
              {mode === "replan" && replanContext && (
                <ReplanContextBlock context={replanContext} />
              )}

              {/* Onboarding block */}
              {messages.length === 0 && !streaming && (
                mode === "replan" ? <ReplanOnboardingBlock /> : <OnboardingBlock />
              )}

              {messages.map((m) => (
                <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                    m.role === "user"
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted/50 border border-border/60"
                  }`}>
                    {m.attachments && m.attachments.length > 0 && (
                      <div className="flex flex-wrap gap-2 mb-2">
                        {m.attachments.map((att) => (
                          att.kind === "image" ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              key={att.path}
                              src={`${getApiBase()}${att.preview_url}`}
                              alt={att.name}
                              className="max-w-[200px] rounded"
                            />
                          ) : (
                            <span key={att.path} className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-background/20 border border-border/40">
                              <FileText className="w-3 h-3" /> {att.name}
                            </span>
                          )
                        ))}
                      </div>
                    )}
                    <MessageContent content={m.content} />
                    {m.role === "assistant" && m.triggeredSkills && m.triggeredSkills.length > 0 && (
                      <div className="mt-2 flex items-center flex-wrap gap-1.5 text-[11px] text-muted-foreground">
                        <Sparkles className="w-3 h-3 text-primary/70 shrink-0" />
                        <span>本轮触发技能：</span>
                        {m.triggeredSkills.map((s) => (
                          <span key={s} className="inline-flex items-center px-1.5 py-0.5 rounded bg-primary/10 text-primary border border-primary/20">
                            {s}
                          </span>
                        ))}
                      </div>
                    )}
                    {m.proposalFile && (
                      <button
                        onClick={() => void openProposalFile(m.proposalFile!)}
                        className="mt-2 inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-primary/10 hover:bg-primary/20 border border-primary/30 text-primary transition-colors"
                      >
                        <FileJson className="w-3.5 h-3.5" />
                        {m.proposalFile}
                      </button>
                    )}
                  </div>
                </div>
              ))}

              {/* PAGE_TIMELINE */}

              {/* Activity timeline / thinking indicator. Shows the planner's
                  multi-step actions (white-box) when the backend emits activity
                  events; falls back to the plain "思考中..." bubble otherwise.
                  While the turn is active it shows the live step list; once the
                  turn ends it folds into a "已完成 N 步" summary (expandable). */}
              {(() => {
                const turnActive = thinking || !!streaming;
                if (!turnActive && activities.length === 0 && runEvents.length === 0) return null;
                // Fine-grained run-event steps (T8) take precedence over the
                // coarse 4-phase activity list — they show the planner's actual
                // thinking/planning stages (理解需求→读取上下文→召回记忆→检索专家→
                // 匹配能力→生成方案) inline as they happen.
                // When Plan+Loop is active (planState exists), the left-side
                // PlanProgress already shows step progress — suppress the
                // run_event step list here to avoid duplication.
                const suppressRunEvents = !!planState;
                const byStep = new Map(runEvents.map((e) => [e.step, e]));
                // Group run events by logical parent phase (planner-process-stream-
                // cleanup): a parent step is a group HEADER; its sub-steps
                // (parent:child) render indented under it. The parent line itself
                // no longer repeats sub-step content. Order: 理解需求 → 读取信息 →
                // 召回记忆 → (agentic tool calls / others). Avoids the父子重复+平铺.
                const PHASES: { key: string; title: string }[] = [
                  { key: "intent_inference", title: "理解需求" },
                  { key: "context_load", title: "读取信息" },
                  { key: "memory_recall", title: "召回记忆" },
                ];
                const phaseKeys = new Set(PHASES.map((p) => p.key));
                // Friendly labels for bare dynamic steps (agentic path) that are
                // neither a fixed phase nor a tool call — e.g. the reasoning opener.
                const BARE_LABELS: Record<string, string> = {
                  reasoning: "正在分析",
                };
                // children grouped by parent prefix
                const childrenByParent = new Map<string, typeof runEvents>();
                const otherSteps: typeof runEvents = [];
                for (const ev of runEvents) {
                  const colon = ev.step.indexOf(":");
                  if (colon > 0) {
                    const parent = ev.step.slice(0, colon);
                    if (!childrenByParent.has(parent)) childrenByParent.set(parent, []);
                    childrenByParent.get(parent)!.push(ev);
                  } else if (!phaseKeys.has(ev.step)) {
                    otherSteps.push(ev); // e.g. agentic tool_call:* has colon, handled above; bare unknowns here
                  }
                }
                const statusIcon = (st?: string) =>
                  st === "completed" ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0 mt-0.5" />
                  : st === "failed" ? <XCircle className="w-3.5 h-3.5 text-red-600 shrink-0 mt-0.5" />
                  : st === "running" ? <Loader2 className="w-3.5 h-3.5 animate-spin text-amber-700 dark:text-amber-300 shrink-0 mt-0.5" />
                  : <Circle className="w-3.5 h-3.5 text-muted-foreground shrink-0 mt-0.5" />;
                // a phase is shown if its parent event OR any of its children exist
                const visiblePhases = PHASES.filter(
                  (p) => byStep.has(p.key) || (childrenByParent.get(p.key)?.length)
                );
                // agentic tool_call steps (parent "tool_call") render as their own group
                const toolCalls = childrenByParent.get("tool_call") || [];
                const hasRunEvents = !suppressRunEvents && (visiblePhases.length > 0 || toolCalls.length > 0 || otherSteps.length > 0);
                return (
                <div className="flex justify-start">
                  <div className="max-w-[85%] w-full rounded-2xl px-4 py-3 bg-amber-50 dark:bg-amber-950/30 border border-amber-200/60 dark:border-amber-800/40">
                    {hasRunEvents ? (
                      <div className="flex flex-col gap-2">
                        {/* bare dynamic steps (agentic reasoning opener, etc.) */}
                        {otherSteps.map((ev, i) => (
                          <div key={ev.event_id || `${ev.run_id}_${ev.step}_${i}`} className="flex items-start gap-2 text-xs font-medium">
                            {statusIcon(ev.status)}
                            <span className={ev.status === "completed" ? "text-foreground/80" : "text-amber-700 dark:text-amber-300"}>
                              {ev.message || BARE_LABELS[ev.step] || ev.step}
                            </span>
                          </div>
                        ))}
                        {visiblePhases.map((p) => {
                          const parentEv = byStep.get(p.key);
                          const kids = (childrenByParent.get(p.key) || []).filter((e) => e.status !== "running");
                          return (
                            <div key={p.key} className="flex flex-col gap-1">
                              {/* phase header */}
                              <div className="flex items-start gap-2 text-xs font-medium">
                                {statusIcon(parentEv?.status || (kids.length ? "completed" : "running"))}
                                <span className={(parentEv?.status === "completed" || kids.length) ? "text-foreground/80" : "text-amber-700 dark:text-amber-300"}>
                                  {p.title}
                                </span>
                              </div>
                              {/* indented sub-steps */}
                              {kids.map((ev) => (
                                <div key={ev.step} className="flex items-start gap-2 text-xs pl-5">
                                  {statusIcon(ev.status)}
                                  <span className="text-muted-foreground">{ev.message || ev.step.split(":")[1]}</span>
                                </div>
                              ))}
                            </div>
                          );
                        })}
                        {/* agentic tool-call group (only when present) */}
                        {toolCalls.length > 0 && (
                          <div className="flex flex-col gap-1">
                            <div className="flex items-start gap-2 text-xs font-medium">
                              {statusIcon("completed")}
                              <span className="text-foreground/80">执行工具</span>
                            </div>
                            {toolCalls.map((ev) => {
                              const det = (ev.details || {}) as { args?: unknown; result?: unknown };
                              const argsText = typeof det.args === "string" ? det.args : "";
                              const resultText = typeof det.result === "string" ? det.result : "";
                              const hasDetail = !!(argsText || resultText);
                              const label = ev.message || ev.step.replace(/^tool_call:/, "调用 ");
                              if (!hasDetail) {
                                return (
                                  <div key={ev.step} className="flex items-start gap-2 text-xs pl-5">
                                    {statusIcon(ev.status)}
                                    <span className="text-muted-foreground">{label}</span>
                                  </div>
                                );
                              }
                              return (
                                <details key={ev.step} className="pl-5 group/tool">
                                  <summary className="flex items-start gap-2 text-xs cursor-pointer list-none">
                                    {statusIcon(ev.status)}
                                    <span className="text-muted-foreground group-hover/tool:text-foreground transition-colors">{label}</span>
                                    <ChevronDown className="w-3 h-3 mt-0.5 text-muted-foreground/60 group-open/tool:rotate-180 transition-transform" />
                                  </summary>
                                  <div className="mt-1 ml-5 flex flex-col gap-1 text-[11px] text-muted-foreground/80 border-l-2 border-amber-200 dark:border-amber-700 pl-2">
                                    {argsText && (
                                      <div className="whitespace-pre-wrap break-words"><span className="text-muted-foreground/60">参数：</span>{argsText}</div>
                                    )}
                                    {resultText && (
                                      <div className="whitespace-pre-wrap break-words max-h-32 overflow-y-auto"><span className="text-muted-foreground/60">结果：</span>{resultText}</div>
                                    )}
                                  </div>
                                </details>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    ) : activities.length > 0 ? (
                      turnActive ? (
                        <div className="flex flex-col gap-1.5">
                          {activities.map((a) => (
                            <div key={a.id} className="flex flex-col gap-0.5">
                              <div className="flex items-center gap-2 text-xs font-medium">
                                {a.status === "done" ? (
                                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0" />
                                ) : (
                                  <Loader2 className="w-3.5 h-3.5 animate-spin text-amber-700 dark:text-amber-300 shrink-0" />
                                )}
                                <span className={a.status === "done" ? "text-muted-foreground" : "text-amber-700 dark:text-amber-300"}>
                                  {a.skill ? `正在使用 ${a.skill} ${a.skillAction || a.label}` : a.label}
                                </span>
                                {a.detail && <span className="text-muted-foreground/70 font-normal">· {a.detail}</span>}
                              </div>
                              {a.subSteps && a.subSteps.length > 0 && (
                                <div className="pl-5 flex flex-col gap-0.5">
                                  {a.subSteps.map((s) => (
                                    <div key={s.id} className="flex items-center gap-1.5 text-xs text-muted-foreground/80">
                                      {s.status === "done"
                                        ? <CheckCircle2 className="w-3 h-3 text-emerald-500/70 shrink-0" />
                                        : <Loader2 className="w-3 h-3 animate-spin text-amber-500/70 shrink-0" />}
                                      <span>{s.label}</span>
                                      {s.detail && <span className="text-muted-foreground/50">· {s.detail}</span>}
                                    </div>
                                  ))}
                                </div>
                              )}
                              {a.texts && a.texts.length > 0 && (
                                <details className="pl-5">
                                  <summary className="text-[11px] text-muted-foreground/70 cursor-pointer select-none">推理过程</summary>
                                  <div className="text-xs text-muted-foreground/70 whitespace-pre-wrap break-words max-h-24 overflow-y-auto mt-0.5 pl-2 border-l border-muted">
                                    {a.texts.join("")}
                                  </div>
                                </details>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div>
                          <button
                            onClick={() => setShowDoneSteps((v) => !v)}
                            className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
                          >
                            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400" />
                            已完成 {activities.length} 步
                            {showDoneSteps ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                          </button>
                          {showDoneSteps && (
                            <div className="flex flex-col gap-1 mt-1.5 pl-1">
                              {activities.map((a) => (
                                <div key={a.id} className="flex items-center gap-2 text-xs text-muted-foreground">
                                  <CheckCircle2 className="w-3 h-3 text-emerald-600/70 dark:text-emerald-400/70 shrink-0" />
                                  {a.skill ? `使用 ${a.skill} ${a.skillAction || a.label}` : a.label}
                                  {a.detail && <span className="text-muted-foreground/70">· {a.detail}</span>}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )
                    ) : (
                      <div className="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-300 font-medium">
                        <Loader2 className="w-3 h-3 animate-spin" />
                        思考中...
                      </div>
                    )}
                    {/* Model thinking stream as an expandable detail under the steps. */}
                    {thinkingContent && turnActive && (
                      <div className="mt-2">
                        <button
                          onClick={() => setShowThinkDetail((v) => !v)}
                          className="flex items-center gap-1 text-[11px] text-muted-foreground/80 hover:text-muted-foreground transition-colors"
                        >
                          {showThinkDetail ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                          思考过程
                        </button>
                        {showThinkDetail && (
                          <div className="text-xs text-muted-foreground/80 whitespace-pre-wrap break-words max-h-32 overflow-y-auto mt-1 pl-2 border-l-2 border-amber-200 dark:border-amber-700">
                            {thinkingContent}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
                );
              })()}

              {/* Streaming response */}
              {streaming && !thinking && (
                <div className="flex justify-start">
                  <div className="max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-relaxed bg-muted/50 border border-border/60">
                    <div className="whitespace-pre-wrap break-words">{streaming}</div>
                  </div>
                </div>
              )}

              {/* A2UI confirmation card — rendered after the assistant prompt that
                  triggered it; clears once the user picks. Condition relaxed: a2ui
                  event always arrives before or with done, and streaming is cleared
                  by done — no need to gate on !streaming (was hiding cards when
                  gpt-5.4 agentic mode didn't fully clear stream state). */}
              {a2uiRequest && !thinking && (
                <div className="flex justify-start">
                  <div className="max-w-[85%] w-full">
                    <A2UIConfirmCard request={a2uiRequest} onSubmit={actions.handleA2UISubmit} />
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Composer */}
          <div className="px-6 py-4 border-t bg-card/60 backdrop-blur-sm shrink-0">
            <div className="max-w-3xl mx-auto">
              {/* Uploaded attachment thumbnails sit above the input row. */}
              <AttachmentPreviews
                attachments={pendingAttachments}
                onChange={actions.handleAttachmentsChange}
                imageSupported={imageSupported}
              />
              {/* Input row: [+] attach · textarea (2 rows) · model · skills · send. */}
              <div className="flex items-end gap-1.5 bg-background border border-border rounded-xl p-1.5 mt-2 focus-within:ring-2 focus-within:ring-ring/30 focus-within:border-primary/40 transition-all">
                <AttachmentButton
                  conversationId={activeConvId}
                  attachments={pendingAttachments}
                  onChange={actions.handleAttachmentsChange}
                  disabled={!connected}
                />
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleSend();
                    }
                  }}
                  placeholder="描述你想构建的智能体..."
                  rows={2}
                  className="flex-1 text-sm resize-none border-0 bg-transparent px-3 py-2 focus:outline-none min-h-[56px] max-h-[160px]"
                  disabled={!connected}
                  style={{ height: "56px" }}
                  onInput={(e) => {
                    const t = e.target as HTMLTextAreaElement;
                    t.style.height = "56px";
                    t.style.height = Math.min(t.scrollHeight, 160) + "px";
                  }}
                />
                <select
                  value={selectedModel}
                  onChange={(e) => actions.handleModelChange(e.target.value)}
                  className="text-xs border border-border rounded-lg px-2 py-1.5 bg-background max-w-[150px] shrink-0 focus:outline-none focus:ring-1 focus:ring-ring"
                  title="切换模型（仅影响当前会话）"
                >
                  {models.map((m) => (
                    <option key={m.model_id} value={m.model_id} disabled={!m.configured}>
                      {m.display_name}{m.configured ? "" : "（未配置）"}
                    </option>
                  ))}
                  {models.length === 0 && <option value="" disabled>暂无已配置模型</option>}
                </select>
                {!selectedModelConfigured && (
                  <button
                    type="button"
                    onClick={() => router.push("/capabilities")}
                    className="text-[10px] font-medium text-primary hover:underline whitespace-nowrap"
                  >
                    配置模型
                  </button>
                )}
                <PlannerSelfSkills
                  conversationId={activeConvId}
                  selectedSkills={selectedSkills}
                  onToggle={actions.handleSkillToggle}
                  refreshKey={skillsRefreshKey}
                />
                <Button
                  onClick={handleSend}
                  disabled={!connected || !selectedModelConfigured || (!input.trim() && pendingAttachments.length === 0) || isSending}
                  size="icon"
                  className="h-9 w-9 rounded-lg shrink-0"
                >
                  {isSending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                </Button>
              </div>
              <p className="text-[10px] text-muted-foreground/60 mt-1.5 text-center">
                Enter 发送 · Shift+Enter 换行
              </p>
            </div>
          </div>
        </div>

        {/* PAGE_PANEL */}

        {/* Desktop: Planner Panel (always visible) */}
        <aside className="hidden lg:flex w-96 xl:w-[28rem] border-l flex-col shrink-0 bg-card/50 overflow-y-auto">
          <PlannerPanel
            stage={stage}
            stageInfo={stageInfo}
            proposal={proposal}
            onApply={actions.handleApply}
            applying={actions.applying}
            memory={memory}
            conversationId={activeConvId}
            files={files}
            finalSummary={finalSummary}
            linkedAgentId={linkedAgentId}
            onOpenDag={actions.handleOpenDag}
            runEvents={runEvents}
            proposalResult={proposalResult}
            draftAgent={draftAgent}
            compileResult={compileResult}
            onConfirmProposal={actions.handleConfirmProposal}
            onCompileDraft={actions.handleCompileDraft}
            planState={planState ?? null}
            platformSkills={platformSkills}
            platformConnectors={platformConnectors}
            selectedPlatformCapabilityIds={selectedPlatformCapabilityIds}
            onTogglePlatformCapability={togglePlatformCapability}
            onOpenCapabilities={() => router.push("/capabilities")}
          />
        </aside>
      </div>

      {/* Mobile: Panel toggle */}
      <div className="lg:hidden border-t bg-card shrink-0">
        <button
          onClick={() => setMobileShowPanel(!mobileShowPanel)}
          className="w-full flex items-center justify-center gap-2 py-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          <ChevronUp className={`w-3 h-3 transition-transform ${mobileShowPanel ? "rotate-180" : ""}`} />
          {proposal ? "查看架构方案" : stageInfo.label}
        </button>
        {mobileShowPanel && (
          <div className="max-h-[50vh] overflow-y-auto px-4 pb-4">
            <PlannerPanel
              stage={stage}
              stageInfo={stageInfo}
              proposal={proposal}
              onApply={actions.handleApply}
              applying={actions.applying}
              memory={memory}
              conversationId={activeConvId}
              files={files}
              finalSummary={finalSummary}
              linkedAgentId={linkedAgentId}
              onOpenDag={actions.handleOpenDag}
              runEvents={runEvents}
              proposalResult={proposalResult}
              draftAgent={draftAgent}
              compileResult={compileResult}
              onConfirmProposal={actions.handleConfirmProposal}
              onCompileDraft={actions.handleCompileDraft}
              planState={planState ?? null}
              platformSkills={platformSkills}
              platformConnectors={platformConnectors}
              selectedPlatformCapabilityIds={selectedPlatformCapabilityIds}
              onTogglePlatformCapability={togglePlatformCapability}
              onOpenCapabilities={() => router.push("/capabilities")}
            />
          </div>
        )}
      </div>

      {/* Delete session confirmation */}
      <Dialog open={!!actions.deleteTarget} onOpenChange={(o) => { if (!o) actions.setDeleteTarget(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除规划会话</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            确定要删除「{actions.deleteTarget?.title || "未命名会话"}」吗？该会话的所有计划文件与附件将一并删除，删除后无法恢复。
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => actions.setDeleteTarget(null)}>取消</Button>
            <Button variant="destructive" onClick={() => void actions.handleDeleteSession()}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Replan landing-mode chooser */}
      <Dialog open={actions.replanLandingOpen} onOpenChange={(o) => { if (!o) actions.setReplanLandingOpen(false); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>选择落地方式</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            将重规划方案应用到智能体 {linkedAgentId != null ? `#${linkedAgentId}` : ""}。请选择落地方式：
          </p>
          <div className="space-y-2">
            <button
              onClick={() => void actions.handleReplanApply("save_new_version")}
              disabled={actions.applying}
              className="w-full text-left rounded-lg border border-border p-3 hover:bg-muted/50 transition-colors disabled:opacity-50"
            >
              <div className="text-sm font-medium">另存为新版本（推荐）</div>
              <div className="text-xs text-muted-foreground">写出 v(N+1) 为最新版本，保留当前版本可回溯。</div>
            </button>
            <button
              onClick={() => void actions.handleReplanApply("override_draft")}
              disabled={actions.applying}
              className="w-full text-left rounded-lg border border-border p-3 hover:bg-muted/50 transition-colors disabled:opacity-50"
            >
              <div className="text-sm font-medium">覆盖当前版本</div>
              <div className="text-xs text-muted-foreground">就地覆盖当前最新 DAG 版本，不保留旧图。</div>
            </button>
            <button
              onClick={() => void actions.handleReplanApply("partial")}
              disabled={actions.applying || !proposal?.diff}
              className="w-full text-left rounded-lg border border-border p-3 hover:bg-muted/50 transition-colors disabled:opacity-50"
            >
              <div className="text-sm font-medium">局部应用</div>
              <div className="text-xs text-muted-foreground">
                仅合并本次新增 / 废弃的节点（{(proposal?.diff?.added || []).length} 增 / {(proposal?.diff?.removed || []).length} 废），其余保持不变。
              </div>
            </button>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => actions.setReplanLandingOpen(false)}>取消</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Proposal file viewer (opened from a chat chip) */}
      {chatFile && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={() => setChatFile(null)}>
          <div className="bg-background border border-border rounded-xl shadow-2xl max-w-3xl w-full max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 px-4 py-2.5 border-b">
              <FileJson className="w-4 h-4 text-primary" />
              <span className="text-sm font-medium">{chatFile.name}</span>
              <div className="flex-1" />
              <button onClick={() => setChatFile(null)} className="p-1 rounded hover:bg-muted/60 text-muted-foreground">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 overflow-auto p-4">
              {chatFile.loading && <div className="flex items-center justify-center py-8 text-muted-foreground"><Loader2 className="w-4 h-4 animate-spin" /></div>}
              {chatFile.error && <div className="text-sm text-destructive">{chatFile.error}</div>}
              {chatFile.content !== null && !chatFile.error && (
                <pre className="text-xs whitespace-pre-wrap break-words font-mono leading-relaxed">{chatFile.content}</pre>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


function OnboardingBlock() {
  return (
    <div className="rounded-2xl border border-border/60 bg-gradient-to-br from-primary/5 to-transparent p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Sparkles className="w-4 h-4 text-primary" />
        <h2 className="text-sm font-semibold">智能体规划师</h2>
      </div>
      <p className="text-sm text-muted-foreground leading-relaxed">
        我可以帮你从零设计一个智能体。描述你想解决的问题，我会通过几轮对话理解需求，然后给出完整的架构方案。
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <div className="rounded-lg bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">流程：</span> 需求澄清 → 方案确认 → 创建到工作台
        </div>
        <div className="rounded-lg bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">示例：</span> {"“帮我做一个客服问答机器人”"}
        </div>
      </div>
    </div>
  );
}

function ReplanOnboardingBlock() {
  return (
    <div className="rounded-2xl border border-amber-500/30 bg-gradient-to-br from-amber-500/5 to-transparent p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Sparkles className="w-4 h-4 text-amber-600 dark:text-amber-400" />
        <h2 className="text-sm font-semibold">重规划模式</h2>
      </div>
      <p className="text-sm text-muted-foreground leading-relaxed">
        我已载入该智能体的现有架构、节点配置与（如有）运行 / 评估证据，作为本次重规划的基线。
        描述你想做的修订，我会给出保留 / 新增 / 废弃节点的结构性建议，确认后可覆盖、另存新版本或局部应用。
      </p>
    </div>
  );
}

// Read-only display of the injected Replan baseline context. Collapsible so it
// doesn't crowd the chat once the conversation gets going.
function ReplanContextBlock({ context }: { context: string }) {
  return (
    <details className="rounded-2xl border border-border/60 bg-muted/30 px-4 py-3" open>
      <summary className="cursor-pointer text-xs font-semibold text-foreground/80 flex items-center gap-1.5">
        <FileText className="w-3.5 h-3.5 text-primary" />
        现有架构上下文（重规划基线）
      </summary>
      <pre className="mt-2 text-[11px] whitespace-pre-wrap break-words font-mono leading-relaxed text-muted-foreground max-h-64 overflow-y-auto">
        {context}
      </pre>
    </details>
  );
}

function MessageContent({ content }: { content: string }) {
  const thinkMatch = content.match(/^<think>([\s\S]*?)<\/think>\s*([\s\S]*)$/);
  if (thinkMatch) {
    const thinkText = thinkMatch[1].trim();
    const responseText = thinkMatch[2].trim();
    return (
      <div>
        {thinkText && (
          <details className="mb-2">
            <summary className="cursor-pointer text-xs text-amber-600 dark:text-amber-400 font-medium">思考过程</summary>
            <div className="mt-1 text-xs text-muted-foreground/80 whitespace-pre-wrap break-words max-h-28 overflow-y-auto border-l-2 border-amber-200 dark:border-amber-700 pl-2">{thinkText}</div>
          </details>
        )}
        <div className="whitespace-pre-wrap break-words">{responseText}</div>
      </div>
    );
  }
  return <div className="whitespace-pre-wrap break-words">{content}</div>;
}
