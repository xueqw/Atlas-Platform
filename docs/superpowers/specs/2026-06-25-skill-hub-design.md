# M3 Skill Hub 设计

> 状态：待实现 · 日期：2026-06-25 · 对应 PRD：`docs/prd/atlas-agent-runtime-pipeline-prd.md` M3
> 前置：M1（Workflow Run 雏形）、M2（Strategy Agent 结构化规划）均已完成。

## 1. 目标

交付一个可独立验收的纵切片，落地用户反复强调的两个效果：

1. **Agent 自动选 skill**：Strategy Agent（skill router）根据用户请求，从 workspace 技能池里自动选 0..N 个合适的 skill。
2. **用户像选模型一样勾选 skill**：工作台对话框底部「Skills」多选下拉（带搜索框），勾选 = **强制启用**。

**手动与自动的关系是并集，不是限定**：
`最终生效 skill = 手动勾选 ∪ 自动选取`。手动是地板不是天花板——用户勾了 A，router 若判断还需要 B，最终带 `{A, B}`。

## 2. 范围

### 做（M3）
- `instruction` 型 skill：往「生成回答」步注入一段方法论 / 输出格式正文。
- workspace 级共享技能池 + 启动幂等灌入的内置 skill。
- Skill Hub 管理页（增删改查自定义 skill；内置只读）。
- 工作台 Skills 多选下拉（搜索框 + 勾选 + 跳管理页）。
- Strategy Agent 扩展为 skill router；选中 skill 持久化进 `plan_json`（= PRD「被选 Skill 记入 run logs」）。

### 不做（YAGNI，留后续）
- `workflow` / `tool-backed` / `code` 型 skill。
- `skill_versions` 版本表（字段折叠进 `skills` 行）。
- `agent_skills` 绑定及其 UI（PRD §4.2，后续）。
- skill 市场 / 导入。
- catalog 注入的 token 预算上限（skill 少时不需要，见 §9 已知约束）。
- `allow_auto`（"某 skill 永不被自动选"）——本切片用不上，刻意不加。

## 3. 调研对照：Codex skills 系统

Codex（openai/codex `docs/skills.md` → developers.openai.com/codex/skills）的机制与本设计同构，用于印证关键决策：

| 维度 | Codex | 本设计采纳 |
|---|---|---|
| skill 元数据 | `name` + `description`（description 写明何时该/不该触发） | `name` + `description` 为主依据；`trigger_phrases` 为可选弱信号 |
| 自动选 | **模型当路由**，渐进披露：先只注入 name+description，**选中后才读完整正文** | Strategy Agent 当 router；catalog 只给 name+description+trigger；选中后才注入 `content` |
| 手动选 | 显式 `/skills` / `$skill` 点名 | 工作台勾选；语义为强制并入（见 §1） |
| 预算 | 压缩描述 / 超量丢弃 | 后续再做（§9） |

差异：Codex 有 `allow_implicit_invocation: false`（敏感 skill 禁自动）。本切片不需要，留作未来。

## 4. 数据模型

新表 `skills`（workspace 隔离，与 conversations 一致）：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | str(36) PK | |
| workspace_id | FK workspaces, index | 多租户隔离边界 |
| name | str(100) | skill 名 |
| description | text | **自动选主依据**：写明何时该用 |
| type | str(30) default `instruction` | M3 仅 instruction |
| trigger_phrases | text default "" | 逗号分隔，弱信号增强 |
| content | text | 注入「生成回答」步的方法论 / 输出格式正文 |
| builtin | bool default false | 内置只读，不可改 / 删 |
| status | str(30) default `active` | active / archived |
| created_at / updated_at | datetime | |

不建 `skill_versions` / `agent_skills`。

内置 skill（启动幂等灌入，仿 `seed_test_accounts`），3 个：
- **销售拜访简报**：把零散资料整理成结构化拜访简报（背景 / 要点 / 风险 / 下一步）。
- **会议纪要**：把内容整理成纪要（议题 / 结论 / 待办+负责人+时间）。
- **中英润色**：在保留原意与术语前提下润色中英文表达，输出对照。

## 5. 核心单元：`api/app/skills_engine.py`（TDD 重点）

codegraph 已确认聊天路径**零测试覆盖**，本模块两个纯函数是测试核心：

```python
def resolve_skills(catalog: list[dict], manual_ids: list[str], auto_ids: list[str]) -> list[dict]:
    """手动(强制) ∪ 自动(需在 catalog 内校验) → 去重 → 标 source。
    - 仅保留存在于 catalog 的 id（防脏数据 / 跨租户串读）。
    - 同时命中手动+自动 → source 标 'manual'（手动优先）。
    - 保序：手动在前，自动补后。
    返回 [{id, name, content, source: 'manual'|'auto'}]。"""

def build_skill_instructions(selected: list[dict]) -> str:
    """把选中 skill 的 content 拼成一条「技能指引」系统消息；空列表返回 ''。"""
```

