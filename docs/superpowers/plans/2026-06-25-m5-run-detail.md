# M5 Run Detail Drawer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-task "运行详情" drawer to the workbench that visualizes every run's plan, tool policy, and step-by-step audit trail — frontend only, no backend changes.

**Architecture:** A self-contained `RunDrawer` component fetches `GET /api/conversations/{id}/runs` (already workspace-scoped, already embeds `steps[]`+`plan_json`), lists the task's runs newest-first, and renders the selected run's step timeline with per-step expandable raw input/output/error. A button in the workbench stage-head toggles it.

**Tech Stack:** React + TS + Vite. No JS unit-test harness in this repo, so the verification gates are `tsc --noEmit` + `vite build` (type safety) plus a read-only live API-shape smoke. Spec: `docs/superpowers/specs/2026-06-25-m5-run-detail-design.md`.

## Global Constraints

- Branch: `feature/m3-skill-hub` (continues M1–M4; not main). Do NOT commit `.codegraph/.gitignore`.
- Frontend gates (run from `web/`): `npx tsc --noEmit && npx vite build` — both must pass after every task that changes TS.
- ZERO backend changes. The endpoint `GET /api/conversations/{conversation_id}/runs` returns `WorkflowRunOut[]`; each run already includes `steps[]` and `plan_json` (a JSON **string**).
- All `*_json` fields (`plan_json`, step `input_json`/`output_json`) are JSON **strings** — always `JSON.parse` inside try/catch; on failure degrade to showing the raw string, never crash.
- Frontend is highly compressed single-line JSX — match the existing style; do not reformat neighboring code.
- Existing backend suite (27 tests) is untouched and must stay green; existing frontend must keep building.

---

### Task 1: Types + API client

**Files:**
- Modify: `web/src/types.ts` (append `WorkflowStep`, `WorkflowRun`)
- Modify: `web/src/api.ts` (import `WorkflowRun`, add `listRuns`)

**Interfaces:**
- Produces: `WorkflowStep` and `WorkflowRun` types (mirror backend `WorkflowStepOut`/`WorkflowRunOut`); `listRuns(id: string): Promise<WorkflowRun[]>`.

- [ ] **Step 1: Append the types**

Append to `web/src/types.ts`:

```typescript
export type WorkflowStep={id:string;index:number;type:string;title:string;executor:string;skill_id:string|null;status:string;input_json:string;output_json:string;error:string;started_at:string|null;ended_at:string|null}
export type WorkflowRun={id:string;conversation_id:string|null;agent_id:string|null;user_id:string|null;input_text:string;status:string;plan_json:string;output_json:string;error:string;started_at:string|null;ended_at:string|null;created_at:string;steps:WorkflowStep[]}
```

- [ ] **Step 2: Add the API call**

In `web/src/api.ts`, the first line imports types: `import type{Account,Agent,...,Source}from'./types'`. Add `WorkflowRun` to that import list (alphabetical-ish, next to `Source` is fine):

```typescript
import type{Account,Agent,Connector,Conversation,KnowledgeBase,Me,ModelCatalog,ModelTestResult,Plan,Skill,Source,WorkflowRun}from'./types'
```

Then add this export next to `listConversations` / `getConversation` (near api.ts line 9):

```typescript
export const listRuns=(id:string)=>json<WorkflowRun[]>(`/api/conversations/${id}/runs`)
```

- [ ] **Step 3: Verify it compiles**

Run (from `web/`): `npx tsc --noEmit`
Expected: PASS (no output). `WorkflowRun`/`WorkflowStep` resolve; `listRuns` typed.

- [ ] **Step 4: Commit**

```bash
git add web/src/types.ts web/src/api.ts
git commit -m "feat(web): WorkflowRun/WorkflowStep types + listRuns api"
```

---

### Task 2: `RunDrawer` component + workbench wiring + CSS

**Files:**
- Modify: `web/src/App.tsx` (import `listRuns` + `WorkflowRun`/`WorkflowStep`; add `RunDrawer`; add drawer button + state in `Workbench`)
- Modify: `web/src/styles.css` (drawer styles)

**Interfaces:**
- Consumes: `listRuns` (Task 1), `WorkflowRun`/`WorkflowStep`.
- Produces: `RunDrawer({conversationId,title,onClose})` component; a `drawerOpen` boolean in `Workbench`.

