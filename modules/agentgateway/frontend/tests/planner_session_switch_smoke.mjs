// Regression smoke test for the planner session-isolation refactor (change:
// fix-planner-session-isolation-and-dag-jump, task 6.4). Verifies the page mounts
// cleanly, a fresh session connects, and switching sessions does not throw or
// leak a "thinking" bubble into another session.
//
// Run from frontend/ with the Next dev server on :3000 and backend on :8000:
//   node tests/planner_session_switch_smoke.mjs
import { chromium } from "playwright";
import path from "node:path";
import os from "node:os";

const EXEC = path.join(os.homedir(), ".cache/ms-playwright/chromium-1217/chrome-linux64/chrome");
const BASE = "http://localhost:3000";

async function main() {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));

  let failed = 0;
  const check = (cond, name) => { if (cond) { console.log("OK  ", name); } else { failed++; console.error("FAIL", name); } };

  await page.goto(`${BASE}/builder/deerflow`, { waitUntil: "domcontentloaded" });
  await page.locator("textarea").first().waitFor({ timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(2500);

  // Page mounted: composer + sidebar present.
  check(await page.locator("textarea").count() > 0, "composer textarea renders");
  check(await page.getByText("规划历史").count() > 0, "session history sidebar renders");

  // Create a second session via 新建, then a third — exercises the multi-runtime path.
  await page.getByRole("button", { name: /新建/ }).click().catch(() => {});
  await page.waitForTimeout(1200);
  await page.getByRole("button", { name: /新建/ }).click().catch(() => {});
  await page.waitForTimeout(1200);

  // Switch between the first two sessions in the sidebar; must not throw and must
  // not show a stray "思考中" (no generation is in flight).
  const rows = page.locator(".group");
  const n = await rows.count();
  check(n >= 1, `sidebar lists sessions (${n})`);
  if (n >= 2) {
    await rows.nth(1).click().catch(() => {});
    await page.waitForTimeout(600);
    await rows.nth(0).click().catch(() => {});
    await page.waitForTimeout(600);
  }
  const thinkingVisible = await page.getByText("思考中...").isVisible().catch(() => false);
  check(!thinkingVisible, "no stray 思考中 bubble after switching idle sessions");

  // No uncaught runtime / console errors during mount + switching.
  const fatal = errors.filter((e) => !/favicon|404|Failed to load resource/i.test(e));
  check(fatal.length === 0, `no runtime errors (${fatal.length})`);
  if (fatal.length) fatal.slice(0, 5).forEach((e) => console.error("   ⚠", e));

  await browser.close();
  if (failed > 0) { console.error(`\n${failed} check(s) failed`); process.exit(1); }
  console.log("\nALL PASS");
}

main().catch((e) => { console.error(e); process.exit(1); });