纯函数、不碰 db / 网络 → 可完整单测边界（空、脏 id、重叠、顺序）。

## 6. Strategy Agent 扩展（`strategy.py`）

`plan()` 新增入参 `skill_catalog: list[dict]`（每项 `{id, name, description, trigger_phrases}`，**不含 content**——渐进披露）。

- 规划器 system prompt 增一段：根据用户请求从 catalog 选最合适的 0..N 个 skill，输出其 id 到 plan 的新字段 `skills`（**此阶段为 id 数组**）。
- `_normalize` 校验 `skills` 里的 id 必须在 catalog 内，非法丢弃。
- 注意字段形变：strategy 输出的 `plan["skills"]` 是 id 数组（仅自动选）；§7 中 send_message 用 `resolve_skills` 合并手动后，**用解析后的对象数组 `[{id,name,source}]` 覆写 `plan["skills"]` 再持久化**。下游（plan_created / 计划卡 / 审计）看到的都是对象数组。
- catalog 为空时跳过（plan.skills=[]），退化 = M2 行为。
- 任何异常 → fallback（plan.skills=[]），绝不阻断主流程。

## 7. 执行流（接入 M1/M2 管线，`main.py send_message`）

```
取 workspace skill catalog + payload.skill_ids(手动)
events():
  planning_started
  plan = strategy.plan(..., skill_catalog)        # 含自动选 plan.skills
  selected = resolve_skills(catalog, manual=skill_ids, auto=plan.skills)
  plan["skills"] = selected                        # 写回，持久化进 plan_json
  save_plan; plan_created(plan)
  若 selected 非空: SSE skills_selected {skills:[{id,name,source}]}
  retrieve?(M2 不变)
  注入: history += build_skill_instructions(selected)  # 在 user_turn 前
  respond（带技能指引的 LLM 步，M2 不变）
  done
```

instruction 型不需要独立执行步，**增强 respond 步**即可；但在计划卡 + SSE 显式展示选中 skill。被选 skill 随 `plan_json` 落库满足审计要求。

## 8. API（`main.py`，全部 workspace 作用域）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/skills` | 列本 workspace 全部 skill |
| POST | `/api/skills` | 建自定义 skill |
| GET | `/api/skills/{id}` | 详情 |
| PUT | `/api/skills/{id}` | 改（builtin → 403） |
| DELETE | `/api/skills/{id}` | 删（builtin → 403） |

`ChatRequest` 加 `skill_ids: list[str] = []`。

## 9. 前端

**类型 / API**：`types.ts` 加 `Skill`；`api.ts` 加 skill CRUD、`streamMessage` 带 `skill_ids`、消费 `skills_selected`。

**Skill Hub 管理页**（左侧导航「资源中心」加 `Skills` 入口，对齐腾讯云 ADP 参考图）：
- 列表（内置带「内置」标 + 只读）、新建 / 编辑 / 删除自定义。
- 表单字段：名称、描述（提示"写清何时该用，影响自动选准确率"）、触发词（可选）、正文。

**工作台 Skills 多选下拉**（composer 底部，模型下拉旁，升级现有占位按钮，对齐参考图）：
- 按钮「Skills」+ 已选数量角标。
- 面板：**顶部搜索框**（按 name/description 过滤）→ 可滚动列表（图标 + 名称 + 描述截断 + 勾选框，多选=强制启用）→ 底部 **`⚙ 管理 Skills`** 跳管理页。

**计划卡**：显示本轮应用的 skill（标「自动 / 手动」）。

## 10. 测试策略（TDD，先红后绿）

pytest（`api/tests/`）：
1. `resolve_skills`：空 / 仅手动 / 仅自动 / 重叠（手动优先）/ 脏 id 过滤 / 保序。
2. `build_skill_instructions`：空返回 '' / 多个拼接含各 content。
3. `strategy._normalize`：plan.skills 非法 id 丢弃 / catalog 空。
4. API：CRUD + 租户隔离（A 建的 B 看不到）+ 内置只读 403 + 启动幂等（重启不重复灌）。

真 HTTP 冒烟：建 skill → 工作台勾选 → 发任务 → `skills_selected` 出现、技能指引注入、`plan_json.skills` 含手动项 + 可能的自动项。

前端：`tsc --noEmit` + `vite build`。

## 11. 验收标准

- [ ] 工作台 Skills 下拉可搜索、可多选，勾选后该 skill 必定生效。
- [ ] 不勾任何 skill 时，Strategy Agent 能按描述自动选中合适 skill（如"帮我写会议纪要"→ 自动带会议纪要 skill）。
- [ ] 手动勾 A + router 选 B → 最终 `{A,B}` 都注入并在计划卡可见。
- [ ] Skill Hub 页可增删改查自定义 skill；内置只读。
- [ ] 选中 skill 落进 `plan_json`，可经 run 查询审计。
- [ ] 多租户隔离、内置幂等、所有测试 + 构建通过。
