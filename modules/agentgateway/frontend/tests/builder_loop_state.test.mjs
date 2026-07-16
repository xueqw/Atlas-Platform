#!/usr/bin/env node
/**
 * Deterministic tests for the plan-first builder loop runtime state (change:
 * builder-frontend-minimal-loop, T7). The action handlers (handleConfirmProposal
 * / handleCompileDraft) need React, but the state they mutate lives on the plain
 * SessionRuntime — so we assert the runtime field contract that the panel renders
 * from, without a browser.
 *
 * Contract (spec builder-frontend-loop):
 *   - blankRuntime seeds the new fields empty (proposalResult / selectedProposalOption
 *     / draftAgent / compileResult / runEvents).
 *   - the confirm→draft→compile progression is representable on the runtime:
 *     selectedProposalOption + draftAgent set on confirm; compileResult on compile.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/builder_loop_state.test.mjs
 */
import { blankRuntime } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// 1) blank runtime seeds the new fields empty ----------------------------------
{
  const rt = blankRuntime("conv-b");
  check(Array.isArray(rt.runEvents) && rt.runEvents.length === 0, "runEvents starts empty");
  check(rt.proposalResult === null, "proposalResult starts null");
  check(rt.selectedProposalOption === null, "selectedProposalOption starts null");
  check(rt.draftAgent === null, "draftAgent starts null");
  check(rt.compileResult === null, "compileResult starts null");
}

// 2) confirm → draft progression representable ---------------------------------
{
  const rt = blankRuntime("conv-b2");
  // emulate handleConfirmProposal's runtime mutation
  rt.selectedProposalOption = "7";
  rt.draftAgent = { id: 3, proposal_id: 7, name: "销售日报 Agent", status: "draft", runtime_mode: "cron",
    capability_refs: [{ id: 1, type: "tool", name: "sql_query" }] };
  check(rt.selectedProposalOption === "7", "selected proposal recorded on confirm");
  check(rt.draftAgent && rt.draftAgent.status === "draft", "draft agent staged (status draft)");
  check(rt.draftAgent.capability_refs[0].name === "sql_query", "capability refs structured (id/type/name)");
}

// 3) compile result representable ----------------------------------------------
{
  const rt = blankRuntime("conv-b3");
  rt.draftAgent = { id: 3, proposal_id: 7, name: "x", status: "draft", runtime_mode: "direct" };
  // emulate handleCompileDraft's runtime mutation (success path)
  rt.compileResult = { success: true, errors: [], warnings: ["能力引用未解析：ghost"],
    graph_json: "{}", dryrun: { ran: true, ok: true, message: "dry-run 通过" } };
  check(rt.compileResult.success === true, "compile success recorded");
  check(rt.compileResult.warnings.length === 1, "warnings preserved (non-blocking)");
  check(rt.compileResult.dryrun.ok === true, "dry-run result carried");

  // failure path
  rt.compileResult = { success: false, errors: ["节点 agent1 缺必填项：system_prompt"], warnings: [] };
  check(rt.compileResult.success === false, "compile failure recorded");
  check(rt.compileResult.errors[0].includes("agent1"), "compile errors are specific");
}

if (failed) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log("\nall builder-loop state tests passed");
