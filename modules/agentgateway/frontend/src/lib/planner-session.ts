// Planner session domain: per-conversation runtime + a pure event reducer.
//
// The reducer is deliberately side-effect free over the runtime's *data* fields
// (it never touches the WebSocket). This is what makes session isolation testable:
// feeding an event for conversation A only ever mutates A's runtime, so a test can
// assert that B is untouched while A is "thinking". The page component owns the
// socket lifecycle and decides — based on the returned result — whether to repaint
// the active view, bump the sidebar, or surface a toast.
import { stripMemoryUpdate } from "./sanitize.ts";
import type { AttachmentMeta } from "./types.ts";

// Marker the backend appends to a persisted assistant message that delivered a
// structured proposal as a file (instead of inlining the JSON). On restore the
// frontend strips it and renders a clickable proposal.json chip.
const PROPOSAL_FILE_OPEN = "<proposal_file>";
const PROPOSAL_FILE_CLOSE = "</proposal_file>";

// Small stable string hash (djb2) for synthesizing fallback ids.
function hashStr(s: string): number {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return h;
}

// Pull the proposal-file marker out of a persisted message. Returns the clean
// display text plus the referenced filename (or null when absent).
export function extractProposalFile(text: string): { content: string; proposalFile: string | null } {
  if (!text || !text.includes(PROPOSAL_FILE_OPEN)) {
    return { content: text, proposalFile: null };
  }
  const start = text.indexOf(PROPOSAL_FILE_OPEN);
  const end = text.indexOf(PROPOSAL_FILE_CLOSE, start + PROPOSAL_FILE_OPEN.length);
  if (end === -1) {
    return { content: text.slice(0, start).trimEnd(), proposalFile: "proposal.json" };
  }
  const filename = text.slice(start + PROPOSAL_FILE_OPEN.length, end).trim() || "proposal.json";
  const content = (text.slice(0, start) + text.slice(end + PROPOSAL_FILE_CLOSE.length)).trim();
  return { content, proposalFile: filename };
}

export type PlannerStage =
  | "clarifying"
  | "drafting"
  | "awaiting_confirmation"
  | "ready_to_apply"
  | "applied";

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  attachments?: AttachmentMeta[];
  // When set, this assistant turn delivered a structured proposal as a file.
  // The chat renders a clickable proposal.json chip instead of raw JSON.
  proposalFile?: string;
  // The skills this turn ACTUALLY triggered (an observable action ran for them):
  // result attribution (change: add-planner-skill-transparency). Renders a
  // "本轮触发技能" badge; absent when nothing fired → no badge.
  triggeredSkills?: string[];
}

export interface ProposalDiff {
  kept?: string[];
  added?: string[];
  removed?: string[];
  edge_changes?: string[];
}

export interface Proposal {
  architecture_summary: string;
  nodes: Array<{ id: string; type: string; config: Record<string, unknown>; description?: string }>;
  edges: Array<{ source: string; target: string; targetHandle?: string }>;
  rationale: string;
  tuning_hints?: string[];
  // Replan-only: structural diff vs the existing architecture.
  diff?: ProposalDiff;
  risks?: string[];
  apply_recommendation?: "direct" | "draft";
  // Capabilities the proposal references that are NOT yet in the library and
  // must be created before apply (backend marks these; user decision: 标注待创建).
  capabilities_to_create?: string[];
}

export interface PlannerMemory {
  requirement_summary: string;
  confirmed_constraints: string[];
  task_classification: string;
  latest_proposal_summary: string;
  user_feedback: string[];
}

export interface A2UIRequestData {
  id: string;
  prompt: string;
  options: Array<{ id: string; label: string; description?: string }>;
  allow_free_text?: boolean;
}

// Whether an A2UI choice expresses "proceed" intent (confirm / create / apply /
// yes …). When true, submitting it should auto-continue the planner turn so the
// proposal / JSON is generated without the user having to type a follow-up.
// "Modify"-style choices are NOT advance intent — the user still owes a detail —
// so a negative cue (修改/取消/no…) always wins over a positive one, guarding
// against labels like "不创建" or "重新生成". Chinese cues match as substrings
// (no word boundaries in CJK); English cues use \b so "yes" doesn't fire on
// "eyes".
// Default: if a choice is neither clearly negative nor clearly advance, treat it
// as advance (the planner asked a question → any non-modify selection should let
// it continue). This covers "场景选择" style cards where labels like "知识库问答"
// don't contain explicit confirm/create keywords.
const A2UI_ADVANCE_RE = /(确认|创建|生成|同意|继续|批准|采纳|就这样|没问题|可以了|开始|^是$|\b(?:yes|confirm|create|apply|proceed|approve|ok|go)\b)/i;
const A2UI_NEGATIVE_RE = /(修改|调整|重新|再想|换成|取消|放弃|返回|^不|^否|\b(?:cancel|modify|change|edit|no|back)\b)/i;

