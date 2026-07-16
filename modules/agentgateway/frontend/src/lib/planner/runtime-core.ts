// Pure runtime helpers for the Planner multi-session store.
//
// These functions hold the *decision logic* of the runtime layer — working-set
// computation, connection recycling, the concurrency gate, and sessionStorage
// (de)serialization — with NO React and NO WebSocket dependency, so they can be
// unit-tested byte-for-byte against the behavior that used to live inline in
// `app/builder/deerflow/page.tsx`. The `usePlannerRuntime` hook wires React
// state + real sockets on top of these.
//
// Behavior contract (spec conversational-agent-builder): session isolation,
// event routing by conversation_id, in-flight generation never recycled, and the
// concurrency gate / connection cap thresholds are preserved exactly as before.
import type { Message, PlannerStage, Proposal, PlannerMemory, SessionRuntime } from "@/lib/planner-session";

// ---- Constants (moved verbatim from page.tsx) ----
export const SESSION_KEY = "planner_session";
export const MAX_STORED_MESSAGES = 50;
// Soft cap on simultaneously-live WebSocket connections (design D5). Sessions with
// an in-flight generation are never recycled.
export const MAX_LIVE_CONNECTIONS = 8;
// Max number of sessions that may be "working" (generating) at once (design D3).
export const MAX_WORKING_SESSIONS = 3;

// ---- Persisted session snapshot (legacy sessionStorage cache) ----
export interface PlannerSessionState {
  conversationId: string;
  messages: Message[];
  stage: PlannerStage;
  selectedModel: string;
  proposal: Proposal | null;
  memorySnapshot: PlannerMemory | null;
  updatedAt: number;
}

export function saveSession(state: PlannerSessionState): void {
  try {
    const toStore = { ...state, messages: state.messages.slice(-MAX_STORED_MESSAGES) };
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(toStore));
  } catch { /* quota exceeded — ignore */ }
}

export function loadSession(): PlannerSessionState | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as PlannerSessionState;
  } catch { return null; }
}

export function clearSession(): void {
  sessionStorage.removeItem(SESSION_KEY);
}

// ---- Working-set logic ----
// A session is "working" when it has an in-flight generation: model is thinking,
// tokens are mid-stream, or an activity step is still in progress. Single source
// of truth = the runtime.
export function isWorking(rt: SessionRuntime): boolean {
  return rt.thinking || rt.streamBuf.length > 0 || rt.activities.some((a) => a.status === "active");
}

// Compute the set of conversation_ids that are currently working, from a runtime map.
export function computeWorkingIds(runtimes: Iterable<SessionRuntime>): string[] {
  const ids: string[] = [];
  for (const rt of runtimes) {
    if (isWorking(rt)) ids.push(rt.conversationId);
  }
  return ids;
}

// Whether a list of ids differs from a previous list (order-insensitive, by membership).
export function workingIdsChanged(next: string[], prev: string[]): boolean {
  return next.length !== prev.length || next.some((id) => !prev.includes(id));
}

// Concurrency gate (design D3): a NEW working session is blocked when the cap is
// already reached. A session already working doesn't count against itself.
export function canStartNewWorking(cid: string, working: string[]): boolean {
  if (working.includes(cid)) return true;
  return working.length < MAX_WORKING_SESSIONS;
}

// ---- Connection recycling ----
// Given the live runtimes and the active conversation, return the runtimes whose
// sockets should be closed to get back under MAX_LIVE_CONNECTIONS. Sessions
// mid-generation (thinking) and the active session are never recycled; the
// least-recently-active idle ones are closed first (design D5).
export function selectRecyclableConnections(
  live: SessionRuntime[],
  activeConvId: string | null,
): SessionRuntime[] {
  if (live.length <= MAX_LIVE_CONNECTIONS) return [];
  const recyclable = live
    .filter((r) => !r.thinking && r.conversationId !== activeConvId)
    .sort((a, b) => a.lastActivity - b.lastActivity);
  const toClose = live.length - MAX_LIVE_CONNECTIONS;
  return recyclable.slice(0, toClose);
}
