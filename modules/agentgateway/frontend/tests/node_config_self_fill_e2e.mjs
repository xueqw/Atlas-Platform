// Headless Playwright capture of B (dag-node-config-self-fill). Run from frontend/:
//   node tests/node_config_self_fill_e2e.mjs
// Requires Next dev server on :3000 and backend on :8000 with GLM creds.
//
// Flow:
//   (a) open workbench → double-click P node → Chat tab → send a request →
//       expect node_config_committed: ring highlight + form fields changed +
//       DAGGraph version bumped.
//   (b) toggle 自动应用 off → send another request → expect a proposed diff
//       card and NO form change until applied.
//   (c) click 应用 → form updates + version bumps again.
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
  const j = await r.json();
  return j.version;
}

async function main() {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") console.log("PAGE_ERR:", m.text()); });

  const v0 = await latestVersion();
  console.log("start version:", v0);

  await page.goto(`${BASE}/workbench/${AGENT_ID}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);

  // Ensure DAG mode (auto-enables when a graph exists). Open the P node panel.
  const pNode = page.locator(".react-flow__node").filter({ hasText: /翻译|提示词|助手/ }).first();
  await pNode.waitFor({ timeout: 15000 });
  await pNode.dblclick();
  await page.waitForTimeout(1200);

  // Chat tab is the default (对话配置). Wait for WS ready (input enabled).
  const chatInput = page.locator('input[placeholder="描述你想要的节点行为..."]');
  await chatInput.waitFor({ timeout: 10000 });
  for (let i = 0; i < 20 && (await chatInput.isDisabled()); i++) {
    await page.waitForTimeout(500);
  }

  // ── (a) auto-apply path ──
  await chatInput.fill("我要一个把中文翻译成英文的翻译助手，角色名叫翻译助手，语气正式专业");
  await chatInput.press("Enter");
  // Wait for the committed note to appear (server wrote the graph).
  await page.locator("text=已自动应用到节点配置").waitFor({ timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/B_auto_commit.png`, fullPage: false });
  const v1 = await latestVersion();
  console.log("after (a) version:", v1, v1 > v0 ? "BUMPED ✓" : "NOT bumped ✗");

  // Open the 能力配置 tab to show the ring highlight on the form fields.
  await page.getByRole("tab", { name: "能力配置" }).click().catch(() => {});
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/B_form_highlight.png`, fullPage: false });

  // ── (b) toggle auto-apply off, send another request → proposed card ──
  await page.getByRole("tab", { name: "对话配置" }).click().catch(() => {});
  await page.waitForTimeout(400);
  await page.getByRole("switch", { name: "自动应用" }).click().catch(() => {});
  await page.waitForTimeout(400);
  await chatInput.fill("把输出格式改成纯文本 text");
  await chatInput.press("Enter");
  await page.locator("text=建议配置变更").waitFor({ timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(800);
  const vBeforeApply = await latestVersion();
  await page.screenshot({ path: `${OUT}/B_proposed_diff.png`, fullPage: false });
  console.log("after (b) version:", vBeforeApply, vBeforeApply === v1 ? "UNCHANGED ✓" : "changed ✗");

  // ── (c) click 应用 → version bumps again ──
  await page.getByRole("button", { name: "应用" }).click().catch(() => {});
  await page.waitForTimeout(2500);
  const v2 = await latestVersion();
  console.log("after (c) version:", v2, v2 > vBeforeApply ? "BUMPED ✓" : "NOT bumped ✗");

  await browser.close();
  console.log("DONE");
}

main().catch((e) => { console.error(e); process.exit(1); });