export function isAdvanceChoice(choiceId: string, label: string): boolean {
  const hay = `${label} ${choiceId}`.trim();
  if (A2UI_NEGATIVE_RE.test(hay)) return false;
  // Explicit advance keyword → advance. Otherwise default to advance (planner is
  // waiting for ANY selection to continue — only negative choices need follow-up).
  return true;
}

export interface PlanFileItemData {
  path: string;
  filename?: string;
  kind: string;
  summary?: string;
  updated_at?: string;
}

export interface ActivitySubStep {
  id: string;
  label: string;
  status: "active" | "done";
  detail?: string;
}

// One step of a planner turn's visible activity timeline. Driven by backend
// `activity` events: a `start` appends an item with status "active", and a
// `done` with the same id flips it to "done". On turn end (done/error) any
// still-"active" item is finalized so the timeline never hangs "in progress".
export interface ActivityItem {
  id: string;
  phase: string;
  label: string;
  status: "active" | "done";
  detail?: string;
  // Skill attribution (change: add-planner-skill-transparency). Set when the
  // backend attributed this action to a skill that ACTUALLY fired this turn;
  // renders "正在使用 «skill» «action»".
  skill?: string;
  skillAction?: string;
  // tool_call sub-steps (planner-process-stream-rich-display)
  subSteps?: ActivitySubStep[];
  // step_text fragments (planner-process-stream-rich-display), capped at 5
  texts?: string[];
}

// One normalized planner run-event (T8: planner-run-events). Streamed live over
// the WS `run_event` type alongside (not replacing) the coarse activity stream.
// `step` is from the backend's normalized turn-loop set; `status` is
// queued|running|completed|failed|waiting_user. De-dup key: (run_id, step, event_kind)
// or event_id when present. Same key updates in place; different keys append.
export interface RunEvent {
  run_id: string;
  proposal_id?: number | null;
  step: string;
  status: string;
  message?: string;
  details?: Record<string, unknown>;
  progress?: number;
  event_kind?: string;
  event_id?: string;
}

// A first-class proposal fetched from / advanced through the backend proposals
// table (T5). Distinct from the live `Proposal` extracted from final_summary:
// this carries lifecycle status + the persisted payload + (when confirmed) a
// derived draft agent.
export interface ProposalResult {
  id: number;
  conversation_id: string;
  status: string;            // draft|confirmed|rejected|applied
  user_goal?: string;
  inferred_goal?: string;
  task_type?: string;
  selected_runtime_mode?: string;
  proposal?: Record<string, unknown>;
}

// Pre-publish draft agent derived from a confirmed proposal (T5).
export interface DraftAgent {
  id: number;
  proposal_id: number;
  name: string;
  status: string;            // draft|tested|published
  runtime_mode: string;
  config?: Record<string, unknown>;
  capability_refs?: Array<{ id?: number; type?: string; name?: string }>;
}

// Structured compile result for a draft agent (T6).
export interface CompileResult {
  success: boolean;
  errors: string[];
  warnings: string[];
  graph_json?: string | null;
  dryrun?: { ran: boolean; ok: boolean; message: string };
}

// ── Plan+Loop Types (planner-plan-loop-refactor) ──

export type PlanStepStatus = "pending" | "running" | "done" | "failed" | "blocked" | "waiting_user" | "skipped";

export interface PlanStepState {
  id: string;
  title: string;
  status: PlanStepStatus;
  executor_type?: string;
  error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  outputs?: Record<string, unknown>;
}

export interface PlanState {
  schema_version: string;
  plan_id: string;
  goal: string;
  mode: string;
  status: string;  // running | waiting_user | completed | failed
  current_step_id: string | null;
  revision: number;
  steps: PlanStepState[];
  stop_reason?: string | null;
}

