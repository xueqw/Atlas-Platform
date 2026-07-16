// Headless Playwright capture of the 4 new planner features. Run from frontend/:
//   node tests/planner_attachments_e2e.mjs
// Requires the Next dev server on :3000 and the backend on :8000.
import { chromium } from "playwright";
import path from "node:path";
import os from "node:os";

const EXEC = path.join(os.homedir(), ".cache/ms-playwright/chromium-1217/chrome-linux64/chrome");
const OUT = "/data/agentgateway/openspec/changes/planner-attachments-and-skill-library/samples/screenshots";
const BASE = "http://localhost:3000";

const PNG_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

async function main() {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // ── A: 能力库 skill 类型 ──
  await page.goto(`${BASE}/capabilities`, { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "技能" }).click().catch(() => {});
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}/A_capabilities_skill_type.png`, fullPage: false });
  console.log("A captured");

  // ── Planner page ──
  await page.goto(`${BASE}/builder/deerflow`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);

  // D: 规划师能力 popover
  await page.getByRole("button", { name: /规划师能力/ }).click().catch(() => {});
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/D_planner_self_skills.png`, fullPage: false });
  console.log("D captured");
  // close popover
  await page.mouse.click(700, 400);
  await page.waitForTimeout(400);

  // B: 附件上传（非多模态默认 qwen → 已忽略蒙层）
  const buf = Buffer.from(PNG_B64, "base64");
  const fileInput = page.locator('input[type="file"]');
  await fileInput.setInputFiles({ name: "mock.png", mimeType: "image/png", buffer: buf }).catch((e) => console.log("upload err", e.message));
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${OUT}/B_attachment_ignored_overlay.png`, fullPage: false });
  console.log("B captured");

  // C: session 删除二次确认
  const row = page.locator(".group").first();
  await row.hover().catch(() => {});
  await page.waitForTimeout(300);
  await row.locator('button[title="删除会话"]').click().catch((e) => console.log("trash err", e.message));
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/C_session_delete_confirm.png`, fullPage: false });
  console.log("C captured");

  await browser.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
