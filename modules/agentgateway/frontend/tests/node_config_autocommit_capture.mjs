// Focused re-run of turn (a): auto-apply commit + ring highlight capture.
import { chromium } from "playwright";
import path from "node:path";
import os from "node:os";

const EXEC = path.join(os.homedir(), ".cache/ms-playwright/chromium-1217/chrome-linux64/chrome");
const OUT = "/data/agentgateway/openspec/changes/dag-node-config-self-fill/samples/screenshots";
const BASE = "http://localhost:3000";
const API = "http://127.0.0.1:8000";
const AGENT_ID = 8;

async function latestVersion() {
  const r = await fetch(`${API}/api/agents/${AGENT_ID}/dag-graph`);
  return (await r.json()).version;
}

async function main() {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const v0 = await latestVersion();
  console.log("start version:", v0);

  await page.goto(`${BASE}/workbench/${AGENT_ID}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);

  const pNode = page.locator(".react-flow__node").filter({ hasText: /翻译|提示词|助手/ }).first();
  await pNode.waitFor({ timeout: 15000 });
  await pNode.dblclick();
  await page.waitForTimeout(1200);

  const chatInput = page.locator('input[placeholder="描述你想要的节点行为..."]');
  await chatInput.waitFor({ timeout: 10000 });
  for (let i = 0; i < 20 && (await chatInput.isDisabled()); i++) await page.waitForTimeout(500);

  await chatInput.fill("请把角色名改成『中英翻译官』，并把系统提示词写成一句专业翻译指引");
  await chatInput.press("Enter");

  // Poll for the committed note for up to 110s (extraction + chat are 2 LLM calls).
  let committed = false;
  for (let i = 0; i < 55; i++) {
    if (await page.locator("text=已自动应用到节点配置").count()) { committed = true; break; }
    await page.waitForTimeout(2000);
  }
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/B_auto_commit.png`, fullPage: false });
  const v1 = await latestVersion();
  console.log("committed note shown:", committed, "| version:", v1, v1 > v0 ? "BUMPED ✓" : "NOT bumped ✗");

  // Capture the ring highlight on the form (能力配置 tab) — flashes 1.5s after commit,
  // so switch tab quickly. Re-trigger a tiny commit won't re-flash old keys, so
  // we just show the form reflecting the new value.
  await page.getByRole("tab", { name: "能力配置" }).click().catch(() => {});
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/B_form_highlight.png`, fullPage: false });

  await browser.close();
  console.log("DONE");
}

main().catch((e) => { console.error(e); process.exit(1); });
