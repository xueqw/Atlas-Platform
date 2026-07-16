#!/usr/bin/env node
/**
 * Deterministic tests for planner session isolation (change:
 * fix-planner-session-isolation-and-dag-jump, tasks 6.1).
 *
 * The page component routes each WebSocket event into the *owning* conversation's
 * runtime via reduceWsEvent, never into whatever session is focused. These tests
 * exercise that reducer directly — no browser, no backend, no timing flake — to
 * prove the isolation invariants from specs/conversational-agent-builder/spec.md:
 *
 *   - "thinking" only flips on the session that received the event
 *   - a streamed token never bleeds into another session's buffer
 *   - a "done" completion appends to the originating session, not the focused one
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_session_isolation.test.mjs
 *
 * Exit code 0 on success, 1 on first failing assertion.
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}
function eq(actual, expected, name) {
  check(actual === expected, `${name} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
}

// Two independent sessions, as the runtime Map would hold them.
const A = blankRuntime("conv-A");
const B = blankRuntime("conv-B");

// 1) thinking only flips on the session that received the event ----------------
reduceWsEvent(A, { type: "thinking_content", content: "想一下…" });
eq(A.thinking, true, "A is thinking after A receives thinking_content");
eq(B.thinking, false, "B is NOT thinking (isolation: no shared state)");
eq(A.thinkBuf, "想一下…", "A accumulates its own think buffer");
eq(B.thinkBuf, "", "B think buffer untouched");

// 2) tokens never bleed across sessions ----------------------------------------
reduceWsEvent(A, { type: "token", content: "Hello" });
reduceWsEvent(A, { type: "token", content: " world" });
eq(A.streamBuf, "Hello world", "A stream buffer accumulates A's tokens");
eq(B.streamBuf, "", "B stream buffer stays empty while A streams");
eq(A.thinking, false, "A stops thinking once tokens arrive");

// A token arriving for B must only touch B.
reduceWsEvent(B, { type: "token", content: "Bonjour" });
eq(B.streamBuf, "Bonjour", "B gets only its own token");
eq(A.streamBuf, "Hello world", "A buffer unaffected by B's token");

// 3) done completes the ORIGINATING session, not the focused one ---------------
// Simulate: user is focused on B, but A's generation finishes in the background.
const aEff = reduceWsEvent(A, { type: "done" });
eq(A.messages.length, 1, "A gets the completed assistant message");
eq(A.messages[0].role, "assistant", "A's message is an assistant turn");
// A's message is built from A's own think + stream buffers (A had a think buffer
// from step 1), never from B's "Bonjour".
eq(A.messages[0].content, "<think>想一下…</think>\nHello world", "A's message is A's own think+stream, not B's");
check(!A.messages[0].content.includes("Bonjour"), "A's message never contains B's stream");
eq(A.streamBuf, "", "A stream buffer cleared on done");
eq(A.thinkBuf, "", "A think buffer cleared on done");
eq(B.messages.length, 0, "B gets NO message from A's completion (return归属发起会话)");
check(aEff.refreshSessions === true, "done asks caller to refresh the sidebar");

// B finishing later appends only to B.
reduceWsEvent(B, { type: "done" });
eq(B.messages.length, 1, "B gets its own completion");
eq(B.messages[0].content, "Bonjour", "B's message is B's own stream");
eq(A.messages.length, 1, "A still has exactly its one message");

// 4) error clears only the target session's transient buffers ------------------
reduceWsEvent(A, { type: "thinking_content", content: "x" });
const errEff = reduceWsEvent(A, { type: "error", message: "boom" });
eq(A.thinking, false, "error stops A thinking");
eq(A.thinkBuf, "", "error clears A think buffer");
check(errEff.toast === "boom", "error surfaces a toast message to the caller");
eq(B.thinking, false, "B never affected by A's error");

// 5) memory_update / skills route to the owning session ------------------------
reduceWsEvent(A, {
  type: "memory_update",
  memory: { requirement_summary: "做个客服bot", confirmed_constraints: [], task_classification: "QA", latest_proposal_summary: "", user_feedback: [], selected_skills: ["web"] },
});
eq(A.memory?.requirement_summary, "做个客服bot", "A memory updated");
check(B.memory === null, "B memory still null");
eq(A.selectedSkills.join(","), "web", "A picks up selected_skills from memory_update");

// 6) unknown event is a no-op (no repaint) -------------------------------------
const noop = reduceWsEvent(A, { type: "totally_unknown" });
check(noop.changed === false, "unknown event reports changed=false (no repaint)");

if (failed > 0) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nALL PASS");