// Per-conversation runtime. All session state lives here keyed by conversation_id
// so switching sessions never bleeds one session's "thinking"/stream into another,
// and an in-flight generation keeps accumulating into its own buffers even while
// the user is looking at a different session.
export interface SessionRuntime {
  conversationId: string;
  ws: WebSocket | null;
  connected: boolean;
  messages: Message[];
  streamBuf: string;
  thinkBuf: string;
  thinking: boolean;
  proposal: Proposal | null;
  memory: PlannerMemory | null;
  stage: PlannerStage;
  files: PlanFileItemData[];
  finalSummary: string | null;
  a2uiRequest: A2UIRequestData | null;
  pendingAttachments: AttachmentMeta[];
  selectedSkills: string[];
  linkedAgentId: number | null;
  model: string;
  provider: string;
  msgId: number;
  lastActivity: number;
  needsReconnect: boolean;
  // "create" (design from scratch) or "replan" (revise an existing agent).
  mode: "create" | "replan";
  // Injected existing-agent context block for replan sessions (read-only display).
  replanContext: string;
  // Set when a final_summary delivers a structured proposal this turn; the
  // subsequent `done` stamps the assistant message as a proposal.json chip.
  pendingProposalFile: string | null;
  // This turn's visible activity steps (white-box timeline). Reset at the start
  // of each turn; finalized on done/error.
  activities: ActivityItem[];
  // Skills this turn ACTUALLY triggered so far, to stamp onto the assistant
  // message on done (carried by skill-attributed activities / final_summary /
  // done). Mirrors pendingProposalFile's stamp-on-done pattern.
  pendingTriggeredSkills: string[] | null;
  // ── Plan-first builder loop (T7) ──
  // Normalized run-event timeline for this run (T8 stream), same step updates
  // in place. Reset at the start of each turn.
  runEvents: RunEvent[];
  // First-class proposal (T5) confirmed/queried via HTTP — drives the plan-first
  // confirm → draft → compile sub-flow, distinct from the live `proposal`.
  proposalResult: ProposalResult | null;
  selectedProposalOption: string | null;
  draftAgent: DraftAgent | null;
  compileResult: CompileResult | null;
  // ── Plan+Loop mode (planner-plan-loop-refactor) ──
  // Authoritative Plan snapshot from plan_update events. null means legacy mode.
  planState: PlanState | null;
}

export function blankRuntime(cid: string): SessionRuntime {
  return {
    conversationId: cid,
    ws: null,
    connected: false,
    messages: [],
    streamBuf: "",
    thinkBuf: "",
    thinking: false,
    proposal: null,
    memory: null,
    stage: "clarifying",
    files: [],
    finalSummary: null,
    a2uiRequest: null,
    pendingAttachments: [],
    selectedSkills: [],
    linkedAgentId: null,
    model: "qwen3.6-27b",
    provider: "glm",
    msgId: 0,
    lastActivity: 0,
    needsReconnect: false,
    mode: "create",
    replanContext: "",
    pendingProposalFile: null,
    activities: [],
    pendingTriggeredSkills: null,
    runEvents: [],
    proposalResult: null,
    selectedProposalOption: null,
    draftAgent: null,
    compileResult: null,
    planState: null,
  };
}

export function tryParseProposal(text: string): Proposal | null {
  try {
    let jsonText = text;
    if (text.includes("```json")) {
      jsonText = text.split("```json")[1].split("```")[0].trim();
    } else if (text.includes("```")) {
      jsonText = text.split("```")[1].split("```")[0].trim();
    }
    const data = JSON.parse(jsonText);
    if (data.ready && data.proposal) return data.proposal as Proposal;
    return null;
  } catch {
    return null;
  }
}

export function inferStage(messages: Message[], proposal: Proposal | null): PlannerStage {
  if (proposal) return "ready_to_apply";
  const assistantMsgs = messages.filter((m) => m.role === "assistant");
  if (assistantMsgs.length <= 1) return "clarifying";
  const lastAssistant = assistantMsgs[assistantMsgs.length - 1]?.content || "";
  if (lastAssistant.includes("是否符合") || lastAssistant.includes("确认后") || lastAssistant.includes("需要调整")) {
    return "awaiting_confirmation";
  }
  if (assistantMsgs.length >= 4) return "drafting";
  return "clarifying";
}

