#!/usr/bin/env node
/**
 * Lightweight assertion harness for `lib/sanitize.ts`.
 *
 * Why no vitest? frontend/package.json intentionally does not pull in a test
 * framework — adding one for one helper is overkill. This script exercises
 * the same scenarios required by docs/planner-fix-requirements.md §4.3:
 *
 *   - full <memory_update> block swallowed
 *   - partial open tag at the tail dropped
 *   - pure block becomes empty
 *   - multiple blocks all swallowed
 *   - unterminated open chops the rest
 *   - empty / no-tag inputs pass through
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/sanitize.test.mjs
 *
 * Exit code 0 on success, 1 on first failing assertion.
 */
import { stripMemoryUpdate } from "../src/lib/sanitize.ts";

let failed = 0;
function eq(actual, expected, name) {
  if (actual === expected) {
    console.log("OK  ", name);
    return;
  }
  failed++;
  console.error("FAIL", name);
  console.error("  expected:", JSON.stringify(expected));
  console.error("  actual:  ", JSON.stringify(actual));
}

eq(
  stripMemoryUpdate("正文 <memory_update>{\"a\":1}</memory_update> 尾部"),
  "正文  尾部",
  "full block stripped (scenario A)",
);
eq(stripMemoryUpdate("hi<mem"), "hi", "partial open at boundary (scenario B)");
eq(stripMemoryUpdate("hi <mem"), "hi ", "partial open keeps preceding space");
eq(stripMemoryUpdate("hi <memory_"), "hi ", "longer partial open keeps preceding space");
eq(stripMemoryUpdate("<memory_update>{}</memory_update>"), "", "pure block becomes empty (scenario C)");
eq(
  stripMemoryUpdate("A<memory_update>1</memory_update>B<memory_update>2</memory_update>C"),
  "ABC",
  "multiple blocks (scenario D)",
);
eq(stripMemoryUpdate("A<memory_update>{abc"), "A", "unterminated open chops");
eq(stripMemoryUpdate(""), "", "empty");
eq(stripMemoryUpdate("plain text"), "plain text", "no tag passes through");

if (failed > 0) {
  console.error(`\n${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("\nALL PASS");