- [ ] **Step 1: Extend the App.tsx imports**

In `web/src/App.tsx`, add `listRuns` to the `./api` import list (line 2, next to `listConversations`), and add `WorkflowRun`/`WorkflowStep` to the `./types` import (line 3, next to `Source`):

```tsx
// line 2: add listRuns into the existing import { ... } from './api'
// line 3: add WorkflowRun,WorkflowStep into the existing import type { ... } from './types'
```

The line-3 result should read `...,SelectedSkill,Skill,Source,WorkflowRun,WorkflowStep}from'./types'`.

- [ ] **Step 2: Add the `RunDrawer` component**

Insert this component into `web/src/App.tsx` just before the `function Workbench(` definition (line ~51):

```tsx
function RunDrawer({conversationId,title,onClose}:{conversationId:string;title:string;onClose:()=>void}){
 const[runs,setRuns]=useState<WorkflowRun[]>([]),[sel,setSel]=useState(''),[expanded,setExpanded]=useState(''),[err,setErr]=useState(''),[loading,setLoading]=useState(true)
 useEffect(()=>{listRuns(conversationId).then(rs=>{const sorted=[...rs].sort((a,b)=>b.created_at.localeCompare(a.created_at));setRuns(sorted);setSel(sorted[0]?.id||'')}).catch(e=>setErr(e instanceof Error?e.message:'加载失败')).finally(()=>setLoading(false))},[conversationId])
 const parse=(s:string):any=>{try{return s?JSON.parse(s):null}catch{return s||null}}
 const dur=(a:string|null,b:string|null)=>a&&b?`${((+new Date(b)-+new Date(a))/1000).toFixed(1)}s`:'—'
 const stType=(t:string)=>t==='retrieve'?'检索':t==='tool'?'工具':t==='skill'?'技能':'回答'
 const stCls=(s:string)=>s==='succeeded'?'ok':s==='failed'?'bad':s==='waiting_confirmation'?'wait':s==='cancelled'?'cancel':'run'
 const block=(label:string,v:any)=>v==null?null:<div className="rd-block"><label>{label}</label><pre>{typeof v==='string'?v:JSON.stringify(v,null,2)}</pre></div>
 const run=runs.find(r=>r.id===sel)||null
 const plan=run?parse(run.plan_json):null
 const policy=plan&&typeof plan==='object'?plan.tool_policy:null
 const skills=plan&&typeof plan==='object'&&Array.isArray(plan.skills)?plan.skills:[]
 return <div className="run-drawer-mask" onClick={onClose}><aside className="run-drawer" onClick={e=>e.stopPropagation()}>
   <header><strong>运行详情 · {title}</strong><button className="rd-close" onClick={onClose}>×</button></header>
   {loading?<p className="rd-empty">加载中…</p>:err?<p className="rd-empty">加载失败：{err}</p>:runs.length===0?<p className="rd-empty">该任务暂无运行记录</p>:<div className="rd-body">
     <div className="rd-runs">{runs.map(r=><button key={r.id} className={`rd-run${r.id===sel?' on':''}`} onClick={()=>{setSel(r.id);setExpanded('')}}><i className={`rd-dot rd-${stCls(r.status)}`}/><span><b>{r.input_text||'(空)'}</b><small>{new Date(r.created_at).toLocaleString()} · {r.steps.length}步</small></span></button>)}</div>
     {run&&<div className="rd-detail">
       <div className="rd-head"><h4>{(plan&&plan.goal)||run.input_text}</h4><span className={`rd-badge rd-${stCls(run.status)}`}>{run.status}</span></div>
       {policy&&<p className="rd-policy">🔧 {(policy.tools||[]).length} 工具 · {(policy.write_tools||[]).length} 写</p>}
       {skills.length>0&&<div className="rd-skills">{skills.map((s:any)=><span key={s.id} className={`skill-chip skill-${s.source}`}>♧ {s.name}<i>{s.source==='manual'?'手动':'自动'}</i></span>)}</div>}
       <ol className="rd-steps">{run.steps.map((st:WorkflowStep)=>{const open=expanded===st.id;const inp=parse(st.input_json),out=parse(st.output_json);return <li key={st.id} className={`rd-step plan-${st.type}`}><button className="rd-step-row" onClick={()=>setExpanded(open?'':st.id)}><span className="plan-step-type">{stType(st.type)}</span><b>{st.title}</b><em className={`rd-badge rd-${stCls(st.status)}`}>{st.status}</em><small>{dur(st.started_at,st.ended_at)}</small></button>{open&&<div className="rd-step-detail">{block('入参',inp)}{block('结果',out)}{st.error&&block('错误',st.error)}{inp==null&&out==null&&!st.error&&<small className="rd-empty">无记录</small>}</div>}</li>})}</ol>
     </div>}
   </div>}
 </aside></div>
}
```

