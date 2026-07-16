"use client";
import { useEffect, useState, useRef, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Wand2, Plus, Loader2, Send, Check, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { getWsBase } from "@/lib/runtime-env";

function statusBadge(status: string) {
  switch (status) {
    case "draft": return <Badge variant="secondary">草稿</Badge>;
    case "testing": return <Badge variant="outline">测试中</Badge>;
    case "published": return <Badge>已发布</Badge>;
    case "deprecated": return <Badge variant="destructive">已弃用</Badge>;
    default: return <Badge variant="secondary">{status}</Badge>;
  }
}

interface BuilderMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
}

interface Recommendation {
  summary: string;
  prompt: { role_name: string; role_description: string; system_prompt: string };
  model: { provider: string; model_name: string; reason: string };
  tools: Array<{ name: string; reason: string }>;
  dag?: { nodes: Array<{ type: string; label: string }>; edges: Array<{ from: number; to: number }> };
}

function tryParseRecommendation(text: string): Recommendation | null {
  try {
    let jsonText = text;
    if (text.includes("```json")) {
      jsonText = text.split("```json")[1].split("```")[0].trim();
    } else if (text.includes("```")) {
      jsonText = text.split("```")[1].split("```")[0].trim();
    }
    const data = JSON.parse(jsonText);
    if (data.ready && data.recommendation) return data.recommendation as Recommendation;
    return null;
  } catch { return null; }
}