// Shape of a decoded WebSocket message from the planner backend.
export interface PlannerWsEvent {
  type?: string;
  content?: string;
  stage?: string;
  message?: string;
  memory?: PlannerMemory;
  messages?: Array<{ role: string; content: string; triggered_skills?: string[] }>;
  path?: string;
  filename?: string;
  kind?: string;
  summary?: string;
  updated_at?: string;
  text?: string;
  files?: PlanFileItemData[];
  file_artifacts?: PlanFileItemData[];
  proposal?: Proposal;
  model?: string;
  provider?: string;
  credential_warning?: { provider: string; missing: string[] };
  id?: string;
  prompt?: string;
  options?: Array<{ id: string; label: string; description?: string }>;
  allow_free_text?: boolean;
  choice?: string;
  skill_name?: string;
  selected_skills?: string[];
  linked_agent_id?: number | null;
  mode?: string;
  replan_context?: string;
  // activity event fields (id reuses the shared `id` above)
  phase?: string;
  label?: string;
  status?: string;
  detail?: string;
  // skill transparency (change: add-planner-skill-transparency)
  // skills this turn actually triggered (an observable action ran for them).
  triggered_skills?: string[];
  // activity skill attribution: which skill conventionally owns this action.
  skill?: string;
  skill_action?: string;
  // run_event fields (T8: planner-run-events). `step`/`status` carry the
  // normalized stage; `progress` is 0..1; `details` is structured extra.
  run_id?: string;
  proposal_id?: number | null;
  step?: string;
  progress?: number;
  details?: Record<string, unknown>;
  event_kind?: string;
  event_id?: string;
  // plan_update fields (planner-plan-loop-refactor). Carries full Plan snapshot.
  plan?: Record<string, unknown>;
  stop_reason?: string | null;
}

// Side-effects the page component must perform after a reduce (the reducer itself
// stays pure over the runtime's data so it can be unit-tested without React).
export interface ReduceEffects {
  changed: boolean;        // runtime data changed → repaint active view if focused
  refreshSessions?: boolean; // a turn completed → refresh sidebar
  refreshSkills?: boolean;   // skills mounted/unmounted → refresh skills popover
  toast?: string;            // surface an error toast (only if this is the active session)
}