- [ ] **Step 3: Add drawer state to `Workbench`**

The `Workbench` body starts (line ~52) with `const quick=...` and `const activeAgent=...`. Add a state line right after `const activeAgent=agents.find(item=>item.id===selectedAgent)`:

```tsx
 const[drawerOpen,setDrawerOpen]=useState(false)
```

- [ ] **Step 4: Add the stage-head button + mount the drawer**

In `Workbench`'s JSX, find the stage-head trailing button (the `◌` placeholder):

```tsx
{activeAgent&&<span className="active-agent-pill"><b>{activeAgent.name.slice(0,1)}</b>{activeAgent.name}</span>}</div><button>◌</button></div>
```

Replace `<button>◌</button>` with a 运行详情 trigger (only meaningful when a task exists) and keep the layout:

```tsx
{activeAgent&&<span className="active-agent-pill"><b>{activeAgent.name.slice(0,1)}</b>{activeAgent.name}</span>}</div>{current&&<button className="stage-runs" onClick={()=>setDrawerOpen(true)}>☰ 运行详情</button>}</div>
```

Then mount the drawer at the very end of the Workbench return, right before the final `</section></div>` (the outer wrapper close). Find the tail `</div></section></div>` of the `Workbench` return and insert the drawer just before `</section></div>`:

```tsx
{drawerOpen&&current&&<RunDrawer conversationId={current.id} title={current.title} onClose={()=>setDrawerOpen(false)}/>}
```

(Concretely: the Workbench return ends with `...对话自动保存到最近任务</small></div></section></div>`. Insert the line so it becomes `...对话自动保存到最近任务</small></div>{drawerOpen&&current&&<RunDrawer conversationId={current.id} title={current.title} onClose={()=>setDrawerOpen(false)}/>}</section></div>`.)

- [ ] **Step 5: Add CSS**

Append to `web/src/styles.css`:

```css
/* M5 run detail drawer */
.stage-runs{border:1px solid var(--line);background:#fff;border-radius:8px;padding:5px 10px;font-size:12px;color:#3a4660;cursor:pointer}
.stage-runs:hover{border-color:#9faff0;color:var(--blue);background:#f5f8ff}
.run-drawer-mask{position:fixed;inset:0;background:#17233a33;z-index:40;display:flex;justify-content:flex-end}
.run-drawer{width:460px;max-width:92vw;height:100%;background:#fff;box-shadow:-10px 0 30px #17233a1f;display:flex;flex-direction:column;animation:rd-in .18s ease}
@keyframes rd-in{from{transform:translateX(30px);opacity:.4}to{transform:none;opacity:1}}
.run-drawer>header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid #eef1f6}
.run-drawer>header strong{font-size:14px}
.rd-close{border:none;background:none;font-size:20px;line-height:1;cursor:pointer;color:var(--muted)}
.rd-empty{color:var(--muted);font-size:12px;text-align:center;padding:24px}
.rd-body{flex:1;overflow-y:auto;padding:10px 14px}
.rd-runs{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
.rd-run{display:flex;gap:9px;align-items:flex-start;text-align:left;border:1px solid transparent;border-radius:9px;padding:8px 9px;background:#f7f9fc;cursor:pointer}
.rd-run:hover{background:#eef3ff}.rd-run.on{border-color:#c7d4ff;background:#eef3ff}
.rd-run span{display:flex;flex-direction:column;min-width:0}
.rd-run b{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:330px}
.rd-run small{color:var(--muted);font-size:10px;margin-top:2px}
.rd-dot{width:8px;height:8px;border-radius:50%;margin-top:5px;flex:none}
.rd-ok{background:#3a9d5d}.rd-bad{background:#d24b3f}.rd-wait{background:#d2682f}.rd-cancel{background:#9aa4b5}.rd-run.rd-run{}
.rd-dot.rd-run{background:var(--blue)}
.rd-detail{border-top:1px solid #eef1f6;padding-top:10px}
.rd-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.rd-head h4{font-size:13px;margin:0}
.rd-badge{font-style:normal;font-size:10px;border-radius:6px;padding:1px 6px;color:#fff;background:var(--blue)}
.rd-badge.rd-ok{background:#3a9d5d}.rd-badge.rd-bad{background:#d24b3f}.rd-badge.rd-wait{background:#d2682f}.rd-badge.rd-cancel{background:#9aa4b5}
.rd-policy{font-size:11px;color:var(--muted);margin:6px 0}
.rd-skills{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.rd-steps{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.rd-step{border:1px solid #eef1f6;border-radius:9px;overflow:hidden}
.rd-step-row{width:100%;display:flex;align-items:center;gap:8px;padding:8px 9px;background:#fff;border:none;cursor:pointer;text-align:left}
.rd-step-row:hover{background:#f7f9fc}
.rd-step-row b{font-size:12px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rd-step-row small{color:var(--muted);font-size:10px}
.rd-step-detail{padding:8px 10px;background:#f7f9fc;border-top:1px solid #eef1f6}
.rd-block label{font-size:10px;color:var(--muted);display:block;margin:4px 0 2px}
.rd-block pre{margin:0;font-size:11px;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow:auto;background:#fff;border:1px solid #eef1f6;border-radius:6px;padding:6px}
```

