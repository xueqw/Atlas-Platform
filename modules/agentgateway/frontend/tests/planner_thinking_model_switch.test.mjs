#!/usr/bin/env node
/**
 * Deterministic tests for the "thinking stuck after model switch" fix (change:
 * planner-activity-visibility-and-thinking-fix, tasks 1.x + 5.1).
 *
 * The bug: the chat could stay on "思考中..." forever (and keep the send button
 * disabled) when a control-plane echo arrived after a turn began but the turn's
 * token/done never landed (e.g. the socket dropped mid-turn and reconnected).
 *
 * The contract (spec conversational-agent-builder):
 *   - "thinking/working" is owned by the message turn: set true in handleSend,
 *     ended only by this turn's token / done / error.
 *   - control-plane echoes (model_resolved / set_model) MUST NOT change or hang it.
 *   - session_restored (only on a fresh socket) MUST finalize any zombie in-flight
 *     state, so a reconnect can't resurrect a stuck "思考中".
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/planner_thinking_model_switch.test.mjs
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// Mirror of page.tsx isWorking(rt): the single source of truth for "working".
const isWorking = (rt) =>
  rt.thinking || rt.streamBuf.length > 0 || rt.activities.some((a) => a.status === "start");

// 1) model_resolved when idle must NOT introduce thinking ----------------------
{
  const rt = blankRuntime("conv-idle");
  check(rt.thinking === false, "fresh runtime: not thinking");
  reduceWsEvent(rt, { type: "model_resolved", model: "gpt-5.4", provider: "openai" });
  check(rt.thinking === false, "model_resolved on idle session does NOT set thinking");
  check(rt.model === "gpt-5.4" && rt.provider === "openai", "model_resolved updates model/provider");
  check(isWorking(rt) === false, "idle session stays not-working after model switch");
}

// 2) model_resolved must NOT clear an in-flight turn's thinking ----------------
{
  const rt = blankRuntime("conv-inflight");
  reduceWsEvent(rt, { type: "thinking_content", content: "考虑中" }); // turn started
  check(rt.thinking === true, "thinking_content starts thinking");
  reduceWsEvent(rt, { type: "model_resolved", model: "gpt-5.4", provider: "openai" });
  check(rt.thinking === true, "model_resolved does NOT clear an in-flight turn's thinking");
  reduceWsEvent(rt, { type: "token", content: "答案" });
  check(rt.thinking === false, "token ends thinking as usual");
}

// 3) reconnect path: session_restored finalizes a zombie thinking --------------
//    (socket dropped mid-turn → onclose, no token/done; reconnect re-sends
//     session_restored + model_resolved — must NOT stay stuck.)
{
  const rt = blankRuntime("conv-zombie");
  reduceWsEvent(rt, { type: "thinking_content", content: "半截思考" });
  rt.streamBuf = "半截输出"; // simulate partial stream before the drop
  check(isWorking(rt) === true, "mid-turn session is working");
  // reconnect handshake:
  reduceWsEvent(rt, { type: "model_resolved", model: "qwen3.6-27b", provider: "glm" });
  reduceWsEvent(rt, { type: "session_restored", memory: null, stage: "clarifying", messages: [] });
  check(rt.thinking === false, "session_restored clears zombie thinking");
  check(rt.streamBuf === "" && rt.thinkBuf === "", "session_restored clears zombie buffers");
  check(isWorking(rt) === false, "after reconnect the session is no longer stuck working");
}

// 4) a turn that ends with `done` clears working as usual ----------------------
{
  const rt = blankRuntime("conv-done");
  reduceWsEvent(rt, { type: "thinking_content", content: "x" });
  reduceWsEvent(rt, { type: "token", content: "hi" });
  reduceWsEvent(rt, { type: "done" });
  check(rt.thinking === false && rt.streamBuf === "", "done clears thinking + stream");
  check(isWorking(rt) === false, "done → not working");
}

if (failed > 0) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log("\nAll thinking/model-switch tests passed");
