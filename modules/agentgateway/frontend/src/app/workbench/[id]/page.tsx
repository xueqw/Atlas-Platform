"use client";
import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { Agent, PromptConfig, ModelConfig, PromptTemplate, ModelRegistryItem, ReleaseGateResult, ReleaseGateFailure } from "@/lib/types";
import type { Message } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { toast } from "sonner";
import { ChatMessages } from "@/components/shared/ChatMessages";
import { ChatInput } from "@/components/shared/ChatInput";
import PipelineTimeline from "@/components/shared/PipelineTimeline";
import KNodeEditor from "@/components/workbench/KNodeEditor";
import TNodeEditor from "@/components/workbench/TNodeEditor";
import CompareChatView from "@/components/workbench/CompareChatView";
import DAGWorkbench from "@/components/workbench/DAGWorkbench";
import TestChatPanel from "@/components/workbench/TestChatPanel";
import EvaluationSuitePanel from "@/components/workbench/EvaluationSuitePanel";
import { createChatSocket } from "@/lib/ws";
import { ArrowLeft, Save, Eye, Play, Sparkles, Columns2, Layout, ClipboardCheck } from "lucide-react";
import type { PromptVersion, OptimizeResult } from "@/lib/types";

export default function AgentEditorPage() {
  const params = useParams();
  const router = useRouter();
  const agentId = Number(params.id);

  const [agent, setAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [selectedNode, setSelectedNode] = useState<string>("p");

  const [prompt, setPrompt] = useState<PromptConfig>({
    role_name: "", role_description: "", output_format: "markdown",
    constraints: "[]", system_prompt: "",
  });

  const [model, setModel] = useState<ModelConfig>({
    provider: "glm", model_name: "glm-4.7-flash",
    temperature: 0.7, max_tokens: 4096, top_p: 1.0, streaming: true,
  });

  const [templates, setTemplates] = useState<PromptTemplate[]>([]);
  const [modelRegistry, setModelRegistry] = useState<ModelRegistryItem[]>([]);
  const [previewOpen, setPreviewOpen] = useState(false);

  // Inline chat test
  const [testPanelOpen, setTestPanelOpen] = useState(false);
  const [testMessages, setTestMessages] = useState<Message[]>([]);
  const [streamingContent, setStreamingContent] = useState("");
  const [thinking, setThinking] = useState(false);
  const [wsStatus, setWsStatus] = useState<"connected" | "disconnected" | "reconnecting">("disconnected");
  const [socketRef, setSocketRef] = useState<ReturnType<typeof createChatSocket> | null>(null);
  const testMsgIdRef = useRef(0);
  const testStreamingRef = useRef("");
  const [dagMode, setDagMode] = useState(false);
  const [dagGraphJson, setDagGraphJson] = useState<string>("{}");
  const [namePromptOpen, setNamePromptOpen] = useState(false);
  const [pendingName, setPendingName] = useState("");
  const [compareMode, setCompareMode] = useState(false);
  const [versions, setVersions] = useState<PromptVersion[]>([]);
  const [plannerSnapshot, setPlannerSnapshot] = useState<{
    requirement_summary: string;
    task_classification: string;
    confirmed_constraints: string[];
    rationale: string;
    user_feedback_summary: string[];
    proposal_version: number;
    created_at: string;
  } | null>(null);
  const [snapshotExpanded, setSnapshotExpanded] = useState(true);
  const [evalOpen, setEvalOpen] = useState(false);

  // Release gate (pre-publish quality check)
  const [gateOpen, setGateOpen] = useState(false);
  const [gateLoading, setGateLoading] = useState(false);
  const [gateResult, setGateResult] = useState<ReleaseGateResult | null>(null);
  const [publishing, setPublishing] = useState(false);

  useEffect(() => {
    api.listPromptVersions(agentId).then(setVersions).catch(() => {});
    api.getDAGGraph(agentId).then((d) => { setDagGraphJson(d.graph_json); setDagMode(true); }).catch(() => {});
    api.getPlannerSnapshot(agentId).then(setPlannerSnapshot).catch(() => {});
  }, [agentId]);

  const nextTestId = () => { testMsgIdRef.current += 1; return testMsgIdRef.current; };

  useEffect(() => {
    return () => { socketRef?.disconnect(); };
  }, [socketRef]);

  useEffect(() => {
    Promise.all([
      api.getAgent(agentId),
      api.listTemplates(),
      api.listModels(),
    ]).then(([a, t, m]) => {
      setAgent(a);
      if (a.prompt_config) setPrompt(a.prompt_config);
      if (a.model) setModel(a.model);
      setTemplates(t);
      setModelRegistry(m);
      setLoading(false);
    }).catch(() => {
      toast.error("加载智能体失败");
      setLoading(false);
    });
  }, [agentId]);

  const doSave = useCallback(async (name: string) => {
    setSaving(true);
    try {
      const updated = await api.updateAgent(agentId, {
        name,
        description: agent?.description,
        prompt_config: prompt,
        model,
      });
      setAgent(updated);
      toast.success("已保存");
    } catch (e: any) {
      toast.error(e.message || "保存失败");
    }
    setSaving(false);
  }, [agentId, agent?.description, prompt, model]);

  const save = useCallback(async () => {
    if (agent?.name === "新建智能体") {
      setPendingName("");
      setNamePromptOpen(true);
      return;
    }
    await doSave(agent?.name || "");
  }, [agent?.name, doSave]);

  const handleNameConfirm = async () => {
    setNamePromptOpen(false);
    if (pendingName.trim()) {
      setAgent((a) => a ? { ...a, name: pendingName.trim() } : a);
      await doSave(pendingName.trim());
    }
  };

  const openGate = async () => {
    setGateOpen(true);
    setGateLoading(true);
    setGateResult(null);
    try {
      const result = await api.getReleaseGate(agentId);
      setGateResult(result);
    } catch (e: any) {
      toast.error(e.message || "门禁检查失败");
    }
    setGateLoading(false);
  };

  const handlePublish = async () => {
    setPublishing(true);
    try {
      await api.publishAgent(agentId);
      const updated = await api.getAgent(agentId);
      setAgent(updated);
      toast.success("智能体已发布！");
      setGateOpen(false);
    } catch (e: any) {
      toast.error(e.message || "发布失败");
    }
    setPublishing(false);
  };

  const previewPrompt = `${prompt.role_name ? `# 角色: ${prompt.role_name}\n\n` : ""}${prompt.role_description ? `${prompt.role_description}\n\n` : ""}${prompt.system_prompt}`;

  const selectedModelData = modelRegistry.find((m) => m.model_id === model.model_name);
  const estimatedCostPerTurn = selectedModelData
    ? ((selectedModelData.input_price_per_1k * (4000 / 1000)) + (selectedModelData.output_price_per_1k * (model.max_tokens / 1000))).toFixed(4)
    : "0.00";

  const startTestChat = () => {
    socketRef?.disconnect();
    setTestPanelOpen(true);
    setTestMessages([]);
    setStreamingContent("");
    testStreamingRef.current = "";
    const socket = createChatSocket(agentId);
    socket.onMessage((data) => {
      if (data.type === "thinking") {
        setThinking(true);
      } else if (data.type === "token" && data.content) {
        setThinking(false);
        testStreamingRef.current += data.content;
        setStreamingContent(testStreamingRef.current);
      } else if (data.type === "done") {
        setThinking(false);
        const content = testStreamingRef.current;
        testStreamingRef.current = "";
        setStreamingContent("");
        setTestMessages((msgs) => [
          ...msgs,
          { id: nextTestId(), conversation_id: 0, role: "assistant", content, created_at: new Date().toISOString() },
        ]);
      } else if (data.type === "error") {
        setThinking(false);
        toast.error(data.message || "对话出错");
        testStreamingRef.current = "";
        setStreamingContent("");
      }
    });
    socket.onStatus(setWsStatus);
    socket.connect();
    setSocketRef(socket);
  };

  const sendTestMessage = (content: string) => {
    setTestMessages((prev) => [
      ...prev,
      { id: nextTestId(), conversation_id: 0, role: "user", content, created_at: new Date().toISOString() },
    ]);
    socketRef?.send(content);
  };

  if (loading) {
    return <div className="flex-1 flex items-center justify-center text-muted-foreground">加载中...</div>;
  }
  if (!agent) {
    return <div className="flex-1 flex items-center justify-center text-muted-foreground">智能体不存在</div>;
  }

  return (
    <div className="flex-1 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-4 p-4 border-b shrink-0">
        <Button variant="ghost" size="icon" onClick={() => router.push("/workbench")}>
          <ArrowLeft className="w-4 h-4" />
        </Button>
        <Input
          value={agent.name}
          onChange={(e) => setAgent({ ...agent, name: e.target.value })}
          className="text-xl font-bold max-w-xs"
        />
        <div className="flex-1" />
        <Button variant="outline" size="sm" onClick={() => setPreviewOpen(true)}>
          <Eye className="w-4 h-4 mr-2" />预览
        </Button>
        <Button
          variant={dagMode ? "default" : "outline"}
          size="sm"
          onClick={() => setDagMode(!dagMode)}
        >
          <Layout className="w-4 h-4 mr-2" />
          {dagMode ? "DAG 工作台" : "流水线"}
        </Button>
        <Button variant="outline" size="sm" onClick={startTestChat} disabled={testPanelOpen}>
          <Play className="w-4 h-4 mr-2" />测试对话
        </Button>
        <Button variant="outline" size="sm" onClick={() => setEvalOpen(true)}>
          <ClipboardCheck className="w-4 h-4 mr-2" />评估
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => router.push(`/builder/deerflow?from_agent=${agentId}`)}
        >
          <Sparkles className="w-4 h-4 mr-2" />重规划
        </Button>
        <Button
          variant={compareMode ? "default" : "outline"}
          size="sm"
          onClick={() => {
            if (compareMode) {
              setCompareMode(false);
              if (socketRef) socketRef.disconnect();
            } else {
              setCompareMode(true);
              setTestPanelOpen(false);
            }
          }}
        >
          <Columns2 className="w-4 h-4 mr-2" />
          {compareMode ? "退出对比" : "版本对比"}
        </Button>
        <Button variant="outline" size="sm" onClick={save} disabled={saving}>
          <Save className="w-4 h-4 mr-2" />{saving ? "保存中..." : "保存"}
        </Button>
        <Button size="sm" onClick={openGate} disabled={agent.status === "published"}>
          发布
        </Button>
      </div>

      {/* Body: DAG workbench or pipeline editor */}
      {dagMode ? (
        <div className="flex-1 min-h-0 relative">
          <DAGWorkbench agentId={agentId} initialGraphJson={dagGraphJson} />
          <TestChatPanel agentId={agentId} />
          {plannerSnapshot && (
            <div className="absolute top-3 right-3 z-10 w-[320px] max-w-[40vw] rounded-xl border border-border/60 bg-card/95 backdrop-blur shadow-lg">
              <button
                type="button"
                onClick={() => setSnapshotExpanded((v) => !v)}
                className="w-full flex items-center justify-between px-3 py-2 text-xs font-semibold border-b border-border/40 hover:bg-muted/40"
              >
                <span className="flex items-center gap-1.5">
                  <Sparkles className="w-3.5 h-3.5 text-primary" />
                  规划师方案
                </span>
                <span className="text-[10px] text-muted-foreground">
                  {snapshotExpanded ? "收起" : "展开"}
                </span>
              </button>
              {snapshotExpanded && (
                <div className="p-3 space-y-3 max-h-[60vh] overflow-y-auto">
                  {plannerSnapshot.requirement_summary && (
                    <div className="space-y-1">
                      <h4 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">需求摘要</h4>
                      <p className="text-xs leading-relaxed">{plannerSnapshot.requirement_summary}</p>
                    </div>
                  )}
                  {plannerSnapshot.task_classification && (
                    <div className="space-y-1">
                      <h4 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">任务分型</h4>
                      <Badge variant="outline" className="text-[10px]">{plannerSnapshot.task_classification}</Badge>
                    </div>
                  )}
                  {plannerSnapshot.confirmed_constraints.length > 0 && (
                    <div className="space-y-1">
                      <h4 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">已确认约束</h4>
                      <ul className="space-y-0.5">
                        {plannerSnapshot.confirmed_constraints.map((c, i) => (
                          <li key={i} className="text-xs text-muted-foreground flex gap-1.5">
                            <span className="text-green-600 shrink-0">✓</span>
                            <span>{c}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {plannerSnapshot.rationale && (
                    <div className="space-y-1">
                      <h4 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">推荐理由</h4>
                      <p className="text-xs text-muted-foreground leading-relaxed">{plannerSnapshot.rationale}</p>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
      <div className="flex flex-1 min-h-0">
        {/* Left: Pipeline Timeline */}
        <div className="w-[240px] shrink-0 border-r bg-card overflow-y-auto">
          <div className="p-3 border-b">
            <h3 className="text-sm font-semibold">流水线</h3>
          </div>
          <PipelineTimeline
            agentId={agentId}
            selectedNode={selectedNode}
            onNodeSelect={setSelectedNode}
          />
        </div>

        {/* Right: Editor area */}
        <div className="flex-1 flex min-w-0">
          <div className={`flex-1 overflow-y-auto p-6 ${testPanelOpen ? "max-w-[55%]" : ""}`}>
            {selectedNode === "p" && (
              <PNodeEditor
                prompt={prompt}
                setPrompt={setPrompt}
                templates={templates}
                setTemplates={setTemplates}
                agentId={agentId}
                testMessages={testMessages}
              />
            )}
            {selectedNode === "m" && (
              <MNodeEditor
                model={model}
                setModel={setModel}
                modelRegistry={modelRegistry}
                estimatedCostPerTurn={estimatedCostPerTurn}
                selectedModelData={selectedModelData}
              />
            )}
            {selectedNode === "k" && <KNodeEditor agentId={agentId} />}
            {selectedNode === "t" && <TNodeEditor agentId={agentId} />}
          </div>

          {/* Test chat panel */}
          {testPanelOpen && (
            <div className="w-[45%] border-l flex flex-col">
              <div className="flex items-center justify-between p-3 border-b">
                <h3 className="text-sm font-medium">测试会话</h3>
                <div className="flex items-center gap-2">
                  <Badge variant={wsStatus === "connected" ? "default" : "destructive"} className="text-xs">
                    {wsStatus === "connected" ? "已连接" : wsStatus === "reconnecting" ? "重连中..." : "已断开"}
                  </Badge>
                  <Button variant="ghost" size="sm" onClick={() => {
                    socketRef?.disconnect();
                    setTestPanelOpen(false);
                    setTestMessages([]);
                    setStreamingContent("");
                  }}>关闭</Button>
                </div>
              </div>
              <ChatMessages messages={testMessages} streamingContent={streamingContent} thinking={thinking} />
              <ChatInput onSend={sendTestMessage} disabled={wsStatus !== "connected"} />
            </div>
          )}

          {/* Compare chat view */}
          {compareMode && (
            <div className="w-[60%] border-l flex flex-col">
              <CompareChatView
                agentId={agentId}
                versions={versions}
                currentPrompt={prompt.system_prompt}
                onClose={() => setCompareMode(false)}
              />
            </div>
          )}
        </div>
      </div>
      )}

      {evalOpen && (
        <EvaluationSuitePanel agentId={agentId} onClose={() => setEvalOpen(false)} />
      )}

      {/* Name prompt dialog */}
      <Dialog open={namePromptOpen} onOpenChange={setNamePromptOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>为智能体命名</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground">请为你的智能体取一个名字，方便之后识别和管理。</p>
            <div>
              <Label>名称</Label>
              <Input
                value={pendingName}
                onChange={(e) => setPendingName(e.target.value)}
                placeholder="输入智能体名称"
                autoFocus
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setNamePromptOpen(false)}>跳过</Button>
            <Button onClick={handleNameConfirm} disabled={!pendingName.trim()}>确认</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Release gate dialog (pre-publish) */}
      <Dialog open={gateOpen} onOpenChange={setGateOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>发布前质量门禁</DialogTitle>
          </DialogHeader>
          {gateLoading ? (
            <p className="text-sm text-muted-foreground py-6 text-center">门禁检查中...</p>
          ) : gateResult ? (
            <div className="space-y-4">
              {/* Overall verdict */}
              <div className="flex items-center gap-2">
                <Badge variant={gateResult.passed ? "default" : "destructive"}>
                  {gateResult.passed ? "达标，可发布" : "未达标，已阻止"}
                </Badge>
              </div>

              {/* Evaluation summary (4.1) */}
              {gateResult.summary.evaluation ? (
                <Card>
                  <CardHeader className="py-2"><CardTitle className="text-sm">最近评估摘要</CardTitle></CardHeader>
                  <CardContent className="text-xs space-y-1">
                    <p>
                      通过率：
                      <strong>
                        {gateResult.summary.evaluation.pass_rate != null
                          ? `${(gateResult.summary.evaluation.pass_rate * 100).toFixed(0)}%`
                          : "—"}
                      </strong>
                    </p>
                    <p>
                      关键 case：
                      <strong>
                        {gateResult.summary.evaluation.key_total
                          ? `${gateResult.summary.evaluation.key_passed ?? 0}/${gateResult.summary.evaluation.key_total} 通过`
                          : "无关键 case"}
                      </strong>
                    </p>
                    <p className="text-muted-foreground">
                      评估时间：{gateResult.summary.evaluation.created_at
                        ? new Date(gateResult.summary.evaluation.created_at).toLocaleString("zh-CN")
                        : "—"}
                    </p>
                  </CardContent>
                </Card>
              ) : (
                <p className="text-xs text-muted-foreground">暂无评估数据，请先在「评估」中运行一轮。</p>
              )}

              {/* Risk hints from observability (4.2) */}
              {gateResult.summary.observability && (
                <Card>
                  <CardHeader className="py-2"><CardTitle className="text-sm">运行风险</CardTitle></CardHeader>
                  <CardContent className="text-xs space-y-1">
                    <p>error rate：<strong>{(gateResult.summary.observability.error_rate * 100).toFixed(1)}%</strong></p>
                    <p>latency p95：<strong>{gateResult.summary.observability.latency_p95_ms.toFixed(0)} ms</strong></p>
                    <p>平均 token：<strong>{gateResult.summary.observability.avg_token.toFixed(0)}</strong></p>
                    <p className="text-muted-foreground">近 {gateResult.summary.observability.run_count} 次运行聚合</p>
                  </CardContent>
                </Card>
              )}

              {/* Dimension quality report (Phase 3, task 5.1): suite type,
                  per-dimension averages, the most-dragging dimension, and which
                  gated dimensions were skipped for lack of data. */}
              {gateResult.summary.dimensions_checked && gateResult.summary.dimension_averages && (
                <Card>
                  <CardHeader className="py-2">
                    <CardTitle className="text-sm">
                      质量维度
                      {gateResult.summary.suite_type && gateResult.summary.suite_type !== "general" && (
                        <Badge variant="secondary" className="ml-2 text-[10px] px-1.5 py-0 h-4 align-middle">
                          {gateResult.summary.suite_type}
                        </Badge>
                      )}
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="text-xs space-y-1">
                    {Object.entries(gateResult.summary.dimension_averages).map(([dim, avg]) => {
                      const gated = gateResult.summary.gate_dimensions?.find((g) => g.name === dim);
                      const below = gated?.min_avg != null && avg < gated.min_avg;
                      return (
                        <p key={dim} className={below ? "text-destructive" : ""}>
                          {dim}：<strong>{avg.toFixed(2)}</strong>
                          {gated?.min_avg != null && (
                            <span className="text-muted-foreground"> / 阈值 {gated.min_avg.toFixed(2)}</span>
                          )}
                        </p>
                      );
                    })}
                    {gateResult.summary.worst_dimension && (
                      <p className="text-muted-foreground pt-0.5">
                        拖累最大：<strong>{gateResult.summary.worst_dimension.dimension}</strong>
                        （{gateResult.summary.worst_dimension.avg.toFixed(2)}）
                      </p>
                    )}
                    {gateResult.summary.dimensions_skipped && gateResult.summary.dimensions_skipped.length > 0 && (
                      <p className="text-muted-foreground">
                        跳过（本轮无数据）：{gateResult.summary.dimensions_skipped.join("、")}
                      </p>
                    )}
                    {gateResult.summary.evaluation?.run_id != null && (
                      <button
                        onClick={() => { setGateOpen(false); setEvalOpen(true); }}
                        className="text-primary hover:underline inline-flex items-center gap-0.5 pt-1"
                      >
                        查看评估详情 ↗
                      </button>
                    )}
                  </CardContent>
                </Card>
              )}

              {/* Blocking reasons (4.3) */}
              {!gateResult.passed && gateResult.reasons.length > 0 && (
                <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3">
                  <h4 className="text-sm font-medium text-destructive mb-1.5">未达标原因</h4>
                  <ul className="space-y-1.5">
                    {(gateResult.failures && gateResult.failures.length > 0
                      ? gateResult.failures
                      : gateResult.reasons.map((r) => ({ code: "", label: r } as ReleaseGateFailure))
                    ).map((f, i) => (
                      <li key={i} className="text-xs text-destructive">
                        <div className="flex gap-1.5">
                          <span className="shrink-0">•</span>
                          <span>{f.label}</span>
                        </div>
                        {/* Key-case failure list for required-dimension failures. */}
                        {f.cases && f.cases.length > 0 && (
                          <ul className="mt-0.5 ml-4 space-y-0.5 text-muted-foreground">
                            {f.cases.slice(0, 5).map((c, j) => (
                              <li key={j}>
                                {c.case_name || `case #${c.case_id}`}
                                {c.dimension ? ` · ${c.dimension}` : ""}
                                {typeof c.score === "number" ? ` · ${c.score.toFixed(2)}` : ""}
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>
                  <p className="text-xs text-muted-foreground mt-2">
                    请到「评估」补跑评估或修复后重试。
                  </p>
                </div>
              )}

              <DialogFooter>
                <Button variant="outline" onClick={() => setGateOpen(false)}>取消</Button>
                <Button onClick={handlePublish} disabled={!gateResult.passed || publishing}>
                  {publishing ? "发布中..." : "确认发布"}
                </Button>
              </DialogFooter>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      {/* Preview dialog */}
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="max-w-2xl max-h-[80vh]">
          <DialogHeader>
            <DialogTitle>组装后的系统提示词</DialogTitle>
          </DialogHeader>
          <ScrollArea className="max-h-[60vh]">
            <pre className="text-sm whitespace-pre-wrap font-mono bg-muted p-4 rounded">{previewPrompt}</pre>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/* ── P Node Sub-Component ── */
function PNodeEditor({
  prompt, setPrompt, templates, setTemplates, agentId, testMessages,
}: {
  prompt: PromptConfig;
  setPrompt: (p: PromptConfig) => void;
  templates: PromptTemplate[];
  setTemplates: (t: PromptTemplate[]) => void;
  agentId: number;
  testMessages: Message[];
}) {
  const [versions, setVersions] = useState<PromptVersion[]>([]);
  const [optimizing, setOptimizing] = useState(false);
  const [optimizeResult, setOptimizeResult] = useState<OptimizeResult | null>(null);
  const [optimizeDialog, setOptimizeDialog] = useState(false);

  useEffect(() => {
    api.listPromptVersions(agentId).then(setVersions).catch(() => {});
  }, [agentId]);

  const handleLoadVersion = async (versionId: string | null) => {
    if (!versionId) return;
    try {
      const v = await api.getPromptVersion(agentId, Number(versionId));
      const config = JSON.parse(v.prompt_config);
      setPrompt({
        ...prompt,
        role_name: config.role_name || "",
        role_description: config.role_description || "",
        system_prompt: config.system_prompt || "",
        output_format: config.output_format || "markdown",
        constraints: config.constraints || "[]",
      });
      toast.success(`已加载版本 ${v.version_number}`);
    } catch {
      toast.error("加载版本失败");
    }
  };

  const handleOptimize = async () => {
    setOptimizing(true);
    try {
      const recentMsgs = testMessages.slice(-6).map((m) => ({ role: m.role, content: m.content }));
      const result = await api.optimizePrompt(agentId, {
        role_name: prompt.role_name,
        role_description: prompt.role_description,
        system_prompt: prompt.system_prompt,
      }, recentMsgs);
      setOptimizeResult(result);
      setOptimizeDialog(true);
    } catch (e: any) {
      toast.error(e.message || "优化失败");
    }
    setOptimizing(false);
  };

  const handleAcceptOptimization = () => {
    if (!optimizeResult) return;
    setPrompt({
      ...prompt,
      role_name: optimizeResult.optimized_prompt.role_name || prompt.role_name,
      role_description: optimizeResult.optimized_prompt.role_description || prompt.role_description,
      system_prompt: optimizeResult.optimized_prompt.system_prompt || prompt.system_prompt,
    });
    setOptimizeDialog(false);
    setOptimizeResult(null);
    toast.success("已应用优化提示词，请保存");
  };

  return (
    <div className="flex flex-col gap-4 max-w-3xl">
      {/* Version selector */}
      {versions.length > 0 && (
        <div>
          <Label>版本历史</Label>
          <Select onValueChange={handleLoadVersion}>
            <SelectTrigger>
              <SelectValue placeholder={`${versions.length} 个版本`} />
            </SelectTrigger>
            <SelectContent>
              {versions.map((v) => {
                let preview = "";
                try {
                  const cfg = JSON.parse(v.prompt_config);
                  preview = (cfg.system_prompt || "").substring(0, 50);
                } catch { /* ignore */ }
                return (
                  <SelectItem key={v.id} value={String(v.id)}>
                    v{v.version_number} — {new Date(v.created_at).toLocaleString("zh-CN")} — {preview}...
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
        </div>
      )}

      {/* Optimize button */}
      <div>
        <Button
          variant="outline" size="sm"
          onClick={handleOptimize}
          disabled={optimizing}
        >
          <Sparkles className="w-3 h-3 mr-1" />
          {optimizing ? "优化中..." : "AI 优化提示词"}
        </Button>
        {testMessages.length === 0 && (
          <p className="text-xs text-muted-foreground mt-1">
            先在测试对话中发送消息，优化效果更好
          </p>
        )}
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <Label>角色名称</Label>
          <Input value={prompt.role_name} onChange={(e) => setPrompt({ ...prompt, role_name: e.target.value })} placeholder="例如：客服智能体" />
        </div>
        <div>
          <Label>输出格式</Label>
          <Select value={prompt.output_format} onValueChange={(v) => setPrompt({ ...prompt, output_format: v ?? "markdown" })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="markdown">Markdown</SelectItem>
              <SelectItem value="json">JSON</SelectItem>
              <SelectItem value="text">纯文本</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <div>
        <Label>角色描述</Label>
        <Textarea value={prompt.role_description} onChange={(e) => setPrompt({ ...prompt, role_description: e.target.value })} placeholder="描述智能体的角色和用途..." rows={3} />
      </div>
      <div>
        <Label>系统提示词</Label>
        <Textarea value={prompt.system_prompt} onChange={(e) => setPrompt({ ...prompt, system_prompt: e.target.value })} placeholder="编写系统提示词..." rows={12} className="font-mono text-sm" />
      </div>
      <Card>
        <CardHeader className="py-2"><CardTitle className="text-sm">质量检查</CardTitle></CardHeader>
        <CardContent className="flex gap-3 text-xs">
          <Badge variant={prompt.role_name ? "default" : "secondary"}>角色: {prompt.role_name ? "通过" : "未通过"}</Badge>
          <Badge variant={prompt.system_prompt.length >= 200 && prompt.system_prompt.length <= 4000 ? "default" : "secondary"}>长度: {prompt.system_prompt.length >= 200 ? "通过" : "未通过"} ({prompt.system_prompt.length}/200-4000)</Badge>
          <Badge variant={prompt.output_format ? "default" : "secondary"}>格式: {prompt.output_format ? "通过" : "未通过"}</Badge>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="py-2">
          <CardTitle className="text-sm flex items-center justify-between">
            提示词模板
            <Button variant="ghost" size="sm" className="text-xs" onClick={async () => {
              const name = prompt.role_name || "未命名";
              try {
                await api.createTemplate(name, "custom", prompt.system_prompt);
                const updated = await api.listTemplates();
                setTemplates(updated);
                toast.success("模板已保存");
              } catch { toast.error("保存模板失败"); }
            }}>保存当前为模板</Button>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <ScrollArea className="h-28">
            <div className="flex gap-2">
              {templates.map((t) => (
                <Button key={t.id} variant="outline" size="sm" className="text-xs whitespace-nowrap" onClick={() => setPrompt({ ...prompt, system_prompt: t.content, role_name: t.name })}>
                  {t.name}
                </Button>
              ))}
            </div>
          </ScrollArea>
        </CardContent>
      </Card>

      {/* Optimize diff dialog */}
      <Dialog open={optimizeDialog} onOpenChange={setOptimizeDialog}>
        <DialogContent className="max-w-4xl max-h-[85vh]">
          <DialogHeader>
            <DialogTitle>优化建议</DialogTitle>
          </DialogHeader>
          {optimizeResult && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4 max-h-[50vh] overflow-y-auto">
                <div>
                  <h4 className="text-sm font-medium mb-2 text-destructive">原始提示词</h4>
                  <pre className="text-xs whitespace-pre-wrap bg-muted p-3 rounded">
                    {prompt.system_prompt}
                  </pre>
                </div>
                <div>
                  <h4 className="text-sm font-medium mb-2 text-green-600">优化后提示词</h4>
                  <pre className="text-xs whitespace-pre-wrap bg-muted p-3 rounded">
                    {optimizeResult.optimized_prompt.system_prompt}
                  </pre>
                </div>
              </div>
              {optimizeResult.changes && optimizeResult.changes.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium mb-2">变更详情</h4>
                  <ScrollArea className="max-h-[200px]">
                    <div className="space-y-2">
                      {optimizeResult.changes.map((c, i) => (
                        <div key={i} className="border rounded p-3 text-sm">
                          <div className="flex items-center gap-2 mb-1">
                            <Badge variant="outline">{c.field}</Badge>
                            <span className="text-xs text-muted-foreground">{c.reason}</span>
                          </div>
                          <div className="grid grid-cols-2 gap-2 text-xs">
                            <div>
                              <span className="text-destructive">- {c.before?.substring(0, 100)}</span>
                            </div>
                            <div>
                              <span className="text-green-600">+ {c.after?.substring(0, 100)}</span>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </ScrollArea>
                </div>
              )}
              <div className="flex justify-end gap-2">
                <Button variant="outline" onClick={() => { setOptimizeDialog(false); setOptimizeResult(null); }}>取消</Button>
                <Button onClick={handleAcceptOptimization}>接受优化</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

/* ── M Node Sub-Component ── */
function MNodeEditor({
  model, setModel, modelRegistry, estimatedCostPerTurn, selectedModelData,
}: {
  model: ModelConfig;
  setModel: (m: ModelConfig) => void;
  modelRegistry: ModelRegistryItem[];
  estimatedCostPerTurn: string;
  selectedModelData: ModelRegistryItem | undefined;
}) {
  return (
    <div className="flex flex-col gap-4 max-w-3xl">
      <div className="grid grid-cols-2 gap-4">
        <div>
          <Label>模型提供商</Label>
          <Select value={model.provider} onValueChange={(v) => setModel({ ...model, provider: v ?? "glm" })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="openai">OpenAI</SelectItem>
              <SelectItem value="anthropic">Anthropic</SelectItem>
              <SelectItem value="deepseek">DeepSeek</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label>模型</Label>
          <Select value={model.model_name} onValueChange={(v) => setModel({ ...model, model_name: v ?? "glm-4.7-flash" })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {modelRegistry.map((m) => (
                <SelectItem key={m.id} value={m.model_id}>{m.display_name} ({m.provider})</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-4">
        <div>
          <Label>温度 ({model.temperature})</Label>
          <Input type="number" min={0} max={2} step={0.1} value={model.temperature} onChange={(e) => setModel({ ...model, temperature: parseFloat(e.target.value) || 0.7 })} />
        </div>
        <div>
          <Label>最大 Token 数</Label>
          <Input type="number" value={model.max_tokens} onChange={(e) => setModel({ ...model, max_tokens: parseInt(e.target.value) || 4096 })} />
        </div>
        <div>
          <Label>Top P</Label>
          <Input type="number" min={0} max={1} step={0.1} value={model.top_p} onChange={(e) => setModel({ ...model, top_p: parseFloat(e.target.value) || 1.0 })} />
        </div>
      </div>
      <Card>
        <CardHeader className="py-2"><CardTitle className="text-sm">成本估算</CardTitle></CardHeader>
        <CardContent className="text-sm space-y-1">
          <p>已选择: <strong>{selectedModelData?.display_name || model.model_name}</strong></p>
          <p>输入: ${selectedModelData?.input_price_per_1k || 0} / 1K tokens</p>
          <p>输出: ${selectedModelData?.output_price_per_1k || 0} / 1K tokens</p>
          <Separator className="my-1" />
          <p className="font-medium">预估 ~${estimatedCostPerTurn} / 轮</p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="py-2"><CardTitle className="text-sm">参数校验</CardTitle></CardHeader>
        <CardContent className="flex gap-3 text-xs">
          <Badge variant={modelRegistry.some((m) => m.model_id === model.model_name) ? "default" : "destructive"}>模型: {modelRegistry.some((m) => m.model_id === model.model_name) ? "通过" : "失败"}</Badge>
          <Badge variant={model.temperature >= 0 && model.temperature <= 2 ? "default" : "destructive"}>温度: {model.temperature >= 0 && model.temperature <= 2 ? "通过" : "失败"}</Badge>
          <Badge variant={model.max_tokens >= 256 && model.max_tokens <= 128000 ? "default" : "destructive"}>最大Token: {model.max_tokens >= 256 && model.max_tokens <= 128000 ? "通过" : "失败"}</Badge>
        </CardContent>
      </Card>
    </div>
  );
}