// Apply one decoded WS event to its owning runtime, in place. Returns which
// follow-up effects the caller should run. Never touches `rt.ws`.
export function reduceWsEvent(rt: SessionRuntime, msg: PlannerWsEvent, now: number = Date.now()): ReduceEffects {
  rt.lastActivity = now;
  // DEBUG: log all incoming WS event types
  if (msg.type === "a2ui_request" || msg.type === "done" || msg.type === "final_summary") {
    console.log("[WS-EVENT]", msg.type, msg.type === "a2ui_request" ? JSON.stringify(msg).slice(0, 150) : "");
  }

  if (msg.type === "thinking_content" && msg.content) {
    rt.thinking = true;
    rt.thinkBuf += msg.content;
    return { changed: true };
  }
  if (msg.type === "token" && msg.content) {
    rt.thinking = false;
    const visible = stripMemoryUpdate(msg.content);
    if (!visible) return { changed: false };
    rt.streamBuf += visible;
    return { changed: true };
  }
  if (msg.type === "done") {
    rt.thinking = false;
    // Finalize the timeline: any step still "active" (lost done frame) is forced
    // to "done" so the timeline never hangs "in progress" after the turn ends.
    if (rt.activities.some((a) => a.status === "active")) {
      rt.activities = rt.activities.map((a) => a.status === "active" ? { ...a, status: "done" } : a);
    }
    const content = stripMemoryUpdate(rt.streamBuf);
    const thinkText = rt.thinkBuf;
    rt.streamBuf = "";
    rt.thinkBuf = "";
    rt.msgId++;
    const fullContent = thinkText ? `<think>${thinkText}</think>\n${content}` : content;
    const proposalFile = rt.pendingProposalFile;
    // Stamp the skills this turn ACTUALLY triggered onto the message (result
    // attribution). Prefer the explicit done payload; fall back to what the
    // skill-attributed activities accumulated this turn.
    const doneSkills = Array.isArray(msg.triggered_skills) && msg.triggered_skills.length
      ? msg.triggered_skills
      : rt.pendingTriggeredSkills;
    const assistantMsg: Message = { id: rt.msgId, role: "assistant", content: fullContent };
    if (doneSkills && doneSkills.length) assistantMsg.triggeredSkills = doneSkills;
    rt.pendingTriggeredSkills = null;
    if (proposalFile) {
      // A structured proposal was delivered this turn as a file (final_summary
      // carried it). The chat shows a clickable chip, never the raw JSON.
      assistantMsg.proposalFile = proposalFile;
      rt.pendingProposalFile = null;
      rt.stage = "ready_to_apply";
      rt.messages = [...rt.messages, assistantMsg];
      return { changed: true, refreshSessions: true };
    }
    rt.messages = [...rt.messages, assistantMsg];
    const parsed = tryParseProposal(content);
    if (parsed) {
      rt.proposal = parsed;
      rt.stage = "ready_to_apply";
    } else {
      rt.stage = inferStage(rt.messages, null);
    }
    return { changed: true, refreshSessions: true };
  }
  if (msg.type === "stage" && msg.stage) {
    rt.stage = msg.stage as PlannerStage;
    return { changed: true };
  }
  if (msg.type === "memory_update" && msg.memory) {
    rt.memory = msg.memory;
    const sk = (msg.memory as PlannerMemory & { selected_skills?: string[] }).selected_skills;
    if (Array.isArray(sk)) rt.selectedSkills = sk;
    return { changed: true };
  }
  if (msg.type === "session_restored") {
    // A session_restored only arrives on a freshly (re)opened socket, so this
    // runtime cannot have an in-flight turn on *this* connection. Any leftover
    // thinking/stream buffer is a zombie from a socket that dropped mid-turn
    // (onclose without a token/done/error) — clear it so a reconnect never
    // resurrects a stuck "思考中". This is the reconnect-path guarantee behind
    // "控制类回执不得使思考中状态悬挂".
    rt.thinking = false;
    rt.streamBuf = "";
    rt.thinkBuf = "";
    rt.activities = [];
    rt.runEvents = [];
    rt.pendingTriggeredSkills = null;
    if (msg.memory) {
      rt.memory = msg.memory;
      const sk = (msg.memory as PlannerMemory & { selected_skills?: string[] }).selected_skills;
      if (Array.isArray(sk)) rt.selectedSkills = sk;
    }
    if (msg.stage) rt.stage = msg.stage as PlannerStage;
    if (typeof msg.linked_agent_id === "number") rt.linkedAgentId = msg.linked_agent_id;
    if (msg.mode === "replan" || msg.mode === "create") rt.mode = msg.mode;
    if (typeof msg.replan_context === "string") rt.replanContext = msg.replan_context;
    // Idempotent hydration (D2): only paint the snapshot when the runtime holds no
    // messages yet. A reconnect that re-sends session_restored while we already have
    // incremental messages must NOT clobber/reorder them — that positional re-hydrate
    // is what produced duplicate React keys. Ids come from the monotonic, never-reused
    // counter (D1), so a later hydrate can never land back on an id already in use.
    if (Array.isArray(msg.messages) && msg.messages.length > 0 && rt.messages.length === 0) {
      const hydrated: Message[] = msg.messages.map((m) => {
        const role = m.role === "user" ? "user" : "assistant";
        const { content, proposalFile } = extractProposalFile(m.content || "");
        const msgItem: Message = { id: ++rt.msgId, role, content: stripMemoryUpdate(content) };
        if (role === "assistant" && proposalFile) msgItem.proposalFile = proposalFile;
        // Restore the result-attribution badge from persisted message metadata
        // (change: add-planner-skill-transparency) so it survives a refresh.
        if (role === "assistant" && Array.isArray(m.triggered_skills) && m.triggered_skills.length) {
          msgItem.triggeredSkills = m.triggered_skills;
        }
        return msgItem;
      });
      rt.messages = hydrated;
    }
    if (Array.isArray(msg.file_artifacts)) rt.files = msg.file_artifacts;
    return { changed: true };
  }
  if (msg.type === "plan_file_updated" && msg.filename) {
    const item: PlanFileItemData = {
      path: msg.path || msg.filename,
      filename: msg.filename,
      kind: msg.kind || "file",
      summary: msg.summary,
      updated_at: msg.updated_at,
    };
    const without = rt.files.filter((p) => (p.filename || p.path) !== (item.filename || item.path));
    rt.files = [...without, item];
    return { changed: true };
  }
  if (msg.type === "final_summary") {
    rt.finalSummary = msg.text || "";
    if (Array.isArray(msg.files)) rt.files = msg.files;
    // Result attribution: a proposal turn's final_summary carries the skills it
    // actually triggered; prime them so the upcoming assistant message is stamped.
    if (Array.isArray(msg.triggered_skills) && msg.triggered_skills.length) {
      rt.pendingTriggeredSkills = msg.triggered_skills;
    }
    // The structured proposal now rides on this event (the chat no longer
    // carries the JSON). Drive PlannerPanel + apply from it, and mark the
    // upcoming assistant turn to render a proposal.json file chip.
    if (msg.proposal && typeof msg.proposal === "object") {
      rt.proposal = msg.proposal as Proposal;
      rt.stage = "ready_to_apply";
    }
    const propFile = (msg.files || []).find((f) => (f.filename || f.path || "").endsWith("proposal.json"));
    rt.pendingProposalFile = propFile ? (propFile.filename || propFile.path.split("/").pop() || "proposal.json") : "proposal.json";
    return { changed: true };
  }
  if (msg.type === "a2ui_request" && Array.isArray(msg.options) && msg.options.length > 0) {
    // DEBUG: confirm event received
    console.log("[A2UI] received a2ui_request event:", JSON.stringify(msg).slice(0, 200));
    // The card renders on options; id keys the response round-trip. A backend
    // that omits id (older agentic request_confirmation) must NOT silently drop
    // the card — synthesize a stable fallback so the user can still confirm.
    const reqId = msg.id || `a2ui_${Math.abs(hashStr(msg.prompt || JSON.stringify(msg.options)))}`;
    rt.a2uiRequest = {
      id: reqId,
      prompt: msg.prompt || "请确认",
      options: msg.options,
      allow_free_text: !!msg.allow_free_text,
    };
    return { changed: true };
  }
  if (msg.type === "a2ui_recorded") {
    rt.a2uiRequest = null;
    return { changed: true };
  }
  if (msg.type === "skill_attached_ok" || msg.type === "skill_detached_ok") {
    if (Array.isArray(msg.selected_skills)) rt.selectedSkills = msg.selected_skills;
    return { changed: true, refreshSkills: true };
  }
  if (msg.type === "activity" && msg.id && msg.status) {
    const phase = msg.phase || "";
    const label = msg.label || phase || "处理中";
    // A skill-attributed activity is the signal that this skill ACTUALLY fired
    // this turn — accumulate it so the assistant message can be stamped on done.
    if (msg.skill) {
      const prev = rt.pendingTriggeredSkills || [];
      if (!prev.includes(msg.skill)) rt.pendingTriggeredSkills = [...prev, msg.skill];
    }

    // tool_call phase: render as sub-step of the last main activity item
    if (phase === "tool_call") {
      if (msg.status === "start" || msg.status === "done") {
        const parentIdx = rt.activities.length - 1;
        if (parentIdx >= 0) {
          const next = [...rt.activities];
          const parent = { ...next[parentIdx] };
          const subSteps = [...(parent.subSteps || [])];
          const subIdx = subSteps.findIndex((s) => s.id === msg.id);
          const subItem: ActivitySubStep = {
            id: msg.id, label,
            status: msg.status === "start" ? "active" : "done",
            ...(msg.detail ? { detail: msg.detail } : {}),
          };
          if (subIdx >= 0) subSteps[subIdx] = subItem;
          else subSteps.push(subItem);
          parent.subSteps = subSteps;
          next[parentIdx] = parent;
          rt.activities = next;
          return { changed: true };
        }
      }
      return { changed: false };
    }

    // step_text phase: append text fragment to last main activity item (cap 5)
    if (phase === "step_text" && msg.status === "stream" && msg.detail) {
      const parentIdx = rt.activities.length - 1;
      if (parentIdx >= 0) {
        const next = [...rt.activities];
        const parent = { ...next[parentIdx] };
        const texts = [...(parent.texts || []), msg.detail];
        parent.texts = texts.length > 5 ? texts.slice(-5) : texts;
        next[parentIdx] = parent;
        rt.activities = next;
        return { changed: true };
      }
      return { changed: false };
    }

    if (msg.status === "start") {
      // New step in progress. Dedupe by id in case of a resend.
      if (!rt.activities.some((a) => a.id === msg.id)) {
        rt.activities = [...rt.activities, {
          id: msg.id, phase, label, status: "active",
          ...(msg.detail ? { detail: msg.detail } : {}),
          ...(msg.skill ? { skill: msg.skill } : {}),
          ...(msg.skill_action ? { skillAction: msg.skill_action } : {}),
        }];
      }
      return { changed: true };
    }
    if (msg.status === "done") {
      // Pair with the matching start by id and flip it to done. If the start was
      // lost (dropped frame), synthesize a completed step so the timeline still
      // reflects that the phase happened.
      const idx = rt.activities.findIndex((a) => a.id === msg.id);
      if (idx >= 0) {
        const next = [...rt.activities];
        next[idx] = {
          ...next[idx], status: "done",
          ...(msg.detail ? { detail: msg.detail } : {}),
          ...(msg.skill ? { skill: msg.skill } : {}),
          ...(msg.skill_action ? { skillAction: msg.skill_action } : {}),
        };
        rt.activities = next;
      } else {
        rt.activities = [...rt.activities, {
          id: msg.id, phase, label, status: "done",
          ...(msg.detail ? { detail: msg.detail } : {}),
          ...(msg.skill ? { skill: msg.skill } : {}),
          ...(msg.skill_action ? { skillAction: msg.skill_action } : {}),
        }];
      }
      return { changed: true };
    }
    return { changed: false };
  }
  // ── Plan+Loop: plan_update (authoritative Plan snapshot) ──
  if (msg.type === "plan_update" && msg.plan) {
    const planData = msg.plan;
    const planState: PlanState = {
      schema_version: (planData.schema_version as string) || "",
      plan_id: (planData.plan_id as string) || "",
      goal: (planData.goal as string) || "",
      mode: (planData.mode as string) || "create",
      status: (planData.status as string) || "running",
      current_step_id: (planData.current_step_id as string) ?? null,
      revision: (planData.revision as number) ?? 1,
      steps: ((planData.steps as any[]) || []).map((s: any) => ({
        id: s.id,
        title: s.title,
        status: s.status || "pending",
        executor_type: s.executor_type,
        error: s.error ?? null,
        started_at: s.started_at ?? null,
        completed_at: s.completed_at ?? null,
        outputs: s.outputs,
      })),
      stop_reason: msg.stop_reason ?? null,
    };
    rt.planState = planState;
    return { changed: true };
  }
  if (msg.type === "run_event" && msg.step && msg.status) {
    // T8 normalized run-event stream — separate from the coarse `activity`
    // timeline (both coexist). De-dup rules:
    // 1. event_id present → unique key
    // 2. Lifecycle events (plan_step_started/completed/validation_failed) → key = (run_id, step)
    //    completed overwrites started for the same step in the same run
    // 3. Content events (llm_draft_delta, hint, etc.) → transient, not stored in runEvents
    const ev: RunEvent = {
      run_id: msg.run_id || "",
      proposal_id: msg.proposal_id ?? null,
      step: msg.step,
      status: msg.status,
      ...(msg.message ? { message: msg.message } : {}),
      ...(msg.details ? { details: msg.details } : {}),
      ...(typeof msg.progress === "number" ? { progress: msg.progress } : {}),
      ...(msg.event_kind ? { event_kind: msg.event_kind } : {}),
      ...(msg.event_id ? { event_id: msg.event_id } : {}),
    };

    // Skip transient events that are for streaming display only, not timeline
    const transientKinds = new Set(["llm_draft_delta", "llm_thinking_delta", "hint", "a2ui_error"]);
    if (ev.event_kind && transientKinds.has(ev.event_kind)) {
      return { changed: true };
    }

    // De-dup: event_id first, then (run_id, step) for lifecycle events
    const idx = msg.event_id
      ? rt.runEvents.findIndex((e) => e.event_id === msg.event_id)
      : rt.runEvents.findIndex(
          (e) => e.run_id === ev.run_id && e.step === ev.step,
        );
    if (idx >= 0) {
      const next = [...rt.runEvents];
      next[idx] = { ...next[idx], ...ev };
      rt.runEvents = next;
    } else {
      rt.runEvents = [...rt.runEvents, ev];
    }
    return { changed: true };
  }
  if (msg.type === "model_resolved") {
    // Control-plane echo (set_model / on-connect). It MUST NOT touch the
    // "thinking/working" state: that lifecycle is owned by the message turn
    // (started in handleSend, ended by this turn's token/done/error). Switching
    // models only updates which model the next turn uses — it must never leave
    // the chat stuck on "思考中" nor clear an in-flight turn's thinking.
    if (msg.model) rt.model = msg.model;
    if (msg.provider) rt.provider = msg.provider;
    // Visible attribution: backend flags when the switched-to model's provider
    // has no usable credentials, so the user learns why the next turn would 401
    // instead of being surprised. Key values are never included.
    if (msg.credential_warning?.provider) {
      const miss = (msg.credential_warning.missing || []).join(" / ");
      return {
        changed: true,
        toast: `模型「${msg.model}」的 provider「${msg.credential_warning.provider}」缺少凭据（${miss}），请在 .env 配置后再使用`,
      };
    }
    return { changed: true };
  }
  if (msg.type === "error") {
    rt.thinking = false;
    rt.streamBuf = "";
    rt.thinkBuf = "";
    // Finalize any in-progress timeline steps so an errored turn doesn't leave
    // a step spinning forever.
    if (rt.activities.some((a) => a.status === "active")) {
      rt.activities = rt.activities.map((a) => a.status === "active" ? { ...a, status: "done" } : a);
    }
    return { changed: true, toast: msg.message || "出错了" };
  }
  return { changed: false };
}

