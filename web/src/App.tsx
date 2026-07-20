import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  configureFeishu,
  configureGithub,
  configureMcp,
  configureModelProvider,
  createConversation,
  createKnowledgeBase,
  createSkill,
  deleteConversation,
  deleteDocument,
  deleteSkill,
  disconnectFeishu,
  disconnectGithub,
  disconnectMcp,
  getConversation,
  getMe,
  getRuntimeRun,
  listAccounts,
  listAgents,
  listConnectors,
  listConversations,
  listKnowledgeBases,
  listModels,
  listRuns,
  listSkills,
  login,
  logout,
  startRuntimeRun,
  streamMessage,
  testModel,
  updateSkill,
  uploadAttachment,
  uploadDocument,
} from "./api";
import type {
  Account,
  Agent,
  Connector,
  Conversation,
  KnowledgeBase,
  Me,
  Message,
  ModelProvider,
  Plan,
  RuntimeRunHandle,
  SelectedSkill,
  Skill,
  Source,
  WorkflowRun,
  WorkflowStep,
} from "./types";
import AgentStudio from "./AgentStudio";
import AppBuilder from "./AppBuilder";
import RuntimeTracePanel from "./RuntimeTracePanel";
import { selectRuntimeTransport, type RuntimeRunProjection } from "./runtime-events";

const groups = [
  {
    label: "使用智能体",
    items: [
      ["⌁", "智能工作台", "workbench"],
      ["▣", "常用应用", "apps"],
    ],
  },
  {
    label: "构建智能体",
    items: [
      ["A", "Agent Studio", "agents"],
      ["◇", "应用开发", "builder"],
      ["▦", "应用模板", "templates"],
    ],
  },
  {
    label: "资源中心",
    items: [
      ["▤", "知识库", "knowledge"],
      ["◉", "模型", "models"],
      ["♧", "Skills", "skills"],
      ["↗", "连接器与工具", "tools"],
      ["▱", "提示词", "prompts"],
    ],
  },
  {
    label: "平台管理",
    items: [
      ["⌁", "数据报表", "analytics"],
      ["⚙", "权限管理", "settings"],
    ],
  },
];
type Attachment = { name: string; text: string };
const runtimeWorkbenchEnabled =
  import.meta.env.VITE_LANGGRAPH_RUNTIME_ENABLED === "true";

export default function App() {
  const [me, setMe] = useState<Me | null>(null),
    [authState, setAuthState] = useState<"loading" | "in" | "out">("loading");
  useEffect(() => {
    getMe()
      .then((m) => {
        setMe(m);
        setAuthState("in");
      })
      .catch(() => setAuthState("out"));
  }, []);
  if (authState === "loading")
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <b>A</b>
          <p>正在加载…</p>
        </div>
      </div>
    );
  if (authState === "out")
    return (
      <LoginScreen
        onLogin={(m) => {
          setMe(m);
          setAuthState("in");
        }}
      />
    );
  return (
    <AppShell
      me={me!}
      onLogout={async () => {
        await logout();
        setMe(null);
        setAuthState("out");
      }}
    />
  );
}

function LoginScreen({ onLogin }: { onLogin: (m: Me) => void }) {
  const [accounts, setAccounts] = useState<Account[]>([]),
    [username, setUsername] = useState(""),
    [password, setPassword] = useState(""),
    [busy, setBusy] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    listAccounts()
      .then(setAccounts)
      .catch(() => {});
  }, []);
  async function doLogin(u: string, p: string, tag: string) {
    if (busy) return;
    setBusy(tag);
    setError("");
    try {
      onLogin(await login(u, p));
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
      setBusy("");
    }
  }
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="brand">
          <b>A</b>
          <div>
            ATLAS<small>智能体平台</small>
          </div>
        </div>
        <h1>登录以继续</h1>
        <p>点选测试账号即可登录，或用账号密码登录。</p>
        <div className="account-list">
          {accounts.map((a) => (
            <button
              key={a.username}
              className="account-btn"
              disabled={!!busy}
              onClick={() => doLogin(a.username, "atlas123", a.username)}
            >
              <b>{a.name.slice(0, 1)}</b>
              <span>
                <strong>{a.name}</strong>
                <small>@{a.username}</small>
              </span>
              {busy === a.username ? <i>登录中…</i> : <i>→</i>}
            </button>
          ))}
        </div>
        <form
          className="login-form"
          onSubmit={(e) => {
            e.preventDefault();
            doLogin(username, password, "form");
          }}
        >
          <input
            placeholder="账号"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
          <input
            placeholder="密码"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button
            className="solid"
            disabled={busy === "form" || !username || !password}
          >
            {busy === "form" ? "登录中…" : "登录"}
          </button>
        </form>
        {error && <p className="login-error">{error}</p>}
        <small className="login-hint">测试账号密码统一为 atlas123</small>
      </div>
    </div>
  );
}

