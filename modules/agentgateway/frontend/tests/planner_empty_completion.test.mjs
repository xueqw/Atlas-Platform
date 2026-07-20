#!/usr/bin/env node
/**
 * Regression coverage for planner-continuation-reliability:
 * empty completions never become assistant turns or advance proposal stages.
 */
import { blankRuntime, reduceWsEvent } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// Empty done is a recoverable failure and preserves the prior stage.
{
  const rt = blankRuntime("empty");
  rt.stage = "awaiting_confirmation";
  rt.thinking = true;
  rt.thinkBuf = "provider-only reasoning";
  const effect = reduceWsEvent(rt, { type: "done" });
  check(rt.messages.length === 0, "empty done does not append an assistant turn");
  check(rt.msgId === 0, "empty done does not consume a message id");
  check(rt.stage === "awaiting_confirmation", "empty done preserves the current stage");
  check(rt.thinking === false, "empty done clears the working indicator");
  check(effect.toast?.includes("未返回可用内容"), "empty done surfaces a recoverable error");
}

// A structured proposal remains a meaningful result even when no text token was
// streamed; it is represented by the proposal file chip.
{
  const rt = blankRuntime("proposal");
  reduceWsEvent(rt, {
    type: "final_summary",
    text: "方案完成",
    proposal: {
      architecture_summary: "single agent",
      nodes: [],
      edges: [],
      rationale: "test",
    },
    files: [{ path: "proposal.json", filename: "proposal.json", kind: "proposal" }],
  });
  reduceWsEvent(rt, { type: "done" });
  check(rt.messages.length === 1, "structured proposal creates one assistant turn");
  check(rt.messages[0].proposalFile === "proposal.json", "proposal turn carries its artifact chip");
  check(rt.stage === "ready_to_apply", "validated proposal advances to ready");
}

// A summary without proposal evidence must not synthesize proposal.json.
{
  const rt = blankRuntime("summary-only");
  reduceWsEvent(rt, { type: "final_summary", text: "普通摘要", files: [] });
  check(rt.pendingProposalFile === null, "text summary does not synthesize a proposal artifact");
  check(rt.stage === "clarifying", "text summary does not advance proposal stage");
}

// Reconnect filters legacy empty assistant rows while preserving user evidence.
{
  const rt = blankRuntime("legacy");
  reduceWsEvent(rt, {
    type: "session_restored",
    stage: "clarifying",
    messages: [
      { role: "user", content: "做一个选股 Agent" },
      { role: "assistant", content: "   " },
      { role: "assistant", content: "请补充投资范围" },
    ],
  });
  check(rt.messages.length === 2, "legacy restore drops only the empty assistant row");
  check(rt.messages[0].role === "user", "legacy restore keeps original user evidence");
  check(rt.messages[1].content === "请补充投资范围", "legacy restore keeps meaningful assistant content");
}

// Awaiting-confirmation is driven by a structured control request.
{
  const rt = blankRuntime("a2ui");
  reduceWsEvent(rt, {
    type: "a2ui_request",
    id: "confirm-1",
    prompt: "确认创建？",
    options: [{ id: "confirm", label: "确认创建" }],
  });
  check(rt.stage === "awaiting_confirmation", "A2UI request supplies confirmation-stage evidence");
  const effect = reduceWsEvent(rt, { type: "done" });
  check(rt.messages.length === 0, "structured A2UI-only turn does not create a blank assistant bubble");
  check(!effect.toast, "structured A2UI-only turn is not misreported as an empty-model failure");
  check(rt.a2uiRequest?.id === "confirm-1", "structured A2UI request remains pending after done");
}

// A decision recorded before a disconnect restores as a one-shot continuation
// command; the runtime layer consumes this reducer effect by replaying the
// stable token on the newly connected socket.
{
  const rt = blankRuntime("continuation-recovery");
  const pending = {
    request_id: "confirm-restore",
    choice: "confirm",
    choice_label: "确认创建",
    token: "stable-token",
    content: "已选择「确认创建」",
    status: "pending",
  };
  const effect = reduceWsEvent(rt, {
    type: "session_restored",
    messages: [{ role: "assistant", content: "请确认方案" }],
    pending_continuation: pending,
  });
  check(effect.resumeContinuation?.token === "stable-token", "restore requests automatic continuation replay");
  check(rt.pendingContinuation?.request_id === "confirm-restore", "runtime keeps the durable continuation until dispatch");
  reduceWsEvent(rt, {
    type: "continuation_completed",
    continuation_token: "stable-token",
  });
  check(rt.pendingContinuation === null, "backend consumption clears the local pending continuation");
}

if (failed > 0) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll empty-completion tests passed");