- [ ] **Step 6: Verify gates**

Run (from `web/`): `npx tsc --noEmit && npx vite build`
Expected: both succeed.

- [ ] **Step 7: Commit**

```bash
git add web/src/App.tsx web/src/styles.css
git commit -m "feat(web): per-task run detail drawer (plan + tool policy + step audit)"
```

---

### Task 3: Verification

**Files:** none (verification only)

- [ ] **Step 1: Frontend gates**

Run (from `web/`): `npx tsc --noEmit && npx vite build`
Expected: clean.

- [ ] **Step 2: Backend untouched**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest -q`
Expected: 27 passed (no backend change; sanity only).

- [ ] **Step 3: Live API-shape smoke (read-only)**

Ensure backend is running on :8000 (M4 build). With `api/.venv/Scripts/python.exe` + httpx: login admin → pick (or create) a conversation that has at least one run (e.g. stream "你好" once) → `GET /api/conversations/{id}/runs` → assert: response is a non-empty list; `runs[0]` has keys `status`, `plan_json`, `steps`; `json.loads(runs[0]["plan_json"])` yields a dict (has `goal`/`steps`, and `tool_policy` when connectors were used); each `steps[i]` has `type`, `status`, `started_at`. Print the run's step types so the drawer's data source is confirmed.

> This validates the exact shape `RunDrawer` consumes. UI rendering itself is covered by `tsc` type-checking (the project on E: makes the browser-preview MCP cross-drive-limited; a manual open at http://localhost:5173 is optional).

- [ ] **Step 4: Final commit (if any cleanup)**

```bash
git add -A && git commit -m "chore: M5 run detail verification pass"
```

---

## Self-Review

**Spec coverage:** spec §2 data source → Task 1 (`listRuns`); §3 components (entry button, RunDrawer, run list, run header, step timeline, expandable detail) → Task 2; §4 data flow + error/degrade handling → Task 2 (`parse` try/catch returns raw string, loading/err/empty states); §5 files → Tasks 1+2; §6 testing → Task 3. No gaps.

**Placeholder scan:** every code step shows full code; no TBD/TODO. No JS unit test exists by design (no harness in repo) — gates are tsc/build + a live shape smoke, stated explicitly. Not a hidden placeholder.

**Type consistency:** `WorkflowRun`/`WorkflowStep` field names (`input_text`, `plan_json`, `input_json`, `output_json`, `started_at`, `ended_at`, `created_at`, `steps`) are identical across types.ts (Task 1), api.ts (Task 1), and RunDrawer (Task 2). `listRuns(id)=>Promise<WorkflowRun[]>` signature identical in api.ts and the RunDrawer `useEffect`. `RunDrawer({conversationId,title,onClose})` prop names match the mount site in Workbench (Task 2 Step 4). Status class helper `stCls` outputs (`ok|bad|wait|cancel|run`) match the CSS classes `.rd-ok/.rd-bad/.rd-wait/.rd-cancel` and the `.rd-badge.rd-*` / `.rd-dot.rd-run` rules in Step 5.
