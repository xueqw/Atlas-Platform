"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import { X, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { toast } from "sonner";
import PromptCapabilityEditor from "./PromptCapabilityEditor";
import ModelCapabilityPicker from "./ModelCapabilityPicker";
import ToolCapabilityPicker from "./ToolCapabilityPicker";
import AgentNodeEditor from "./AgentNodeEditor";
import { getWsBase } from "@/lib/runtime-env";
import { api } from "@/lib/api";

interface NodeConfig {
  [key: string]: string | number | boolean | undefined;
}

interface Props {
  nodeId: string;
  nodeType: string;
  displayName: string;
  config: NodeConfig;
  agentId?: number;
  onSave: (nodeId: string, config: NodeConfig) => void;
  onClose: () => void;
  validationErrors?: string[];
}

interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
}

export default function NodePropertyPanel({
  nodeId, nodeType, displayName, config, agentId, onSave, onClose, validationErrors,
}: Props) {
  const [form, setForm] = useState<NodeConfig>({ ...config });
  const [highlightKeys, setHighlightKeys] = useState<string[]>([]);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setForm({ ...config });
  }, [config, nodeId]);

  useEffect(() => () => {
    if (highlightTimer.current) clearTimeout(highlightTimer.current);
  }, []);

  const flashHighlight = useCallback((keys: string[]) => {
    if (keys.length === 0) return;
    setHighlightKeys(keys);
    if (highlightTimer.current) clearTimeout(highlightTimer.current);
    highlightTimer.current = setTimeout(() => setHighlightKeys([]), 1500);
  }, []);

  // Server auto-committed (or user applied a proposal): reflect new values in
  // the form and flash the changed fields. The graph is already persisted, so
  // we update local state only — no extra POST.
  const handleConfigCommitted = useCallback((newConfig: NodeConfig, changedKeys: string[]) => {
    setForm((prev) => ({ ...prev, ...newConfig }));
    flashHighlight(changedKeys);
  }, [flashHighlight]);

  const handleChange = (key: string, value: string | number | boolean) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = () => {
    onSave(nodeId, form);
    onClose();
  };

  const panelWidth = (nodeType === "p" || nodeType === "agent") ? "w-[520px]" : "w-96";

  return (
    <div className={`fixed right-0 top-0 h-full ${panelWidth} bg-card border-l border-border shadow-xl z-50 flex flex-col`}>
      <div className="flex items-center justify-between p-4 border-b border-border">
        <div>
          <h3 className="font-semibold">{displayName}</h3>
          <p className="text-xs text-muted-foreground">节点 ID: {nodeId}</p>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose}>
          <X className="w-4 h-4" />
        </Button>
      </div>

      <Tabs defaultValue={0} className="flex-1 flex flex-col min-h-0">
        <TabsList className="mx-4 mt-2">
          <TabsTrigger value={0}>对话配置</TabsTrigger>
          <TabsTrigger value={1}>能力配置</TabsTrigger>
        </TabsList>

        <TabsContent value={0} className="flex-1 flex flex-col min-h-0">
          <ChatConfigTab
            nodeId={nodeId}
            agentId={agentId}
            currentConfig={form}
            onConfigCommitted={handleConfigCommitted}
          />
        </TabsContent>

        <TabsContent value={1} className="flex-1 flex flex-col min-h-0">
          {nodeType === "p" && (
            <PromptCapabilityEditor
              agentId={agentId}
              config={form}
              onChange={setForm}
              onSave={handleSave}
              highlightKeys={highlightKeys}
            />
          )}
          {nodeType === "m" && (
            <ModelCapabilityPicker
              config={form}
              onChange={setForm}
              onSave={handleSave}
            />
          )}
          {nodeType === "t" && (
            <ToolCapabilityPicker
              agentId={agentId}
              config={form}
              onChange={setForm}
              onSave={handleSave}
            />
          )}
          {nodeType === "agent" && (
            <AgentNodeEditor
              agentId={agentId}
              config={form}
              onChange={setForm}
              onSave={handleSave}
            />
          )}
          {!["p", "m", "t", "agent"].includes(nodeType) && (
            <GenericConfigForm
              form={form}
              validationErrors={validationErrors}
              highlightKeys={highlightKeys}
              onChange={handleChange}
              onSave={handleSave}
              onClose={onClose}
            />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

/* ── Generic Config Form (for K, C, X, H, A nodes) ── */
function GenericConfigForm({
  form, validationErrors, highlightKeys, onChange, onSave, onClose,
}: {
  form: NodeConfig;
  validationErrors?: string[];
  highlightKeys?: string[];
  onChange: (key: string, value: string | number | boolean) => void;
  onSave: () => void;
  onClose: () => void;
}) {
  const ringFor = (key: string) =>
    highlightKeys?.includes(key) ? "ring-2 ring-primary/40 rounded-md transition-shadow" : "transition-shadow";
  return (
    <>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {validationErrors && validationErrors.length > 0 && (
          <div className="p-3 rounded-md bg-destructive/10 border border-destructive/20">
            {validationErrors.map((err, i) => (
              <p key={i} className="text-xs text-destructive">{err}</p>
            ))}
          </div>
        )}
        {Object.entries(form).map(([key, value]) => {
          if (key.startsWith("_")) return null;
          if (typeof value === "boolean") {
            return (
              <div key={key} className={`space-y-1.5 ${ringFor(key)}`}>
                <Label className="text-xs">{key}</Label>
                <Select value={value ? "true" : "false"} onValueChange={(v) => onChange(key, v === "true")}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="true">是</SelectItem>
                    <SelectItem value="false">否</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            );
          }
          if (typeof value === "number") {
            return (
              <div key={key} className={`space-y-1.5 ${ringFor(key)}`}>
                <Label className="text-xs">{key}</Label>
                <Input type="number" value={value} onChange={(e) => onChange(key, Number(e.target.value))} />
              </div>
            );
          }
          const isLong = typeof value === "string" && value.length > 80;
          return (
            <div key={key} className={`space-y-1.5 ${ringFor(key)}`}>
              <Label className="text-xs">{key}</Label>
              {isLong ? (
                <Textarea value={value as string} onChange={(e) => onChange(key, e.target.value)} rows={6} />
              ) : (
                <Input value={value as string} onChange={(e) => onChange(key, e.target.value)} />
              )}
            </div>
          );
        })}
      </div>
      <div className="p-4 border-t border-border flex gap-2 shrink-0">
        <Button variant="outline" className="flex-1" onClick={onClose}>取消</Button>
        <Button className="flex-1" onClick={onSave}>保存</Button>
      </div>
    </>
  );
}

/* ── Chat Config Tab ── */
function ChatConfigTab({
  nodeId,
  agentId,
  currentConfig,
  onConfigCommitted,
}: {
  nodeId: string;
  agentId?: number;
  currentConfig: NodeConfig;
  onConfigCommitted: (newConfig: NodeConfig, changedKeys: string[]) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState("");
  const [thinking, setThinking] = useState(false);
  const [autoCommit, setAutoCommit] = useState(true);
  const [proposed, setProposed] = useState<{ partial: NodeConfig; changedKeys: string[] } | null>(null);
  const [committedNote, setCommittedNote] = useState<{ keys: string[]; version: number } | null>(null);
  const [applying, setApplying] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const msgIdRef = useRef(0);
  const streamRef = useRef("");
  const scrollRef = useRef<HTMLDivElement>(null);
  // Refs the WS message handler reads, kept current so `connect` stays
  // dependency-stable (reconnecting would reset server-side pending_config).
  const autoCommitRef = useRef(autoCommit);
  useEffect(() => { autoCommitRef.current = autoCommit; }, [autoCommit]);
  const streamRefLast = useRef("");

  const connect = useCallback(() => {
    if (!agentId) return;
    setError(null);
    const url = `${getWsBase()}/api/node-config/ws/${agentId}/${nodeId}`;
    const ws = new WebSocket(url);

    ws.onopen = () => {
      setConnected(true);
      setError(null);
      // Sync the current toggle state to the fresh connection.
      ws.send(JSON.stringify({ type: "set_auto_commit", value: autoCommitRef.current }));
    };
    ws.onclose = () => setConnected(false);
    ws.onerror = () => { setConnected(false); setError("连接失败，请检查后端服务是否运行"); };

    ws.onmessage = (event) => {
      let data: any;
      try {
        data = JSON.parse(event.data);
      } catch {
        setError("收到无效响应");
        return;
      }
      if (data.type === "ready") {
        // Connection established
      } else if (data.type === "thinking") {
        setThinking(true);
      } else if (data.type === "token" && data.content) {
        setThinking(false);
        streamRef.current += data.content;
        setStreaming(streamRef.current);
      } else if (data.type === "done") {
        setThinking(false);
        const content = streamRef.current;
        streamRef.current = "";
        streamRefLast.current = content;
        setStreaming("");
        if (content.trim()) {
          msgIdRef.current++;
          setMessages((prev) => [...prev, { id: msgIdRef.current, role: "assistant", content }]);
        }
      } else if (data.type === "node_config_committed") {
        // Server already wrote the graph. Pull the freshly committed values out
        // of the assistant reply we just rendered and reflect them in the form.
        const changedKeys: string[] = data.changed_keys || [];
        const fromReply = extractJsonConfig(streamRefLast.current) || {};
        const committed: NodeConfig = {};
        for (const k of changedKeys) {
          if (k in fromReply) committed[k] = fromReply[k] as string | number | boolean;
        }
        onConfigCommitted(committed, changedKeys);
        setProposed(null);
        setCommittedNote({ keys: changedKeys, version: data.version });
      } else if (data.type === "node_config_proposed") {
        setProposed({ partial: data.partial || {}, changedKeys: data.changed_keys || [] });
        setCommittedNote(null);
      } else if (data.type === "error") {
        setThinking(false);
        streamRef.current = "";
        setStreaming("");
        setError(data.message || "配置对话出错");
        toast.error(data.message || "配置对话出错");
      }
    };

    wsRef.current = ws;
  // autoCommit/onConfigCommitted are read through refs to keep the socket stable.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, nodeId]);

  useEffect(() => {
    connect();
    return () => { wsRef.current?.close(); };
  }, [connect]);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, streaming]);

  const toggleAutoCommit = (next: boolean) => {
    setAutoCommit(next);
    if (next) setProposed(null);
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "set_auto_commit", value: next }));
    }
  };

  const handleApplyProposed = async () => {
    if (!proposed || !agentId) return;
    setApplying(true);
    try {
      const latest = await api.getDAGGraph(agentId);
      const graph = JSON.parse(latest.graph_json);
      const node = (graph.nodes || []).find((n: any) => n.id === nodeId);
      if (!node) throw new Error("节点不存在");
      node.config = { ...(node.config || {}), ...proposed.partial };
      await api.saveDAGGraph(agentId, JSON.stringify(graph), latest.state_schema);
      onConfigCommitted(proposed.partial, proposed.changedKeys);
      setCommittedNote({ keys: proposed.changedKeys, version: latest.version + 1 });
      setProposed(null);
    } catch {
      toast.error("应用配置失败");
    }
    setApplying(false);
  };

  const handleSend = () => {
    if (!input.trim() || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: "message", content: input }));
    msgIdRef.current++;
    setMessages((prev) => [...prev, { id: msgIdRef.current, role: "user", content: input }]);
    setInput("");
    setProposed(null);
    setCommittedNote(null);
    setError(null);
  };

  const handleRetry = () => {
    wsRef.current?.close();
    setError(null);
    connect();
  };

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Auto-apply toggle */}
      <div className="flex items-center justify-between mx-3 mt-2 px-2 py-1.5 rounded-md bg-muted/40 border border-border">
        <span className="text-xs text-muted-foreground">自动应用</span>
        <button
          type="button"
          role="switch"
          aria-checked={autoCommit}
          aria-label="自动应用"
          onClick={() => toggleAutoCommit(!autoCommit)}
          className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors ${
            autoCommit ? "bg-primary" : "bg-input"
          }`}
        >
          <span
            className={`inline-block h-3 w-3 transform rounded-full bg-background transition-transform ${
              autoCommit ? "translate-x-3.5" : "translate-x-0.5"
            }`}
          />
        </button>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mx-3 mt-2 p-2 rounded-md bg-destructive/10 border border-destructive/20 flex items-center justify-between">
          <span className="text-xs text-destructive">{error}</span>
          <Button variant="ghost" size="sm" className="h-5 text-xs px-1.5" onClick={handleRetry}>重试</Button>
        </div>
      )}

      {/* Disconnected banner */}
      {!connected && !error && (
        <div className="mx-3 mt-2 p-2 rounded-md bg-muted border border-border flex items-center justify-between">
          <span className="text-xs text-muted-foreground">未连接</span>
          <Button variant="ghost" size="sm" className="h-5 text-xs px-1.5" onClick={handleRetry}>重连</Button>
        </div>
      )}

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.length === 0 && !streaming && !thinking && (
          <p className="text-xs text-muted-foreground text-center mt-6">
            用自然语言描述你想要的节点行为，AI 会帮你生成配置
          </p>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[85%] rounded-lg px-2.5 py-1.5 text-sm ${
              m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted"
            }`}>
              <div className="whitespace-pre-wrap break-words">{m.content}</div>
            </div>
          </div>
        ))}
        {thinking && (
          <div className="flex justify-start">
            <div className="rounded-lg px-2.5 py-1.5 bg-muted">
              <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
            </div>
          </div>
        )}
        {streaming && (
          <div className="flex justify-start">
            <div className="max-w-[85%] rounded-lg px-2.5 py-1.5 text-sm bg-muted">
              <div className="whitespace-pre-wrap break-words">{streaming}</div>
            </div>
          </div>
        )}
      </div>

      {/* Committed note (auto-apply path) */}
      {committedNote && (
        <div className="mx-3 mb-2 p-2 rounded-md border border-primary/30 bg-primary/5">
          <p className="text-xs text-primary">
            已自动应用到节点配置 (v{committedNote.version})：{committedNote.keys.join(", ") || "无变更"}
          </p>
        </div>
      )}

      {/* Proposed diff card (manual-apply path) */}
      {proposed && (
        <div className="mx-3 mb-2 p-3 rounded-lg border border-primary/30 bg-primary/5">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium">建议配置变更</span>
            <div className="flex gap-1.5">
              <Button
                size="sm"
                className="h-6 text-xs px-2"
                disabled={applying}
                onClick={handleApplyProposed}
              >
                {applying ? "应用中..." : "应用"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-6 text-xs px-2"
                onClick={() => setProposed(null)}
              >
                忽略
              </Button>
            </div>
          </div>
          <div className="space-y-1">
            {proposed.changedKeys.length === 0 && (
              <p className="text-xs text-muted-foreground">与当前配置一致，无变更</p>
            )}
            {proposed.changedKeys.map((key) => (
              <div key={key} className="flex items-center gap-2 text-xs">
                <span className="font-mono text-muted-foreground shrink-0">{key}</span>
                <span className="text-muted-foreground line-through truncate max-w-[40%]">
                  {formatValue(currentConfig[key])}
                </span>
                <span className="text-muted-foreground">→</span>
                <span className="text-foreground truncate max-w-[40%]">
                  {formatValue(proposed.partial[key])}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Input */}
      <div className="flex items-center gap-2 px-3 py-2 border-t border-border shrink-0">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
          placeholder="描述你想要的节点行为..."
          className="flex-1 text-sm bg-transparent border border-border rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-ring"
          disabled={!connected}
        />
        <Button size="sm" className="h-7 px-2" onClick={handleSend} disabled={!connected || !input.trim()}>
          发送
        </Button>
      </div>
    </div>
  );
}

function extractJsonConfig(text: string): NodeConfig | null {
  const match = text.match(/```json\s*\n([\s\S]*?)\n```/);
  if (!match) return null;
  try {
    return JSON.parse(match[1]);
  } catch {
    return null;
  }
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "(空)";
  if (typeof value === "string") return value || "(空)";
  return String(value);
}
