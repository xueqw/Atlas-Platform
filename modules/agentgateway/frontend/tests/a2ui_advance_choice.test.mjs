#!/usr/bin/env node
/**
 * Tests for isAdvanceChoice — decides whether an A2UI card choice should
 * auto-continue the planner turn (generate the proposal/JSON) instead of
 * waiting for the user to type a follow-up.
 *
 * Contract:
 *   - "proceed" intent (确认创建 / 同意 / yes / apply …) → true (auto-continue).
 *   - "modify"/negative intent (我要修改 / 取消 / no …) → false (record only),
 *     and a negative cue ALWAYS wins over a positive one (e.g. "不创建",
 *     "重新生成" must not auto-continue).
 *   - matches on both the visible label and the option id.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/a2ui_advance_choice.test.mjs
 */
import { isAdvanceChoice } from "../src/lib/planner-session.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

// 1) proceed-intent labels auto-continue ---------------------------------------
for (const label of ["确认创建", "确认", "生成方案", "同意", "继续", "批准", "采纳", "就这样", "没问题", "可以了", "开始构建"]) {
  check(isAdvanceChoice("opt", label) === true, `advance: "${label}"`);
}

// 2) proceed-intent ids (English) auto-continue --------------------------------
for (const id of ["yes", "confirm", "create", "apply", "proceed", "approve", "ok", "go"]) {
  check(isAdvanceChoice(id, "选项") === true, `advance id: "${id}"`);
}

// 3) modify / negative labels do NOT auto-continue -----------------------------
for (const label of ["我要修改", "修改", "调整方案", "重新规划", "再想想", "取消", "放弃", "返回", "否"]) {
  check(isAdvanceChoice("modify", label) === false, `record-only: "${label}"`);
}

// 4) negative cue beats positive cue (the tricky cases) ------------------------
check(isAdvanceChoice("opt", "不创建") === false, "negative wins: 不创建");
check(isAdvanceChoice("opt", "重新生成") === false, "negative wins: 重新生成");
check(isAdvanceChoice("opt", "取消创建") === false, "negative wins: 取消创建");
check(isAdvanceChoice("modify", "修改后确认") === false, "negative wins: 修改后确认");
check(isAdvanceChoice("no", "no, change it") === false, "negative wins: 'no, change it'");

// 5) neutral / unknown choices stay record-only (safe default) -----------------
check(isAdvanceChoice("a", "选项 A") === false, "unknown label → record-only");
check(isAdvanceChoice("b", "") === false, "empty label → record-only");
check(isAdvanceChoice("", "") === false, "all empty → record-only");

// 6) substring false-positive guards -------------------------------------------
// "yes" must match as a word but "eyes"/"keyboard" must not flip a neutral label.
check(isAdvanceChoice("opt", "eyes only") === false, "'eyes' is not 'yes'");
check(isAdvanceChoice("opt", "keyboard") === false, "'go' substring in word does not match");

if (failed) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll isAdvanceChoice tests passed");