// --- Planner page bootstrap orchestration ----------------------------------
// The DeerFlow planner page mounts a one-shot effect that picks exactly ONE of
// four routes to open a session. Under React StrictMode the effect mounts twice
// (mount → unmount → mount), and the first mount may be cancelled mid-`await`.
// The naive version latched a `restored` flag on *entry* to a route, so an
// aborted first mount consumed the latch and the second mount fell through to
// the fresh-start backstop — spawning a stray blank session (the replan bug).
//
// This pure orchestrator encodes the fix (design D1/D3): the latch is set ONLY
// after a route reports it actually opened a session, and a cancelled mount
// leaves the latch untouched so the next mount re-runs the same route. It is
// parameterized over injected actions so the React effect and the unit tests
// drive byte-for-byte identical control flow.

export interface BootstrapParams {
  // URL params (null when absent).
  fromAgentId: number | null;
  sessionParam: string | null;
  // Cross-mount latch (a useRef in the component). Survives StrictMode remounts.
  restored: { current: boolean };
  // Per-mount cancellation flag (set true by the effect cleanup).
  cancelled: { current: boolean };
  // Actions — each opener returns true iff it actually opened/focused a session.
  createSessionFromAgent: (agentId: number) => Promise<{ conversation_id: string }>;
  openExistingSession: (cid: string) => Promise<boolean>;
  restoreFromCache: () => boolean; // synchronous legacy local-cache hydrate
  startNewSession: () => Promise<boolean>; // fresh-start backstop
  onFromAgentError?: (e: unknown) => void;
}