export default function WorkbenchPage() {
  const router = useRouter();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Manual create dialog
  const [manualDialog, setManualDialog] = useState(false);
  const [manualName, setManualName] = useState("");
  const [manualDesc, setManualDesc] = useState("");
  const [creating, setCreating] = useState(false);

  // Template dialog
  const [templateDialog, setTemplateDialog] = useState(false);
  const [templates, setTemplates] = useState<Array<{ id: number; name: string; description: string; category: string; graph_json: string; icon: string }>>([]);
  const [templateLoading, setTemplateLoading] = useState(false);
  const [templateError, setTemplateError] = useState<string | null>(null);

  // Builder wizard dialog
  const [builderDialog, setBuilderDialog] = useState(false);
  const [builderMessages, setBuilderMessages] = useState<BuilderMessage[]>([]);
  const [builderInput, setBuilderInput] = useState("");
  const [builderThinking, setBuilderThinking] = useState(false);
  const [builderStreaming, setBuilderStreaming] = useState("");
  const [builderWsReady, setBuilderWsReady] = useState(false);
  const [builderRec, setBuilderRec] = useState<Recommendation | null>(null);
  const [builderApplying, setBuilderApplying] = useState(false);
  const [builderModel, setBuilderModel] = useState("qwen3.6-27b");
  const [builderModels, setBuilderModels] = useState<Array<{ model_id: string; display_name: string; provider: string }>>([]);
  const builderMsgIdRef = useRef(0);
  const builderStreamRef = useRef("");
  const builderWsRef = useRef<WebSocket | null>(null);
  const builderConvIdRef = useRef("");

  useEffect(() => {
    api.listAgents()
      .then(setAgents)
      .catch(() => setLoadError("无法加载智能体列表，请确认后端服务已启动"))
      .finally(() => setLoading(false));
  }, []);

  const reloadAgents = () => {
    api.listAgents().then(setAgents);
  };

  const handleDeleteAgent = async (e: React.MouseEvent, agentId: number) => {
    e.preventDefault();
    e.stopPropagation();
    if (!window.confirm("确定要删除该智能体吗？")) return;
    try {
      await api.deleteAgent(agentId);
      setAgents((prev) => prev.filter((a) => a.id !== agentId));
      toast.success("已删除");
    } catch {
      toast.error("删除失败");
    }
  };

  // Manual create — directly creates agent with default P→M DAG, no name prompt
  const handleManualCreate = async () => {
    if (creating) return;
    setCreating(true);
    try {
      const agent = await api.createAgent("新建智能体", "");
      const defaultDAG = JSON.stringify({
        nodes: [
          { id: "p1", type: "p", position: { x: 200, y: 40 }, config: {
            role_name: "AI助手", role_description: "", system_prompt: "你是一个有帮助的AI助手。", output_format: "markdown"
          }},
          { id: "m1", type: "m", position: { x: 400, y: 40 }, config: {
            model_name: "qwen3.6-27b", provider: "glm", temperature: 0.7, max_tokens: 4096, streaming: true
          }},
          { id: "agent1", type: "agent", position: { x: 250, y: 200 }, config: {
            role_name: "AI助手", system_prompt: "你是一个有帮助的AI助手。", output_format: "markdown",
            model_name: "qwen3.6-27b", provider: "glm", temperature: 0.7, max_tokens: 4096, streaming: true
          }},
        ],
        edges: [
          { id: "e1", source: "p1", target: "agent1", targetHandle: "prompt" },
          { id: "e2", source: "m1", target: "agent1", targetHandle: "model" },
        ],
      });
      await api.saveDAGGraph(agent.id, defaultDAG);
      router.push(`/workbench/${agent.id}`);
    } catch {
      toast.error("创建失败，请确认后端服务已启动");
    } finally {
      setCreating(false);
    }
  };

  // Template create
  const handleTemplateCreate = async (templateId: number) => {
    setCreating(true);
    try {
      const result = await api.applyDAGTemplate(templateId);
      router.push(`/workbench/${result.agent_id}`);
    } catch {
      toast.error("从模板创建失败");
      setCreating(false);
    }
  };

  // Builder wizard
  const startBuilder = useCallback(async () => {
    setBuilderDialog(true);
    api.listModels().then((models) => setBuilderModels(models.map((m) => ({ model_id: m.model_id, display_name: m.display_name, provider: m.provider })))).catch(() => {});
    try {
      const res = await api.startBuilder();
      builderConvIdRef.current = res.conversation_id;
      setBuilderMessages([{ id: 1, role: "assistant", content: res.greeting }]);
      builderMsgIdRef.current = 1;

      const ws = new WebSocket(`${getWsBase()}/api/builder/ws/${res.conversation_id}`);
      builderWsRef.current = ws;

      ws.onopen = () => setBuilderWsReady(true);
      ws.onmessage = (event) => {
        let data: any;
        try { data = JSON.parse(event.data); } catch { return; }
        if (data.type === "thinking") {
          setBuilderThinking(true);
        } else if (data.type === "token" && data.content) {
          setBuilderThinking(false);
          builderStreamRef.current += data.content;
          setBuilderStreaming(builderStreamRef.current);
        } else if (data.type === "done") {
          setBuilderThinking(false);
          const content = builderStreamRef.current;
          builderStreamRef.current = "";
          setBuilderStreaming("");
          const id = builderMsgIdRef.current + 1;
          builderMsgIdRef.current = id;
          setBuilderMessages((prev) => [...prev, { id, role: "assistant", content }]);

          const rec = tryParseRecommendation(content);
          if (rec) setBuilderRec(rec);
        } else if (data.type === "error") {
          setBuilderThinking(false);
          builderStreamRef.current = "";
          setBuilderStreaming("");
          toast.error(data.message || "出错了");
        }
      };
      ws.onclose = () => setBuilderWsReady(false);
      ws.onerror = () => {
        setBuilderWsReady(false);
        setBuilderMessages((prev) => [
          ...prev,
          { id: prev.length + 1, role: "assistant", content: "⚠️ 连接失败，请确认后端服务已启动后重试。" },
        ]);
      };
    } catch {
      setBuilderMessages([{ id: 1, role: "assistant", content: "⚠️ 无法启动构建向导，请确认后端服务已启动。" }]);
    }
  }, []);

  const builderSend = () => {
    if (!builderInput.trim() || !builderWsRef.current || builderWsRef.current.readyState !== WebSocket.OPEN) return;
    builderWsRef.current.send(JSON.stringify({ type: "message", content: builderInput }));
    const id = builderMsgIdRef.current + 1;
    builderMsgIdRef.current = id;
    setBuilderMessages((prev) => [...prev, { id, role: "user", content: builderInput }]);
    setBuilderInput("");
  };

  const builderApply = async () => {
    if (!builderRec) return;
    setBuilderApplying(true);
    try {
      const agent = await api.applyBuilderRecommendation(
        builderRec as unknown as Record<string, unknown>,
        builderRec.prompt.role_name || "新建智能体"
      );
      closeBuilder();
      router.push(`/workbench/${agent.id}`);
    } catch {
      toast.error("应用失败");
      setBuilderApplying(false);
    }
  };

  const closeBuilder = () => {
    builderWsRef.current?.close();
    setBuilderDialog(false);
    setBuilderMessages([]);
    setBuilderRec(null);
    setBuilderInput("");
    setBuilderWsReady(false);
  };

  return (
    <div className="flex-1 p-6 max-w-5xl mx-auto w-full">
      <h1 className="text-2xl font-bold mb-6">智能体工作台</h1>

      {/* Creation entries */}
      <div className="grid grid-cols-3 gap-4 mb-8">
        <Card className="hover:border-primary transition-colors cursor-pointer" onClick={() => router.push("/builder/deerflow")}>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Wand2 className="w-5 h-5" />
              智能体规划师
            </CardTitle>
            <CardDescription>AI 架构师多轮对话，智能推荐架构方案并一键创建</CardDescription>
          </CardHeader>
        </Card>
        <Card className="hover:border-primary transition-colors cursor-pointer" onClick={handleManualCreate}>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Plus className="w-5 h-5" />
              手动创建
            </CardTitle>
            <CardDescription>直接创建 DAG 智能体，默认 P→M 即可对话，名称稍后设定</CardDescription>
          </CardHeader>
        </Card>
        <Card className="hover:border-primary transition-colors cursor-pointer" onClick={() => setTemplateDialog(true)}>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Wand2 className="w-5 h-5" />
              从模板创建
            </CardTitle>
            <CardDescription>选择预置 DAG 模板，快速搭建常见场景</CardDescription>
          </CardHeader>
        </Card>
      </div>

      {/* Agent list */}
      <h2 className="text-lg font-semibold mb-4">已有智能体</h2>
      {loading ? (
        <div className="text-center text-muted-foreground py-12">加载中...</div>
      ) : loadError ? (
        <div className="text-center py-12">
          <p className="text-sm text-destructive mb-3">{loadError}</p>
          <Button variant="outline" size="sm" onClick={() => {
            setLoading(true);
            setLoadError(null);
            api.listAgents()
              .then(setAgents)
              .catch(() => setLoadError("无法加载智能体列表，请确认后端服务已启动"))
              .finally(() => setLoading(false));
          }}>重试</Button>
        </div>
      ) : agents.length === 0 ? (
        <div className="text-center text-muted-foreground py-12">
          <p>暂无智能体，选择上方方式创建</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3">
          {agents.map((agent) => (
            <Link key={agent.id} href={`/workbench/${agent.id}`}>
              <Card className="hover:border-primary transition-colors cursor-pointer h-full">
                <CardContent className="p-3">
                  <div className="flex items-start justify-between gap-1 mb-1">
                    <p className="text-sm font-medium truncate flex-1">{agent.name}</p>
                    <div className="flex items-center gap-1 shrink-0">
                      {statusBadge(agent.status)}
                      <button
                        onClick={(e) => handleDeleteAgent(e, agent.id)}
                        className="p-0.5 rounded hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition-colors"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                  <p className="text-xs text-muted-foreground truncate">
                    {agent.description || "暂无描述"}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {agent.prompt_config ? (
                      <span>{agent.model?.model_name || "未配置模型"}</span>
                    ) : (
                      <span>DAG 智能体</span>
                    )}
                  </p>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}

      {/* Manual create dialog */}
      <Dialog open={manualDialog} onOpenChange={setManualDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>手动创建智能体</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={manualName}
                onChange={(e) => setManualName(e.target.value)}
                placeholder="输入智能体名称"
              />
            </div>
            <div>
              <Label>描述</Label>
              <Textarea
                value={manualDesc}
                onChange={(e) => setManualDesc(e.target.value)}
                placeholder="简要描述智能体的用途（可选）"
                rows={3}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setManualDialog(false)}>取消</Button>
            <Button onClick={handleManualCreate} disabled={creating || !manualName.trim()}>
              {creating ? "创建中..." : "创建"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Template selection dialog */}
      <Dialog open={templateDialog} onOpenChange={(open) => {
        setTemplateDialog(open);
        if (open) {
          setTemplateLoading(true);
          setTemplateError(null);
          api.listDAGTemplates()
            .then(setTemplates)
            .catch(() => setTemplateError("加载模板失败，请确认后端服务已启动"))
            .finally(() => setTemplateLoading(false));
        }
      }}>
        <DialogContent className="max-w-2xl max-h-[80vh]">
          <DialogHeader>
            <DialogTitle>选择 DAG 模板</DialogTitle>
          </DialogHeader>
          <ScrollArea className="max-h-[60vh]">
            <div className="grid gap-3">
              {templates.map((t) => (
                <Card
                  key={t.id}
                  className="cursor-pointer hover:border-primary transition-colors"
                  onClick={() => handleTemplateCreate(t.id)}
                >
                  <CardHeader>
                    <CardTitle className="text-base">{t.name}</CardTitle>
                    <CardDescription>{t.description}</CardDescription>
                  </CardHeader>
                </Card>
              ))}
              {templateLoading && (
                <p className="text-sm text-muted-foreground text-center py-8">加载中...</p>
              )}
              {templateError && (
                <p className="text-sm text-destructive text-center py-8">{templateError}</p>
              )}
              {!templateLoading && !templateError && templates.length === 0 && (
                <p className="text-sm text-muted-foreground text-center py-8">暂无可用模板</p>
              )}
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>

      {/* Builder wizard dialog */}
      <Dialog open={builderDialog} onOpenChange={(open) => { if (!open) closeBuilder(); }}>
        <DialogContent className="max-w-[96vw] w-[96vw] h-[92vh] flex flex-col">
          <DialogHeader className="shrink-0">
            <div className="flex items-center justify-between">
              <DialogTitle className="flex items-center gap-2">
                <Wand2 className="w-5 h-5" />
                构建向导
              </DialogTitle>
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">模型:</span>
                <select
                  value={builderModel}
                  onChange={(e) => setBuilderModel(e.target.value)}
                  className="text-xs border border-border rounded px-2 py-1 bg-background"
                >
                  {builderModels.map((m) => (
                    <option key={m.model_id} value={m.model_id}>{m.display_name} ({m.provider})</option>
                  ))}
                  {builderModels.length === 0 && <option value="qwen3.6-27b">Qwen 3.6 27B</option>}
                </select>
              </div>
            </div>
          </DialogHeader>

          <div className="flex-1 flex gap-4 min-h-0 overflow-hidden">
            {/* Left: Chat area */}
            <div className="flex-1 flex flex-col min-h-0 min-w-0 overflow-hidden">
              <div className="flex-1 overflow-y-auto min-h-0">
                <div className="space-y-3 p-1">
                  {builderMessages.map((m) => (
                    <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                      <div className={`max-w-[85%] rounded-lg px-4 py-2 text-sm ${
                        m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted"
                      }`}>
                        <div className="whitespace-pre-wrap break-words">{m.content}</div>
                      </div>
                    </div>
                  ))}
                  {builderThinking && !builderStreaming && (
                    <div className="flex justify-start">
                      <div className="rounded-lg px-4 py-2 bg-muted">
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Loader2 className="w-3 h-3 animate-spin" />
                          构建向导正在思考...
                        </div>
                      </div>
                    </div>
                  )}
                  {builderStreaming && (
                    <div className="flex justify-start">
                      <div className="max-w-[85%] rounded-lg px-4 py-2 text-sm bg-muted">
                        <div className="whitespace-pre-wrap break-words">{builderStreaming}</div>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {!builderRec && (
                <div className="flex items-center gap-2 mt-3 pt-3 border-t shrink-0">
                  <Input
                    placeholder="描述你的智能体需求..."
                    value={builderInput}
                    onChange={(e) => setBuilderInput(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && builderSend()}
                    disabled={!builderWsReady}
                    className="flex-1"
                  />
                  <Button onClick={builderSend} disabled={!builderWsReady || !builderInput.trim()}>
                    <Send className="w-4 h-4" />
                  </Button>
                </div>
              )}
            </div>

            {/* Right: Recommendation panel */}
            {builderRec && (
              <div className="w-72 shrink-0 border-l pl-4 flex flex-col">
                <Card className="border-primary/50 bg-primary/5 flex-1">
                  <CardHeader className="py-3">
                    <CardTitle className="text-sm flex items-center gap-2">
                      <Check className="w-4 h-4 text-green-600" />推荐配置
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3 text-xs">
                    <div>
                      <span className="text-muted-foreground">名称:</span>
                      <p className="font-medium">{builderRec.prompt.role_name}</p>
                    </div>
                    <div>
                      <span className="text-muted-foreground">模型:</span>
                      <p className="font-medium">{builderRec.model.provider}/{builderRec.model.model_name}</p>
                    </div>
                    <div>
                      <span className="text-muted-foreground">提示词:</span>
                      <p className="text-muted-foreground line-clamp-4">{builderRec.prompt.system_prompt || builderRec.prompt.role_description}</p>
                    </div>
                    {builderRec.tools && builderRec.tools.length > 0 && (
                      <div>
                        <span className="text-muted-foreground">工具:</span>
                        <p className="font-medium">{builderRec.tools.map((t) => t.name).join(", ")}</p>
                      </div>
                    )}
                    <Button onClick={builderApply} disabled={builderApplying} size="sm" className="w-full mt-4">
                      {builderApplying && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
                      应用并进入工作台
                    </Button>
                  </CardContent>
                </Card>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}