function AppShell({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [view, setView] = useState("workbench"),
    [conversations, setConversations] = useState<Conversation[]>([]),
    [current, setCurrent] = useState<Conversation | null>(null),
    [messages, setMessages] = useState<Message[]>([]),
    [input, setInput] = useState(""),
    [busy, setBusy] = useState(false),
    [knowledge, setKnowledge] = useState<KnowledgeBase[]>([]),
    [selectedKb, setSelectedKb] = useState(""),
    [agents, setAgents] = useState<Agent[]>([]),
    [selectedAgent, setSelectedAgent] = useState(""),
    [selectedModel, setSelectedModel] = useState(""),
    [models, setModels] = useState<string[]>([]),
    [providers, setProviders] = useState<ModelProvider[]>([]),
    [attachment, setAttachment] = useState<Attachment | null>(null),
    [connectors, setConnectors] = useState<Connector[]>([]),
    [selectedConnectors, setSelectedConnectors] = useState<string[]>([]),
    [notice, setNotice] = useState(""),
    [step, setStep] = useState(""),
    [plan, setPlan] = useState<Plan | null>(null),
    [skills, setSkills] = useState<Skill[]>([]),
    [selectedSkills, setSelectedSkills] = useState<string[]>([]),
    [skillFilter, setSkillFilter] = useState(""),
    [appliedSkills, setAppliedSkills] = useState<SelectedSkill[]>([]),
    [toolCalls, setToolCalls] = useState<
      { name: string; access?: string; done: boolean }[]
    >([]),
    [runtimeRun, setRuntimeRun] = useState<RuntimeRunHandle | null>(null),
    [skillDraft, setSkillDraft] = useState({
      name: "",
      description: "",
      trigger_phrases: "",
      content: "",
    });
  const end = useRef<HTMLDivElement>(null);
  const runtimeAssistantId = useRef("");
  const finalizedRuntimeRun = useRef("");
  const refreshConversations = () => listConversations().then(setConversations);
  const refreshKnowledge = () => listKnowledgeBases().then(setKnowledge);
  const refreshAgents = () => listAgents().then(setAgents);
  const refreshModels = () =>
    listModels().then((data) => {
      setProviders(data.providers);
      const flat = data.providers.filter((p) => p.configured).flatMap((p) => p.models);
      setModels(flat);
      setSelectedModel((prev) => flat.includes(prev) ? prev : flat.includes(data.default) ? data.default : flat[0] || "");
    });
  const refreshConnectors = () => listConnectors().then(setConnectors);
  const toggleConnector = (id: string) =>
    setSelectedConnectors((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  const refreshSkills = () => listSkills().then(setSkills);
  const toggleSkill = (id: string) =>
    setSelectedSkills((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  useEffect(() => {
    refreshConversations();
    refreshKnowledge();
    refreshAgents();
    refreshModels();
    refreshConnectors();
    refreshSkills();
  }, []);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages]);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      document
        .querySelectorAll<HTMLDetailsElement>(
          "details.skill-select[open],details.connector-select[open]",
        )
        .forEach((d) => {
          if (!d.contains(e.target as Node)) d.open = false;
        });
    };
    document.addEventListener("click", onDoc);
    return () => document.removeEventListener("click", onDoc);
  }, []);
  async function newTask() {
    const item = await createConversation();
    setCurrent(item);
    setMessages([]);
    setRuntimeRun(null);
    setView("workbench");
    await refreshConversations();
  }
  async function deleteTask(id: string) {
    await deleteConversation(id);
    if (current?.id === id) {
      setCurrent(null);
      setMessages([]);
    }
    await refreshConversations();
  }
  async function openTask(id: string) {
    const item = await getConversation(id);
    setCurrent(item);
    setMessages(item.messages || []);
    setRuntimeRun(null);
    setView("workbench");
  }
  async function invokeAgent(agent: Agent) {
    setSelectedAgent(agent.id);
    setSelectedKb(agent.knowledge_base_id || "");
    await newTask();
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    const content = input.trim();
    if (!content || busy) return;
    let task = current;
    if (!task) {
      task = await createConversation();
      setCurrent(task);
    }
    const sent = attachment;
    setInput("");
    setAttachment(null);
    setBusy(true);
    const user: Message = {
        id: crypto.randomUUID(),
        role: "user",
        content: sent ? `📎 ${sent.name}\n${content}` : content,
        sources: "",
        created_at: new Date().toISOString(),
      },
      assistant: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: "",
        sources: "[]",
        created_at: new Date().toISOString(),
      };
    setMessages((old) => [...old, user, assistant]);
    setStep("");
    setPlan(null);
    setAppliedSkills([]);
    setToolCalls([]);
    setRuntimeRun(null);
    let runtimeDeferred = false;
    try {
      const runtimeEligible = Boolean(
        selectedAgent && !sent && !selectedKb && selectedSkills.length === 0 && selectedConnectors.length === 0,
      );
      if (selectRuntimeTransport(runtimeWorkbenchEnabled, runtimeEligible) === "langgraph") {
        runtimeAssistantId.current = assistant.id;
        const handle = await startRuntimeRun({
          agent_id: selectedAgent,
          input: content,
          idempotency_key: crypto.randomUUID(),
          conversation_id: task.id,
          source: "workbench",
        });
        setRuntimeRun(handle);
        runtimeDeferred = true;
      } else {
        await streamMessage(
        task.id,
        content,
        selectedKb || undefined,
        (token) =>
          setMessages((old) =>
            old.map((m) =>
              m.id === assistant.id ? { ...m, content: m.content + token } : m,
            ),
          ),
        (sources) =>
          setMessages((old) =>
            old.map((m) =>
              m.id === assistant.id
                ? { ...m, sources: JSON.stringify(sources) }
                : m,
            ),
          ),
        selectedAgent || undefined,
        selectedModel,
        sent || undefined,
        selectedConnectors,
        (label) => setStep(label || ""),
        (p) => setPlan(p),
        selectedSkills,
        (s) => setAppliedSkills(s),
        (t) =>
          setToolCalls((prev) =>
            t.phase === "call"
              ? [...prev, { name: t.name, access: t.access, done: false }]
              : prev.map((x, i) =>
                  i === prev.map((y) => y.name).lastIndexOf(t.name) && !x.done
                    ? { ...x, done: true }
                    : x,
                ),
          ),
        );
      }
      await refreshConversations();
    } catch (error) {
      setMessages((old) =>
        old.map((m) =>
          m.id === assistant.id
            ? {
                ...m,
                content: `任务失败：${error instanceof Error ? error.message : "未知错误"}`,
              }
            : m,
        ),
      );
    } finally {
      if (!runtimeDeferred) setBusy(false);
      setStep("");
      setPlan(null);
      setAppliedSkills([]);
      setToolCalls([]);
    }
  }
  const projectRuntime = useCallback((projection: RuntimeRunProjection) => {
    if (projection.tokens) {
      setMessages((old) => old.map((message) =>
        message.id === runtimeAssistantId.current && message.content !== projection.tokens
          ? { ...message, content: projection.tokens }
          : message,
      ));
    }
    if (!["succeeded", "failed", "cancelled"].includes(projection.status)) return;
    setBusy(false);
    if (projection.tokens || finalizedRuntimeRun.current === projection.runId) return;
    finalizedRuntimeRun.current = projection.runId;
    void getRuntimeRun(projection.runId).then((state) => {
      setMessages((old) => old.map((message) =>
        message.id === runtimeAssistantId.current
          ? { ...message, content: state.output || (state.status === "failed" ? "运行失败，请查看执行轨迹。" : "运行没有返回内容。") }
          : message,
      ));
    });
  }, []);
  async function attachFile(file?: File) {
    if (!file) return;
    try {
      const att = await uploadAttachment(file);
      setAttachment(att);
      showNotice(`已附加文件：${att.name}`);
    } catch (error) {
      showNotice(error instanceof Error ? error.message : "文件解析失败");
    }
  }
  const showNotice = (text: string) => {
    setNotice(text);
    setTimeout(() => setNotice(""), 2400);
  };
  const title =
    view === "knowledge"
      ? "知识库"
      : view === "agents"
        ? "Agent Studio"
        : view === "builder"
          ? "智能体开发"
          : view === "models"
            ? "模型管理"
            : view === "tools"
              ? "连接器与工具"
              : view === "skills"
                ? "Skills 技能"
                : "智能工作台";
  return (
    <div className="shell">
      <aside className="global-nav">
        <div className="brand">
          <b>A</b>
          <div>
            ATLAS<small>智能体平台</small>
          </div>
        </div>
        <nav>
          {groups.map((group) => (
            <div key={group.label}>
              <p>{group.label}</p>
              {group.items.map(([icon, name, key]) => (
                <button
                  key={key}
                  className={view === key ? "active" : ""}
                  onClick={() =>
                    [
                      "workbench",
                      "knowledge",
                      "agents",
                      "builder",
                      "models",
                      "tools",
                      "skills",
                    ].includes(key)
                      ? setView(key)
                      : showNotice(`${name}将在后续里程碑开放`)
                  }
                >
                  <span>{icon}</span>
                  {name}
                  {key === "workbench" && <i>NEW</i>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <footer>
          <strong>
            <i />
            本地服务运行中
          </strong>
          <small>React + FastAPI</small>
        </footer>
      </aside>
      <main>
        <Header title={title} me={me} onLogout={onLogout} />
        {view === "knowledge" ? (
          <KnowledgeView
            items={knowledge}
            refresh={refreshKnowledge}
            notice={showNotice}
          />
        ) : view === "agents" ? (
          <AgentStudio
            agents={agents}
            knowledge={knowledge}
            providers={providers}
            refresh={refreshAgents}
            notice={showNotice}
            onConfigureModels={() => setView("models")}
          />
        ) : view === "builder" ? (
          <AppBuilder
            agents={agents}
            knowledge={knowledge}
            refresh={refreshAgents}
            notice={showNotice}
            onInvokeAgent={invokeAgent}
          />
        ) : view === "models" ? (
          <ModelsView
            providers={providers}
            defaultModel={models[0] || ""}
            notice={showNotice}
            refresh={refreshModels}
          />
        ) : view === "tools" ? (
          <ConnectorsView
            connectors={connectors}
            selectedConnectors={selectedConnectors}
            toggleConnector={toggleConnector}
            refresh={refreshConnectors}
            notice={showNotice}
          />
        ) : view === "skills" ? (
          <SkillsView
            refresh={refreshSkills}
            notice={showNotice}
            draft={skillDraft}
            setDraft={setSkillDraft}
          />
        ) : (
          <Workbench
            conversations={conversations}
            current={current}
            messages={messages}
            input={input}
            busy={busy}
            step={step}
            plan={plan}
            toolCalls={toolCalls}
            knowledge={knowledge}
            selectedKb={selectedKb}
            setSelectedKb={setSelectedKb}
            agents={agents}
            selectedAgent={selectedAgent}
            setSelectedAgent={(id) => {
              setSelectedAgent(id);
              const agent = agents.find((item) => item.id === id);
              setSelectedKb(agent?.knowledge_base_id || "");
            }}
            selectedModel={selectedModel}
            setSelectedModel={setSelectedModel}
            models={models}
            connectors={connectors}
            selectedConnectors={selectedConnectors}
            toggleConnector={toggleConnector}
            skills={skills}
            selectedSkills={selectedSkills}
            toggleSkill={toggleSkill}
            skillFilter={skillFilter}
            setSkillFilter={setSkillFilter}
            appliedSkills={appliedSkills}
            goSkills={() => setView("skills")}
            attachment={attachment}
            attachFile={attachFile}
            clearAttachment={() => setAttachment(null)}
            goBuilder={() => setView("builder")}
            notice={showNotice}
            setInput={setInput}
            newTask={newTask}
            openTask={openTask}
            deleteTask={deleteTask}
            submit={submit}
            runtimeRun={runtimeRun}
            onRuntimeProjection={projectRuntime}
            end={end}
          />
        )}
      </main>
      {notice && <div className="app-toast">{notice}</div>}
    </div>
  );
}

function Header({
  title,
  me,
  onLogout,
}: {
  title: string;
  me: Me;
  onLogout: () => void;
}) {
  return (
    <header>
      <div>
        <small>{me.workspace.name}</small>
        <strong>{title}</strong>
      </div>
      <div className="top-actions">
        <button>⌕ 搜索功能或智能体　⌘ K</button>
        <span>♢</span>
        <div className="user-menu">
          <b title={me.user.name}>{me.user.name.slice(0, 1) || "U"}</b>
          <div className="user-pop">
            <strong>{me.user.name}</strong>
            <small>
              {me.role === "owner" ? "所有者" : "成员"}
              {me.user.email ? ` · ${me.user.email}` : ""}
            </small>
            <button onClick={onLogout}>退出登录</button>
          </div>
        </div>
      </div>
    </header>
  );
}

function RunDrawer({
  conversationId,
  title,
  onClose,
}: {
  conversationId: string;
  title: string;
  onClose: () => void;
}) {
  const [runs, setRuns] = useState<WorkflowRun[]>([]),
    [sel, setSel] = useState(""),
    [expanded, setExpanded] = useState(""),
    [err, setErr] = useState(""),
    [loading, setLoading] = useState(true);
  useEffect(() => {
    listRuns(conversationId)
      .then((rs) => {
        const sorted = [...rs].sort((a, b) =>
          b.created_at.localeCompare(a.created_at),
        );
        setRuns(sorted);
        setSel(sorted[0]?.id || "");
      })
      .catch((e) => setErr(e instanceof Error ? e.message : "加载失败"))
      .finally(() => setLoading(false));
  }, [conversationId]);
  const parse = (s: string): any => {
    try {
      return s ? JSON.parse(s) : null;
    } catch {
      return s || null;
    }
  };
  const dur = (a: string | null, b: string | null) =>
    a && b ? `${((+new Date(b) - +new Date(a)) / 1000).toFixed(1)}s` : "—";
  const stType = (t: string) =>
    t === "retrieve"
      ? "检索"
      : t === "tool"
        ? "工具"
        : t === "skill"
          ? "技能"
          : "回答";
  const stCls = (s: string) =>
    s === "succeeded"
      ? "ok"
      : s === "failed"
        ? "bad"
        : s === "waiting_confirmation"
          ? "wait"
          : s === "cancelled"
            ? "cancel"
            : "run";
  const block = (label: string, v: any) =>
    v == null ? null : (
      <div className="rd-block">
        <label>{label}</label>
        <pre>{typeof v === "string" ? v : JSON.stringify(v, null, 2)}</pre>
      </div>
    );
  const run = runs.find((r) => r.id === sel) || null;
  const plan = run ? parse(run.plan_json) : null;
  const policy = plan && typeof plan === "object" ? plan.tool_policy : null;
  const skills =
    plan && typeof plan === "object" && Array.isArray(plan.skills)
      ? plan.skills
      : [];
  return (
    <div className="run-drawer-mask" onClick={onClose}>
      <aside className="run-drawer" onClick={(e) => e.stopPropagation()}>
        <header>
          <strong>运行详情 · {title}</strong>
          <button className="rd-close" onClick={onClose}>
            ×
          </button>
        </header>
        {loading ? (
          <p className="rd-empty">加载中…</p>
        ) : err ? (
          <p className="rd-empty">加载失败：{err}</p>
        ) : runs.length === 0 ? (
          <p className="rd-empty">该任务暂无运行记录</p>
        ) : (
          <div className="rd-body">
            <div className="rd-runs">
              {runs.map((r) => (
                <button
                  key={r.id}
                  className={`rd-run${r.id === sel ? " on" : ""}`}
                  onClick={() => {
                    setSel(r.id);
                    setExpanded("");
                  }}
                >
                  <i className={`rd-dot rd-${stCls(r.status)}`} />
                  <span>
                    <b>{r.input_text || "(空)"}</b>
                    <small>
                      {new Date(r.created_at).toLocaleString()} ·{" "}
                      {r.steps.length}步
                    </small>
                  </span>
                </button>
              ))}
            </div>
            {run && (
              <div className="rd-detail">
                <div className="rd-head">
                  <h4>{(plan && plan.goal) || run.input_text}</h4>
                  <span className={`rd-badge rd-${stCls(run.status)}`}>
                    {run.status}
                  </span>
                </div>
                {policy && (
                  <p className="rd-policy">
                    🔧 {(policy.tools || []).length} 工具 ·{" "}
                    {(policy.write_tools || []).length} 写
                  </p>
                )}
                {skills.length > 0 && (
                  <div className="rd-skills">
                    {skills.map((s: any) => (
                      <span
                        key={s.id}
                        className={`skill-chip skill-${s.source}`}
                      >
                        ♧ {s.name}
                        <i>{s.source === "manual" ? "手动" : "自动"}</i>
                      </span>
                    ))}
                  </div>
                )}
                <ol className="rd-steps">
                  {run.steps.map((st: WorkflowStep) => {
                    const open = expanded === st.id;
                    const inp = parse(st.input_json),
                      out = parse(st.output_json);
                    return (
                      <li key={st.id} className={`rd-step plan-${st.type}`}>
                        <button
                          className="rd-step-row"
                          onClick={() => setExpanded(open ? "" : st.id)}
                        >
                          <span className="plan-step-type">
                            {stType(st.type)}
                          </span>
                          <b>{st.title}</b>
                          <em className={`rd-badge rd-${stCls(st.status)}`}>
                            {st.status}
                          </em>
                          <small>{dur(st.started_at, st.ended_at)}</small>
                        </button>
                        {open && (
                          <div className="rd-step-detail">
                            {block("入参", inp)}
                            {block("结果", out)}
                            {st.error && block("错误", st.error)}
                            {inp == null && out == null && !st.error && (
                              <small className="rd-empty">无记录</small>
                            )}
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </div>
            )}
          </div>
        )}
      </aside>
    </div>
  );
}
function Workbench({
  conversations,
  current,
  messages,
  input,
  busy,
  step,
  plan,
  toolCalls,
  knowledge,
  selectedKb,
  setSelectedKb,
  agents,
  selectedAgent,
  setSelectedAgent,
  selectedModel,
  setSelectedModel,
  models,
  connectors,
  selectedConnectors,
  toggleConnector,
  skills,
  selectedSkills,
  toggleSkill,
  skillFilter,
  setSkillFilter,
  appliedSkills,
  goSkills,
  attachment,
  attachFile,
  clearAttachment,
  goBuilder,
  notice,
  setInput,
  newTask,
  openTask,
  deleteTask,
  submit,
  runtimeRun,
  onRuntimeProjection,
  end,
}: {
  conversations: Conversation[];
  current: Conversation | null;
  messages: Message[];
  input: string;
  busy: boolean;
  step: string;
  plan: Plan | null;
  toolCalls: { name: string; access?: string; done: boolean }[];
  knowledge: KnowledgeBase[];
  selectedKb: string;
  setSelectedKb: (v: string) => void;
  agents: Agent[];
  selectedAgent: string;
  setSelectedAgent: (v: string) => void;
  selectedModel: string;
  setSelectedModel: (v: string) => void;
  models: string[];
  connectors: Connector[];
  selectedConnectors: string[];
  toggleConnector: (id: string) => void;
  skills: Skill[];
  selectedSkills: string[];
  toggleSkill: (id: string) => void;
  skillFilter: string;
  setSkillFilter: (v: string) => void;
  appliedSkills: SelectedSkill[];
  goSkills: () => void;
  attachment: Attachment | null;
  attachFile: (f?: File) => void;
  clearAttachment: () => void;
  goBuilder: () => void;
  notice: (v: string) => void;
  setInput: (v: string) => void;
  newTask: () => void;
  openTask: (id: string) => void;
  deleteTask: (id: string) => void;
  submit: (e: FormEvent) => void;
  runtimeRun: RuntimeRunHandle | null;
  onRuntimeProjection: (projection: RuntimeRunProjection) => void;
  end: React.RefObject<HTMLDivElement | null>;
}) {
  const quick = (text: string) => {
    setInput(text);
    document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus();
  };
  const activeAgent = agents.find((item) => item.id === selectedAgent);
  const [drawerOpen, setDrawerOpen] = useState(false);
  return (
    <div className="workspace">
      <aside className="task-list">
        <label>
          <input placeholder="搜索任务" />
          <span>⌕</span>
        </label>
        <button className="new" onClick={newTask}>
          □＋　新建任务
        </button>
        <button>◷　定时任务</button>
        <h4>
          远程终端 <span>⚙</span>
        </h4>
        <h4>最近任务</h4>
        {conversations.map((item) => (
          <div
            className={`task-item${current?.id === item.id ? " selected" : ""}`}
            key={item.id}
          >
            <button className="task-item-btn" onClick={() => openTask(item.id)}>
              <em>{item.title}</em>
              <small>{new Date(item.updated_at).toLocaleDateString()}</small>
            </button>
            <button
              className="task-delete"
              title="删除任务"
              onClick={(e) => {
                e.stopPropagation();
                deleteTask(item.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </aside>
      <section className="stage">
        <div className="stage-head">
          <div>
            <button>◫</button>
            <button onClick={newTask}>□＋</button>
            <strong>{current?.title || "新任务"}</strong>
            {activeAgent && (
              <span className="active-agent-pill">
                <b>{activeAgent.name.slice(0, 1)}</b>
                {activeAgent.name}
              </span>
            )}
          </div>
          {current && (
            <button className="stage-runs" onClick={() => setDrawerOpen(true)}>
              ☰ 运行详情
            </button>
          )}
        </div>
        <div className="conversation">
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="agent-mark">
                {activeAgent?.name.slice(0, 1) || "A"}
              </div>
              <h1>
                {activeAgent
                  ? `和${activeAgent.name}开始对话`
                  : "描述需求，开启智能工作方式"}
              </h1>
              <p>
                {activeAgent?.description ||
                  "选择智能体和知识库，开始一段可持续的任务对话。"}
              </p>
            </div>
          ) : (
            messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                assistantName={activeAgent?.name}
              />
            ))
          )}
          {runtimeRun && <RuntimeTracePanel run={runtimeRun} onProjection={onRuntimeProjection} />}
          {busy && plan && (
            <div className="run-plan">
              <div className="run-plan-head">
                <b>◇ 执行计划</b>
                <small>{plan.source === "llm" ? "智能规划" : "默认流程"}</small>
              </div>
              <ol className="run-plan-steps">
                {plan.steps.map((s, i) => (
                  <li key={s.id || i} className={`plan-step plan-${s.type}`}>
                    <span className="plan-step-type">
                      {s.type === "retrieve"
                        ? "检索"
                        : s.type === "tool"
                          ? "工具"
                          : s.type === "skill"
                            ? "技能"
                            : "回答"}
                    </span>
                    {s.title}
                    {s.risk === "write" && <i className="plan-risk">写</i>}
                  </li>
                ))}
              </ol>
              {appliedSkills.length > 0 && (
                <div className="plan-skills">
                  {appliedSkills.map((s) => (
                    <span key={s.id} className={`skill-chip skill-${s.source}`}>
                      ♧ {s.name}
                      <i>{s.source === "manual" ? "手动" : "自动"}</i>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
          {busy && step && (
            <div className="run-step">
              <i className="run-step-dot" />
              <span>{step}…</span>
            </div>
          )}
          {busy && toolCalls.length > 0 && (
            <div className="run-tools">
              {toolCalls.map((t, i) => (
                <span
                  key={i}
                  className={`tool-chip tool-${t.access === "write" ? "write" : "read"}`}
                >
                  🔧 {t.name}
                  <i>{t.access === "write" ? "写" : "读"}</i>
                  {t.done ? (
                    <b className="tool-ok">✓</b>
                  ) : (
                    <b className="tool-run">…</b>
                  )}
                </span>
              ))}
            </div>
          )}
          <div ref={end} />
        </div>
        <div className="dock">
          <div className="quick">
            <button onClick={() => quick("帮我总结知识库中的主要内容")}>
              ▣ 文档处理
            </button>
            <button onClick={() => quick("分析资料并给出关键结论")}>
              ⌁ 数据分析
            </button>
            <button onClick={() => quick("围绕资料进行深度研究")}>
              ⌕ 深度研究
            </button>
            <button onClick={goBuilder}>◇ 创建应用</button>
          </div>
          <form className="composer" onSubmit={submit}>
            {attachment && (
              <div className="attachment-chip">
                <b>📎</b>
                <span>{attachment.name}</span>
                <button type="button" onClick={clearAttachment}>
                  ×
                </button>
              </div>
            )}
            <textarea
              rows={2}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder={
                activeAgent
                  ? `给 ${activeAgent.name} 发送消息`
                  : "支持上传文件进行提问，选择智能体与知识库开启对话"
              }
            />
            <div className="composer-bar">
              <div className="composer-tools">
                <label className="tool-btn upload" title="上传文件">
                  <span>＋</span>
                  <input
                    type="file"
                    accept=".pdf,.docx,.txt,.md,.csv,.json"
                    onChange={(e) => {
                      attachFile(e.target.files?.[0] || undefined);
                      e.target.value = "";
                    }}
                  />
                </label>
                <select
                  className="model-select"
                  value={selectedModel}
                  onChange={(event) => setSelectedModel(event.target.value)}
                  title="选择模型"
                  disabled={models.length === 0}
                >
                  {models.length === 0 && <option value="">请先配置模型</option>}
                  {models.map((m) => (
                    <option value={m} key={m}>
                      ◈ {m}
                    </option>
                  ))}
                </select>
                <details className="skill-select">
                  <summary className="tool-btn">
                    ♧ Skills
                    {selectedSkills.length > 0 && (
                      <i className="conn-count">{selectedSkills.length}</i>
                    )}
                  </summary>
                  <div className="skill-menu">
                    <input
                      className="skill-search"
                      placeholder="搜索 Skills"
                      value={skillFilter}
                      onChange={(e) => setSkillFilter(e.target.value)}
                    />
                    <div className="skill-list">
                      {(() => {
                        const q = skillFilter.trim().toLowerCase();
                        const list = skills.filter(
                          (s) =>
                            !q ||
                            s.name.toLowerCase().includes(q) ||
                            s.description.toLowerCase().includes(q),
                        );
                        return list.length === 0 ? (
                          <p className="conn-empty">
                            {skills.length === 0 ? "暂无 Skills" : "无匹配结果"}
                          </p>
                        ) : (
                          list.map((s) => {
                            const on = selectedSkills.includes(s.id);
                            return (
                              <label
                                key={s.id}
                                className={`skill-opt${on ? " on" : ""}`}
                              >
                                <input
                                  type="checkbox"
                                  checked={on}
                                  onChange={() => toggleSkill(s.id)}
                                />
                                <span>
                                  <b>
                                    {s.name}
                                    {s.builtin && (
                                      <em className="skill-builtin">内置</em>
                                    )}
                                  </b>
                                  <small>{s.description}</small>
                                </span>
                              </label>
                            );
                          })
                        );
                      })()}
                    </div>
                    <button
                      type="button"
                      className="skill-manage"
                      onClick={goSkills}
                    >
                      ⚙ 管理 Skills
                    </button>
                  </div>
                </details>
                <details className="connector-select">
                  <summary className="tool-btn">
                    ↗ 连接器
                    {selectedConnectors.length > 0 && (
                      <i className="conn-count">{selectedConnectors.length}</i>
                    )}
                  </summary>
                  <div className="connector-menu">
                    {connectors.length === 0 ? (
                      <p className="conn-empty">暂无连接器</p>
                    ) : (
                      connectors.map((c) => {
                        const on = selectedConnectors.includes(c.provider);
                        return (
                          <label
                            key={c.provider}
                            className={`conn-opt${c.connected ? "" : " disabled"}`}
                            title={
                              c.connected
                                ? ""
                                : "未连接，请先到「连接器与工具」连接"
                            }
                          >
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={!c.connected}
                              onChange={() => toggleConnector(c.provider)}
                            />
                            <span>
                              <b>{c.name}</b>
                              <small>
                                {c.connected
                                  ? c.account_name
                                    ? `已连接 · ${c.account_name}`
                                    : "已连接"
                                  : "未连接"}
                              </small>
                            </span>
                          </label>
                        );
                      })
                    )}
                  </div>
                </details>
                <select
                  className="agent-select"
                  value={selectedAgent}
                  onChange={(event) => setSelectedAgent(event.target.value)}
                >
                  <option value="">默认助手</option>
                  {agents
                    .filter((item) => item.status !== "archived")
                    .map((item) => (
                      <option value={item.id} key={item.id}>
                        ◆ {item.name}
                      </option>
                    ))}
                </select>
                <select
                  className="kb-select"
                  value={selectedKb}
                  onChange={(event) => setSelectedKb(event.target.value)}
                >
                  <option value="">不使用知识库</option>
                  {knowledge.map((item) => (
                    <option value={item.id} key={item.id}>
                      ▤ {item.name}
                    </option>
                  ))}
                </select>
              </div>
              <button className="send-btn" disabled={busy || !input.trim()}>
                {busy ? "···" : "↑"}
              </button>
            </div>
          </form>
          <small>Enter 发送，Shift + Enter 换行 · 对话自动保存到最近任务</small>
        </div>
        {drawerOpen && current && (
          <RunDrawer
            conversationId={current.id}
            title={current.title}
            onClose={() => setDrawerOpen(false)}
          />
        )}
      </section>
    </div>
  );
}

function ModelsView({
  providers,
  defaultModel,
  notice,
  refresh,
}: {
  providers: ModelProvider[];
  defaultModel: string;
  notice: (v: string) => void;
  refresh: () => Promise<void> | void;
}) {
  const [testing, setTesting] = useState("");
  const [configuring, setConfiguring] = useState<ModelProvider | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [results, setResults] = useState<
    Record<string, { ok: boolean; latency_ms?: number; message?: string }>
  >({});
  async function runTest(model: string) {
    setTesting(model);
    try {
      const r = await testModel(model);
      setResults((old) => ({ ...old, [model]: r }));
      notice(
        r.ok
          ? `${model} 连接正常 · ${r.latency_ms}ms`
          : `${model} 失败：${r.message || "未知错误"}`,
      );
    } catch {
      notice("测试请求失败");
    } finally {
      setTesting("");
    }
  }
  async function saveProvider() {
    if (!configuring || !apiKey.trim()) return notice("请输入 API Key");
    setSaving(true);
    try {
      await configureModelProvider(configuring.id, apiKey.trim(), baseUrl.trim());
      await refresh();
      setConfiguring(null);
      setApiKey("");
      notice(`${configuring.name} 已配置，可以开始测试和运行 Agent`);
    } catch (error) {
      notice(error instanceof Error ? error.message : "模型配置失败");
    } finally {
      setSaving(false);
    }
  }
  const total = providers.reduce((sum, p) => sum + p.models.length, 0);
  return (
    <section className="models-page">
      <div className="models-title">
        <div>
          <span className="section-code">RESOURCE / MODELS</span>
          <h1>模型管理</h1>
          <p>
            查看已接入的模型供应商，测试连通性。配置在 <code>api/.env</code>
            ，按模型名自动路由。
          </p>
        </div>
        <div className="models-stat">
          <span>
            <b>{providers.filter((p) => p.configured).length}</b> 供应商
          </span>
          <span>
            <b>{total}</b> 模型
          </span>
        </div>
      </div>
      <div className="provider-grid">
        {providers.map((p) => (
          <div className="provider-card" key={p.id}>
            <div className="provider-head">
              <div className="provider-mark">{p.name.slice(0, 1)}</div>
              <div>
                <strong>
                  {p.name}
                  <small className="provider-note">{p.note}</small>
                </strong>
                <small>{p.base_url}</small>
              </div>
              <span
                className={`provider-status ${p.configured ? "on" : "off"}`}
              >
                {p.configured ? "● 已配置" : "○ 未配置 Key"}
              </span>
              <button className="provider-config-button" onClick={() => { setConfiguring(p); setBaseUrl(p.base_url); setApiKey(""); }}>
                {p.configured ? "更新配置" : "配置"}
              </button>
            </div>
            <div className="model-rows">
              {p.models.map((m) => {
                const r = results[m];
                return (
                  <div className="model-row" key={m}>
                    <span className="model-name">
                      ◈ {m}
                      {m === defaultModel && (
                        <i className="default-tag">默认</i>
                      )}
                    </span>
                    <span className="model-test">
                      {r &&
                        (r.ok ? (
                          <b className="ok">✓ {r.latency_ms}ms</b>
                        ) : (
                          <b className="bad">✗ 失败</b>
                        ))}
                      <button
                        disabled={!p.configured || testing === m}
                        onClick={() => runTest(m)}
                      >
                        {testing === m ? "测试中…" : "测试"}
                      </button>
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
        {providers.length === 0 && (
          <p className="models-empty">正在加载模型供应商…</p>
        )}
      </div>
      {configuring && <div className="model-config-mask" role="dialog" aria-modal="true" aria-label={`配置 ${configuring.name}`}>
        <div className="model-config-dialog">
          <header><div><small>MODEL PROVIDER</small><strong>配置 {configuring.name}</strong></div><button onClick={() => setConfiguring(null)}>×</button></header>
          <p>密钥只提交给 Atlas API，并写入本地 <code>api/.env</code>；页面不会回显已保存的 Key。</p>
          <label>API Key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="输入供应商 API Key" /></label>
          <label>Base URL<input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
          <footer><button onClick={() => setConfiguring(null)}>取消</button><button className="solid" disabled={saving || !apiKey.trim()} onClick={saveProvider}>{saving ? "保存中…" : "保存并启用"}</button></footer>
        </div>
      </div>}
    </section>
  );
}

function ConnectorsView({
  connectors,
  selectedConnectors,
  toggleConnector,
  refresh,
  notice,
}: {
  connectors: Connector[];
  selectedConnectors: string[];
  toggleConnector: (id: string) => void;
  refresh: () => Promise<void> | void;
  notice: (v: string) => void;
}) {
  const [ghOpen, setGhOpen] = useState(false),
    [pat, setPat] = useState(""),
    [fsOpen, setFsOpen] = useState(false),
    [appId, setAppId] = useState(""),
    [appSecret, setAppSecret] = useState(""),
    [saving, setSaving] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(false),
    [mcpKey, setMcpKey] = useState(""),
    [mcpProvider, setMcpProvider] = useState(""),
    [mcpName, setMcpName] = useState(""),
    [mcpLabel, setMcpLabel] = useState("");
  const byProvider = (p: string) => connectors.find((c) => c.provider === p);
  async function saveGithub() {
    const v = pat.trim();
    if (!v) {
      notice("请粘贴 GitHub PAT");
      return;
    }
    setSaving(true);
    try {
      const st = await configureGithub(v);
      notice(
        st.connected
          ? `GitHub 已连接 · ${st.account_name}`
          : `配置失败：${st.account_name || "PAT 无效"}`,
      );
      setGhOpen(false);
      await refresh();
    } catch {
      notice("配置失败，请检查 PAT");
    } finally {
      setSaving(false);
    }
  }
  async function saveFeishu() {
    const a = appId.trim(),
      s = appSecret.trim();
    if (!a || !s) {
      notice("请填写 App ID 和 App Secret");
      return;
    }
    setSaving(true);
    try {
      const st = await configureFeishu(a, s);
      notice(
        st.connected
          ? "飞书应用已就绪"
          : `配置失败：${st.account_name || "凭证无效"}`,
      );
      setFsOpen(false);
      await refresh();
    } catch {
      notice("配置失败，请检查应用凭证");
    } finally {
      setSaving(false);
    }
  }
  async function saveMcp() {
    const v = mcpKey.trim();
    if (!v) {
      notice("请填写 Key");
      return;
    }
    setSaving(true);
    try {
      const st = await configureMcp(mcpProvider, v);
      notice(
        st.connected
          ? `${st.name} 已连接 · ${st.account_name}`
          : `配置失败：${st.account_name || "Key 无效"}`,
      );
      setMcpOpen(false);
      await refresh();
    } catch {
      notice("配置失败，请检查 Key");
    } finally {
      setSaving(false);
    }
  }
  async function disconnect(provider: string) {
    const c = byProvider(provider);
    if (c?.config_kind === "mcp_key") await disconnectMcp(provider);
    else if (provider === "github") await disconnectGithub();
    else if (provider === "feishu") await disconnectFeishu();
    notice(
      c?.config_kind === "local_mcp"
        ? `${c.name} 是本地服务，请在本机停止对应 MCP 进程`
        : `已断开 ${c?.name || provider}`,
    );
    await refresh();
  }
  const openConfig = (provider: string) => {
    const c = byProvider(provider);
    if (c?.config_kind === "local_mcp") {
      notice(c.account_name || `${c.name} 是本地 MCP 服务，启动后刷新状态即可`);
      return;
    }
    if (c?.config_kind === "mcp_key") {
      setMcpProvider(provider);
      setMcpName(c.name);
      setMcpLabel(c.key_label || "API Key");
      setMcpKey("");
      setMcpOpen(true);
    } else if (provider === "feishu") {
      setAppId("");
      setAppSecret("");
      setFsOpen(true);
    } else {
      setPat("");
      setGhOpen(true);
    }
  };
  return (
    <section className="connectors-page">
      <div className="connectors-title">
        <div>
          <span className="section-code">RESOURCE / CONNECTORS</span>
          <h1>连接器与工具</h1>
          <p>
            把外部应用接入平台。飞书为公司统一接入（管理员配置应用凭证），GitHub
            为个人接入（各自填 PAT）。可在工作台底部勾选启用。
          </p>
        </div>
        <button className="solid" onClick={() => refresh()}>
          ↻ 刷新状态
        </button>
      </div>
      <div className="connector-grid">
        {connectors.map((c) => {
          const on = selectedConnectors.includes(c.provider),
            local = c.config_kind === "local_mcp";
          return (
            <div
              className={`connector-card${c.connected ? " ready" : ""}`}
              key={c.provider}
            >
              <div className="connector-card-head">
                <div className="connector-mark">{c.name.slice(0, 1)}</div>
                <div>
                  <strong>
                    {c.name}
                    <span className="conn-kind">
                      {local
                        ? "本地 MCP"
                        : c.config_kind === "mcp_key"
                          ? "MCP 工具"
                          : c.provider === "feishu"
                            ? "公司统一"
                            : "个人接入"}
                    </span>
                  </strong>
                  <small>{c.description}</small>
                </div>
                <span
                  className={`provider-status ${c.connected ? "on" : "off"}`}
                >
                  {c.connected
                    ? "● 已连接"
                    : local
                      ? "○ 未启动"
                      : c.configured
                        ? "○ 凭证无效"
                        : "○ 未配置"}
                </span>
              </div>
              <div className="connector-actions-row">
                {c.actions.map((a) => (
                  <span className="action-tag" key={a}>
                    ⚒ {a}
                  </span>
                ))}
              </div>
              <div className="connector-card-foot">
                {c.connected ? (
                  <>
                    <span className="conn-account">
                      {c.account_name || "已就绪"}
                    </span>
                    <div>
                      {!local && (
                        <button onClick={() => openConfig(c.provider)}>
                          重新配置
                        </button>
                      )}
                      {c.provider === "feishu" && (
                        <button
                          onClick={() =>
                            window.open(
                              "/api/connectors/feishu/login",
                              "_blank",
                            )
                          }
                          title="可选：授权一个默认收件人，用于「发给我」"
                        >
                          授权身份
                        </button>
                      )}
                      <button onClick={() => disconnect(c.provider)}>
                        {local ? "说明" : "断开"}
                      </button>
                      <button
                        className={on ? "solid" : ""}
                        onClick={() => toggleConnector(c.provider)}
                      >
                        {on ? "✓ 已启用" : "在对话启用"}
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <span className="conn-account">
                      {local
                        ? c.account_name || "启动本地 MCP 服务后刷新状态"
                        : c.configured
                          ? "凭证无效，请重新配置"
                          : c.config_kind === "mcp_key"
                            ? "填入 Key 即可连接"
                            : c.provider === "feishu"
                              ? "配置应用凭证即可连接"
                              : "粘贴 PAT 即可连接"}
                    </span>
                    <button
                      className="solid"
                      onClick={() =>
                        local ? refresh() : openConfig(c.provider)
                      }
                    >
                      {local ? "刷新状态" : `配置${c.name}`}
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })}
        {connectors.length === 0 && (
          <p className="models-empty">正在加载连接器…</p>
        )}
      </div>
      {ghOpen && (
        <div className="dialog-backdrop" onMouseDown={() => setGhOpen(false)}>
          <div className="dialog" onMouseDown={(e) => e.stopPropagation()}>
            <h2>配置 GitHub（个人）</h2>
            <label>
              Personal Access Token
              <input
                value={pat}
                onChange={(e) => setPat(e.target.value)}
                autoFocus
                placeholder="ghp_... 或 fine-grained token"
                type="password"
              />
            </label>
            <p className="dialog-hint">
              GitHub → Settings → Developer settings → Personal access tokens
              生成；建议给最小够用的 scope。仅存本地后端，不上传。
            </p>
            <div>
              <button type="button" onClick={() => setGhOpen(false)}>
                取消
              </button>
              <button className="solid" disabled={saving} onClick={saveGithub}>
                {saving ? "验证中…" : "保存并连接"}
              </button>
            </div>
          </div>
        </div>
      )}
      {fsOpen && (
        <div className="dialog-backdrop" onMouseDown={() => setFsOpen(false)}>
          <div className="dialog" onMouseDown={(e) => e.stopPropagation()}>
            <h2>配置飞书（公司统一）</h2>
            <label>
              App ID
              <input
                value={appId}
                onChange={(e) => setAppId(e.target.value)}
                autoFocus
                placeholder="cli_xxxxxxxx"
              />
            </label>
            <label>
              App Secret
              <input
                value={appSecret}
                onChange={(e) => setAppSecret(e.target.value)}
                placeholder="应用凭证密钥"
                type="password"
              />
            </label>
            <p className="dialog-hint">
              在飞书开放平台创建企业自建应用，开通发消息权限+机器人能力、可用范围设全公司；把
              App ID / Secret 填这里。配置后全公司成员都可被发消息。重定向 URL
              填{" "}
              <code>http://localhost:3001/api/connectors/feishu/callback</code>
              。
            </p>
            <div>
              <button type="button" onClick={() => setFsOpen(false)}>
                取消
              </button>
              <button className="solid" disabled={saving} onClick={saveFeishu}>
                {saving ? "验证中…" : "保存并连接"}
              </button>
            </div>
          </div>
        </div>
      )}
      {mcpOpen && (
        <div className="dialog-backdrop" onMouseDown={() => setMcpOpen(false)}>
          <div className="dialog" onMouseDown={(e) => e.stopPropagation()}>
            <h2>配置 {mcpName}（MCP）</h2>
            <label>
              {mcpLabel}
              <input
                value={mcpKey}
                onChange={(e) => setMcpKey(e.target.value)}
                autoFocus
                placeholder="粘贴 Key"
                type="password"
              />
            </label>
            <p className="dialog-hint">
              平台作为 MCP 客户端连接该服务，连上后其工具会动态提供给模型。Key
              仅存本地后端，绝不传给模型。
            </p>
            <div>
              <button type="button" onClick={() => setMcpOpen(false)}>
                取消
              </button>
              <button className="solid" disabled={saving} onClick={saveMcp}>
                {saving ? "验证中…" : "保存并连接"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function MessageBubble({
  message,
  assistantName,
}: {
  message: Message;
  assistantName?: string;
}) {
  let sources: Source[] = [];
  try {
    sources = message.sources ? JSON.parse(message.sources) : [];
  } catch {}
  return (
    <div className={`message ${message.role}`}>
      {message.role === "assistant" && (
        <span className="message-avatar">
          {assistantName?.slice(0, 1) || "A"}
        </span>
      )}
      <div>
        {message.role === "assistant" && (
          <small className="message-author">
            {assistantName || "默认助手"}
          </small>
        )}
        {message.content || <span className="typing">正在检索与思考</span>}
        {sources.length > 0 && (
          <div className="citations">
            <b>引用来源</b>
            {sources.map((source, index) => (
              <details key={`${source.document}-${index}`}>
                <summary>
                  {source.document} · 第 {source.page} 页
                </summary>
                <p>{source.quote}</p>
              </details>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function SkillsView({
  refresh,
  notice,
  draft,
  setDraft,
}: {
  refresh: () => Promise<void> | void;
  notice: (v: string) => void;
  draft: {
    name: string;
    description: string;
    trigger_phrases: string;
    content: string;
  };
  setDraft: (d: {
    name: string;
    description: string;
    trigger_phrases: string;
    content: string;
  }) => void;
}) {
  const [items, setItems] = useState<Skill[]>([]),
    [editing, setEditing] = useState<Skill | null>(null),
    [creating, setCreating] = useState(false),
    [saving, setSaving] = useState(false);
  const reload = () => listSkills().then(setItems);
  useEffect(() => {
    reload();
  }, []);
  const EMPTY_DRAFT = {
    name: "",
    description: "",
    trigger_phrases: "",
    content: "",
  };
  type DraftKey = "name" | "description" | "trigger_phrases" | "content";
  // 新建时受控绑定到提升态草稿（切页不丢，刷新清空）；编辑时仍走非受控 defaultValue
  const bind = (k: DraftKey) =>
    creating
      ? {
          value: draft[k],
          onChange: (e: { target: { value: string } }) =>
            setDraft({ ...draft, [k]: e.target.value }),
        }
      : { defaultValue: (editing?.[k] as string) || "" };
  const draftDirty =
    creating &&
    !!(
      draft.name ||
      draft.description ||
      draft.trigger_phrases ||
      draft.content
    );
  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const body = {
      name: String(f.get("name") || "").trim(),
      description: String(f.get("description") || ""),
      trigger_phrases: String(f.get("trigger_phrases") || ""),
      content: String(f.get("content") || ""),
    };
    if (!body.name) {
      notice("请填写技能名称");
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        await updateSkill(editing.id, { ...body, status: editing.status });
      } else {
        await createSkill(body);
        setDraft(EMPTY_DRAFT);
      }
      setEditing(null);
      setCreating(false);
      await reload();
      await refresh();
      notice("技能已保存");
    } catch (err) {
      notice(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }
  async function remove(s: Skill) {
    if (s.builtin) return;
    await deleteSkill(s.id);
    await reload();
    await refresh();
    notice("技能已删除");
  }
  const dialog = creating || editing;
  return (
    <section className="knowledge-page">
      <div className="knowledge-title">
        <div>
          <span className="section-code">RESOURCE / SKILLS</span>
          <h1>Skills 技能</h1>
          <p>
            技能为「生成回答」注入方法论与输出格式。对话时可手动勾选（强制启用），智能体也会按描述自动选用。内置技能只读。
          </p>
        </div>
        <button
          className="solid"
          onClick={() => {
            setEditing(null);
            setCreating(true);
          }}
        >
          ＋ 新建技能
        </button>
      </div>
      <div className="skill-grid">
        {items.map((s) => (
          <div className="skill-card" key={s.id}>
            <div className="skill-card-head">
              <div className="skill-mark">♧</div>
              <div>
                <strong>
                  {s.name}
                  {s.builtin && <i className="skill-builtin-tag">内置</i>}
                </strong>
                <small>{s.description || "未填写描述"}</small>
              </div>
            </div>
            {s.trigger_phrases && (
              <div className="skill-triggers">
                {s.trigger_phrases
                  .split(",")
                  .filter(Boolean)
                  .map((t) => (
                    <span key={t}>#{t.trim()}</span>
                  ))}
              </div>
            )}
            <div className="skill-card-foot">
              {s.builtin ? (
                <span className="conn-account">内置技能 · 只读</span>
              ) : (
                <>
                  <button
                    onClick={() => {
                      setCreating(false);
                      setEditing(s);
                    }}
                  >
                    编辑
                  </button>
                  <button className="danger-link" onClick={() => remove(s)}>
                    删除
                  </button>
                </>
              )}
            </div>
          </div>
        ))}
        {items.length === 0 && (
          <p className="models-empty">
            还没有技能，新建一个，或等待内置技能加载。
          </p>
        )}
      </div>
      {dialog && (
        <div className="dialog-backdrop">
          <form
            key={editing ? "edit-" + editing.id : "new"}
            className="dialog skill-dialog"
            onSubmit={save}
          >
            <h2>{editing ? "编辑技能" : "新建技能"}</h2>
            {draftDirty && (
              <p className="skill-draft-hint">
                已恢复未完成的草稿 ·{" "}
                <button type="button" onClick={() => setDraft(EMPTY_DRAFT)}>
                  清空
                </button>
              </p>
            )}
            <label>
              名称
              <input
                name="name"
                autoFocus
                {...bind("name")}
                placeholder="例如：周报助手"
                required
              />
            </label>
            <label>
              描述
              <textarea
                name="description"
                {...bind("description")}
                placeholder="写清何时该用这个技能——这直接影响自动选择的准确率"
              />
            </label>
            <label>
              触发词（可选，逗号分隔）
              <input
                name="trigger_phrases"
                {...bind("trigger_phrases")}
                placeholder="周报,汇报"
              />
            </label>
            <label>
              正文
              <textarea
                name="content"
                {...bind("content")}
                placeholder="注入回答步的方法论与输出格式"
                rows={5}
              />
            </label>
            <div>
              <button
                type="button"
                onClick={() => {
                  setEditing(null);
                  setCreating(false);
                }}
              >
                取消
              </button>
              <button className="solid" disabled={saving}>
                {saving ? "保存中…" : "保存"}
              </button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}

function KnowledgeView({
  items,
  refresh,
  notice,
}: {
  items: KnowledgeBase[];
  refresh: () => Promise<void> | void;
  notice: (v: string) => void;
}) {
  const [selected, setSelected] = useState(""),
    [creating, setCreating] = useState(false),
    [uploading, setUploading] = useState(false);
  const active = items.find((item) => item.id === (selected || items[0]?.id));
  useEffect(() => {
    if (!selected && items[0]) setSelected(items[0].id);
  }, [items, selected]);
  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget),
      name = String(form.get("name") || "").trim();
    if (!name) return;
    const item = await createKnowledgeBase(
      name,
      String(form.get("description") || ""),
    );
    setCreating(false);
    await refresh();
    setSelected(item.id);
    notice("知识库已创建");
  }
  async function upload(file?: File) {
    if (!file || !active) return;
    setUploading(true);
    try {
      await uploadDocument(active.id, file);
      await refresh();
      notice("文档已解析并完成分块");
    } catch (error) {
      notice(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
  }
  return (
    <section className="knowledge-page">
      <div className="knowledge-title">
        <div>
          <span className="section-code">RESOURCE / KNOWLEDGE</span>
          <h1>企业知识库</h1>
          <p>上传资料、检查解析状态，并将可信内容连接到智能工作台。</p>
        </div>
        <button className="solid" onClick={() => setCreating(true)}>
          ＋ 新建知识库
        </button>
      </div>
      <div className="knowledge-layout">
        <aside className="kb-list">
          <h3>
            知识空间 <small>{items.length}</small>
          </h3>
          {items.map((item) => (
            <button
              key={item.id}
              className={active?.id === item.id ? "active" : ""}
              onClick={() => setSelected(item.id)}
            >
              <b>▤</b>
              <span>
                <strong>{item.name}</strong>
                <small>{item.documents.length} 个文档</small>
              </span>
            </button>
          ))}
          {items.length === 0 && <p>还没有知识库，先新建一个。</p>}
        </aside>
        <div className="document-panel">
          {active ? (
            <>
              <div className="document-head">
                <div>
                  <h2>{active.name}</h2>
                  <p>{active.description || "未填写说明"}</p>
                </div>
                <label
                  className={`upload-button ${uploading ? "loading" : ""}`}
                >
                  ⇧ {uploading ? "正在解析…" : "上传文档"}
                  <input
                    type="file"
                    accept=".pdf,.docx,.txt,.md,.csv,.json"
                    disabled={uploading}
                    onChange={(e) => upload(e.target.files?.[0])}
                  />
                </label>
              </div>
              <div className="document-summary">
                <span>
                  <b>{active.documents.length}</b> 文档
                </span>
                <span>
                  <b>
                    {active.documents.reduce(
                      (sum, item) => sum + item.chunk_count,
                      0,
                    )}
                  </b>{" "}
                  文本分块
                </span>
                <span>
                  <b>
                    {Math.round(
                      active.documents.reduce(
                        (sum, item) => sum + item.size,
                        0,
                      ) / 1024,
                    )}
                  </b>{" "}
                  KB
                </span>
              </div>
              <table>
                <thead>
                  <tr>
                    <th>文件</th>
                    <th>状态</th>
                    <th>分块</th>
                    <th>大小</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {active.documents.map((document) => (
                    <tr key={document.id}>
                      <td>
                        <b className="file-icon">TXT</b>
                        <span>
                          {document.name}
                          <small>
                            {new Date(document.created_at).toLocaleString()}
                          </small>
                        </span>
                      </td>
                      <td>
                        <i className="ready-dot" /> 已就绪
                      </td>
                      <td>{document.chunk_count}</td>
                      <td>
                        {Math.max(1, Math.round(document.size / 1024))} KB
                      </td>
                      <td>
                        <button
                          className="danger-link"
                          onClick={async () => {
                            await deleteDocument(document.id);
                            await refresh();
                            notice("文档已删除");
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                  {active.documents.length === 0 && (
                    <tr>
                      <td colSpan={5} className="table-empty">
                        上传第一个文档，系统会自动解析和分块。
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </>
          ) : (
            <div className="empty-knowledge">
              <b>▤</b>
              <h2>从一个知识库开始</h2>
              <p>为某个业务主题建立独立的可信资料空间。</p>
              <button className="solid" onClick={() => setCreating(true)}>
                新建知识库
              </button>
            </div>
          )}
        </div>
      </div>
      {creating && (
        <div className="dialog-backdrop" onMouseDown={() => setCreating(false)}>
          <form
            className="dialog"
            onSubmit={create}
            onMouseDown={(e) => e.stopPropagation()}
          >
            <h2>新建知识库</h2>
            <label>
              名称
              <input
                name="name"
                autoFocus
                placeholder="例如：产品服务资料"
                required
              />
            </label>
            <label>
              说明
              <textarea
                name="description"
                placeholder="这个知识库服务于什么场景？"
              />
            </label>
            <div>
              <button type="button" onClick={() => setCreating(false)}>
                取消
              </button>
              <button className="solid">创建</button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}
