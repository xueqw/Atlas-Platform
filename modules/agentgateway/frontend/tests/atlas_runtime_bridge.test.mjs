#!/usr/bin/env node
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { atlasRuntimeBindingFromUrl } from "../src/lib/atlas-runtime-contract.ts";

assert.deepEqual(
  atlasRuntimeBindingFromUrl(7, "http://gateway/workbench/7?atlas_agent_id=atlas-a&atlas_gateway_agent_id=7&atlas_agent_version_id=v2", true),
  { binding: { agentId: "atlas-a", versionId: "v2" }, error: null },
  "an embedded workbench accepts an explicit, matching Atlas/Gateway mapping",
);
assert.equal(
  atlasRuntimeBindingFromUrl(8, "http://gateway/workbench/8?atlas_agent_id=atlas-a&atlas_gateway_agent_id=7", true).binding,
  null,
  "a mismatched Gateway mapping fails closed",
);
assert.deepEqual(
  atlasRuntimeBindingFromUrl(7, "http://gateway/workbench/7?atlas_agent_id=atlas-a", false),
  { binding: null, error: null },
  "standalone workbench keeps the legacy transport fallback",
);

const runtimeClient = readFileSync(new URL("../src/lib/atlas-runtime.ts", import.meta.url), "utf8");
for (const command of ["runtime.start", "runtime.subscribe", "runtime.cancel", "runtime.resume"]) {
  assert.match(runtimeClient, new RegExp(`dispatchAtlasRuntimeCommand\\(\\\"${command}\\\"`), `${command} is dispatched through the bridge`);
}
for (const field of ["interrupt_id", "nonce", "parameter_digest", "resource_version"]) {
  assert.match(runtimeClient, new RegExp(field), `confirmation resume preserves ${field}`);
}

const nodeDebug = readFileSync(new URL("../src/components/workbench/NodeDebugDialog.tsx", import.meta.url), "utf8");
assert.match(nodeDebug, /if \(runtime\.embedded\)/, "node debug selects Atlas Runtime only when embedded");
assert.match(nodeDebug, /debug-node/, "node debug retains the standalone REST fallback");

const testChat = readFileSync(new URL("../src/components/workbench/TestChatPanel.tsx", import.meta.url), "utf8");
assert.match(testChat, /MultiAgentRuntimePanel/, "embedded test chat renders the Atlas multi-agent runtime view");
assert.match(testChat, /runtimeEvents/, "runtime events feed the visible subagent projection");

const teamPanel = readFileSync(new URL("../src/components/workbench/MultiAgentRuntimePanel.tsx", import.meta.url), "utf8");
for (const label of ["ORCHESTRATOR", "SUBAGENTS", "REVIEWER SUBAGENT"]) {
  assert.match(teamPanel, new RegExp(label), `${label} is visible in the runtime mission control`);
}

console.log("Atlas Runtime Bridge assertions passed");
