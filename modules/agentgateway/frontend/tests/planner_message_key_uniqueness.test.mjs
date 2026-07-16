#!/usr/bin/env node
/**
 * Deterministic tests for planner message id/key uniqueness (change:
 * fix-planner-message-key-and-dev-perf, tasks 2.1 / 2.2).
 *
 * The bug: hydration (session_restored) used positional ids (i + 1) and reset
 * rt.msgId to length, while incremental turns use the monotonic rt.msgId++. A
 * WebSocket reconnect re-sends session_restored, so a second hydrate landed back
 * on 1..n and collided with already-rendered incremental messages → React
 * "two children with the same key '1'".
 *
 * The fix (D1 + D2): every message id comes from the per-runtime monotonic,
 * never-reused counter, and session_restored only paints the snapshot when the
 * runtime has no messages yet (idempotent). These tests exercise reduceWsEvent
 * directly — no browser, no backend — to prove the spec invariants from
 * specs/conversational-agent-builder/spec.md.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_message_key_uniqueness.test.mjs
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
function allDistinct(ids) {
  return new Set(ids).size === ids.length;
}

const snapshot = (n) =>
  Array.from({ length: n }, (_, i) => ({
    role: i % 2 === 0 ? "user" : "assistant",
    content: `历史消息 ${i + 1}`,
  }));

// 2.1) hydrate N → append incremental → re-restore (reconnect): all ids distinct
{
  const rt = blankRuntime("conv-reconnect");

  // Initial hydrate of 3 history messages.
  reduceWsEvent(rt, { type: "session_restored", messages: snapshot(3) });
  eq(rt.messages.length, 3, "2.1 hydrate paints 3 history messages");
  check(allDistinct(rt.messages.map((m) => m.id)), "2.1 hydrated ids are distinct");

  // User sends + assistant completes (incremental, monotonic counter).
  rt.msgId++;
  rt.messages = [...rt.messages, { id: rt.msgId, role: "user", content: "新问题" }];
  reduceWsEvent(rt, { type: "token", content: "回答" });
  reduceWsEvent(rt, { type: "done" });
  eq(rt.messages.length, 5, "2.1 two incremental messages appended (user + assistant)");
  check(allDistinct(rt.messages.map((m) => m.id)), "2.1 ids still distinct after increments");

  // WebSocket reconnect re-sends the (now stale, 3-message) snapshot.
  reduceWsEvent(rt, { type: "session_restored", messages: snapshot(3) });
  eq(rt.messages.length, 5, "2.1 reconnect snapshot does NOT clobber the 5 live messages");
  check(allDistinct(rt.messages.map((m) => m.id)), "2.1 NO duplicate ids after reconnect re-restore");
}

// 2.2) two consecutive session_restored: idempotent, no dup ids, no overwrite
{
  const rt = blankRuntime("conv-double-restore");

  reduceWsEvent(rt, { type: "session_restored", messages: snapshot(4) });
  const firstIds = rt.messages.map((m) => m.id);
  eq(rt.messages.length, 4, "2.2 first restore paints 4 messages");

  // Append one incremental assistant turn so the runtime is non-empty.
  reduceWsEvent(rt, { type: "token", content: "增量" });
  reduceWsEvent(rt, { type: "done" });
  eq(rt.messages.length, 5, "2.2 incremental assistant message appended");
  const incrementalId = rt.messages[4].id;

  // Second restore (e.g. duplicate snapshot) must be a no-op on messages.
  reduceWsEvent(rt, { type: "session_restored", messages: snapshot(4) });
  eq(rt.messages.length, 5, "2.2 second restore is idempotent — does not re-append or overwrite");
  check(allDistinct(rt.messages.map((m) => m.id)), "2.2 no duplicate ids after double restore");
  eq(rt.messages[4].id, incrementalId, "2.2 incremental message id preserved (not overwritten)");
  check(
    firstIds.every((id, i) => rt.messages[i].id === id),
    "2.2 originally hydrated ids unchanged"
  );
}

// 2.2b) restore into an empty runtime still hydrates (idempotent ≠ never paints)
{
  const rt = blankRuntime("conv-empty");
  reduceWsEvent(rt, { type: "session_restored", messages: snapshot(2) });
  eq(rt.messages.length, 2, "2.2b empty runtime DOES hydrate from snapshot");
  check(rt.msgId >= 2, "2.2b msgId advanced past hydrated count");
}

if (failed > 0) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nALL PASS");
