#!/usr/bin/env node
/**
 * Deterministic tests for the status presentation map (change:
 * add-frontend-design-primitives, tasks 2.3).
 *
 * Contract (spec frontend-design-system):
 *   - every known AgStatus maps to a presentation with a label + a
 *     --color-status-* token class + an icon + a tone;
 *   - an unknown status degrades to the neutral idle presentation, never throws.
 *
 * Run with:
 *   cd frontend && node --experimental-strip-types --no-warnings tests/status_presentation.test.mjs
 */
import { statusPresentation, AG_STATUSES } from "../src/lib/status-presentation.ts";

let failed = 0;
function check(cond, name) {
  if (cond) { console.log("OK  ", name); return; }
  failed++;
  console.error("FAIL", name);
}

check(AG_STATUSES.length === 11, "11 known statuses defined");

// 1) every known status has a complete, token-backed presentation -------------
for (const s of AG_STATUSES) {
  const p = statusPresentation(s);
  check(!!p.label, `${s}: has a label`);
  check(p.tokenClass === `text-status-${s}`, `${s}: token class is text-status-${s}`);
  check(typeof p.Icon === "function" || typeof p.Icon === "object", `${s}: has an icon`);
  check(["neutral", "info", "progress", "success", "warning", "danger"].includes(p.tone), `${s}: has a valid tone`);
}

// 2) live states spin --------------------------------------------------------
check(statusPresentation("running").live === true, "running is live (spins)");
check(statusPresentation("reconnecting").live === true, "reconnecting is live");
check(statusPresentation("passed").live === false, "passed is not live");

// 3) unknown status degrades to idle, never throws ---------------------------
{
  let threw = false;
  let p;
  try { p = statusPresentation("totally-unknown-state"); } catch { threw = true; }
  check(threw === false, "unknown status does not throw");
  check(p && p.label === statusPresentation("idle").label, "unknown status degrades to idle");
  check(p && p.tone === "neutral", "degraded fallback is neutral tone");
}

// 4) same status → identical presentation (consistency) ----------------------
{
  const a = statusPresentation("degraded");
  const b = statusPresentation("degraded");
  check(a.label === b.label && a.tokenClass === b.tokenClass && a.Icon === b.Icon,
    "same status yields the same label/token/icon");
}

if (failed > 0) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log("\nAll status-presentation tests passed");