export async function runPlannerBootstrap(p: BootstrapParams): Promise<void> {
  const { restored, cancelled } = p;

  // Route 1 — from_agent: resume the replan session linked to that agent.
  // createSessionFromAgent is idempotent on the backend (reuses the linked
  // session), so a double-mount won't duplicate rows. An aborted mount must
  // NOT latch, so the next mount can re-run this route (D2).
  if (p.fromAgentId != null && !restored.current) {
    try {
      const created = await p.createSessionFromAgent(p.fromAgentId);
      if (cancelled.current) return; // aborted: leave latch unset for next mount
      if (await p.openExistingSession(created.conversation_id)) {
        restored.current = true;
      }
      return;
    } catch (e) {
      if (!cancelled.current) p.onFromAgentError?.(e);
      // fall through to fresh start
    }
  }

  // Route 2 — ?session=<cid>: explicit restore.
  if (p.sessionParam && !restored.current) {
    if (await p.openExistingSession(p.sessionParam)) {
      restored.current = true;
    }
    if (cancelled.current || restored.current) return;
    // open failed (not cancelled): fall through to fresh start
  }

  // Route 3 — local cache restore (synchronous; no await boundary to abort).
  if (!restored.current && !cancelled.current) {
    if (p.restoreFromCache()) {
      restored.current = true;
      return;
    }
  }

  // Route 4 — fresh-start backstop. Latched + cancel-guarded (D3).
  if (!restored.current && !cancelled.current) {
    if (await p.startNewSession()) {
      restored.current = true;
    }
  }
}

