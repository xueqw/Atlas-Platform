# M5 设计 —— 任务运行详情抽屉(Run Detail)

> 状态:已批准(2026-06-25)。承接 M1/M2/M3/M4,在分支 `feature/m3-skill-hub` 上继续。
> 关联:PRD `docs/prd/atlas-agent-runtime-pipeline-prd.md` §4.3(管理员治理工具调用 / 审计)、§8.2-8.3(Run / Step 数据模型)。

## 1. 目标与边界

给工作台每个任务加一个**「运行详情」抽屉**,把 M1-M4 攒下的台账(plan + tool_policy + 每步状态/耗时/读写/入参/结果)第一次完整可视化。**纯前端,零后端改动**。

**核心决策**(brainstorming 钉死):
1. **任务内右侧滑出抽屉**,不加导航页(YAGNI 砍掉全局 workspace 审计页)。
2. 抽屉列该任务**全部 run**(每轮一条),选中展开步骤时间线,每步可再展开看原始入参/结果/错误/耗时。
3. **后端完全不动**——`GET /api/conversations/{id}/runs` 已返回带 `steps[]`+`plan_json` 的完整 run 列表。

**YAGNI 砍掉**:全局 workspace 审计页、跨任务搜索/筛选、导出、角色门禁、实时刷新(打开即拉一次)、单 run 端点 `GET /api/workflows/runs/{id}`(抽屉不用它)。

## 2. 数据来源(已就绪,不改后端)

一次 `GET /api/conversations/{conversation_id}/runs`(已按 workspace 隔离)→ `WorkflowRunOut[]`,newest first(后端按 created_at desc?——实际按返回顺序,前端按 `created_at` 再排一次保证 newest first)。每个 run 含:

- `id / input_text / status / plan_json(字符串) / output_json / error / started_at / ended_at / created_at / steps[]`
- `status` 取值:`created|planning|running|waiting_confirmation|succeeded|failed|cancelled`
- 每个 `step`:`index / type(retrieve|skill|tool|respond) / title / executor / status / input_json(字符串) / output_json(字符串) / error / started_at / ended_at`
- `plan_json` 解析出:`goal`、`requires_knowledge`、`tool_policy.{tools,write_tools}`、`skills[]`

> `plan_json` 与 step 的 `input_json/output_json` 都是 JSON **字符串**,前端 `JSON.parse` 时 try/catch,失败则降级显示原始字符串。

## 3. 组件

- **入口**:任务 stage-head(标题行,现有 `.stage-head`)加一个「☰ 运行详情」按钮 → 置 `drawerOpen=true`。仅当 `current`(有任务)时显示。
- **`RunDrawer`**(新组件,右侧 fixed 滑出):
  - 顶部:任务标题 + 关闭按钮 `×`。
  - **run 列表**(newest first):每条 = 状态点(succeeded 绿 / failed 红 / waiting_confirmation 橙 / cancelled 灰 / 其余蓝)+ `input_text` 摘要(截断)+ 相对时间 + `steps.length` 步。点选高亮 `selectedRunId`。
  - **选中 run 详情**:
    - run 头:`goal`(plan.goal 回退 input_text)+ 状态徽章 + 工具策略概要文本(`N 工具 · M 写`,来自 tool_policy)+ 技能 chips(plan.skills,手动/自动标)。
    - **步骤时间线**:每步一行 = 类型标签(检索/技能/工具/回答)+ title + 状态 + 耗时(`ended_at-started_at` 秒,缺失显示 —)。点击该行 → 展开:解析 `input_json`、`output_json`、`error`,分块显示(工具 args+result、检索 hits、skill 注入)。
  - 复用已有标签/chip 样式族(`.plan-step`、`.tool-chip`、`.skill-chip`)。

## 4. 数据流

```
点「运行详情」→ drawerOpen=true → listRuns(current.id) → runs[]（按 created_at desc 排）
 → selectedRunId 默认 = runs[0].id → 渲染 run 头(parse plan_json)+ steps 时间线
 → 点某步 → expandedStepId 切换 → parse 该 step 的 input_json/output_json/error 显示
 → 点另一 run → 切 selectedRunId
 → × 或点遮罩 → drawerOpen=false
```

错误处理:`listRuns` 失败 → 抽屉内「加载失败：<msg>」;任意 `JSON.parse` 失败 → 该块降级显示原始字符串(整体不崩);空 run 列表 → 「该任务暂无运行记录」。

## 5. 文件

- 改 `web/src/types.ts`:加 `WorkflowStep`、`WorkflowRun` 类型(对齐 `WorkflowStepOut`/`WorkflowRunOut`)。
- 改 `web/src/api.ts`:加 `listRuns(conversationId: string): Promise<WorkflowRun[]>` → `GET /api/conversations/{id}/runs`。
- 改 `web/src/App.tsx`:`AppShell` 不动数据;在 `Workbench` 加 `drawerOpen`/`runs`/`selectedRunId`/`expandedStepId` 本地状态 + stage-head 按钮 + 挂 `RunDrawer`;新增 `RunDrawer` 组件(自洽,props = `conversationId`、`title`、`onClose`)。
- 改 `web/src/styles.css`:`.run-drawer`、`.run-drawer-mask`、run 列表/时间线/展开块样式。

> `RunDrawer` 自己 `useEffect` 拉数据(props 给 `conversationId`),保持 Workbench 不臃肿——符合「小而聚焦的单元」。

## 6. 测试

- `tsc --noEmit` + `vite build` 通过(类型保证渲染正确性)。
- 真服务器 live smoke(只读):用 admin 造一个带 github 工具的 run(M4 已验证可造),`GET /api/conversations/{id}/runs` 返回的 run 含 `steps`(retrieve/respond/tool 任意组合)且 `plan_json` 能解析出 `tool_policy` —— 断言数据 shape 足够喂抽屉。UI 本身靠 tsc 类型 + 手动可选(项目在 E: 盘,preview MCP 跨盘受限,不强制截图)。

## 7. 影响清单

- 新增:`RunDrawer` 组件(在 App.tsx 内,跟随现有「所有视图组件同文件」的约定)。
- 改:`web/src/types.ts`、`web/src/api.ts`、`web/src/App.tsx`、`web/src/styles.css`。
- 不改:任何后端文件。
