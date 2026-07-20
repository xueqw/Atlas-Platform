"use client";
import { useState, useRef, useCallback, useEffect } from "react";
import { createChatSocket } from "@/lib/ws";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { MessageSquare, X, Loader2, ExternalLink } from "lucide-react";
import { toast } from "sonner";
import { useAtlasRuntimeExecution, type AtlasRuntimeEvent } from "@/lib/atlas-runtime";
import MultiAgentRuntimePanel from "@/components/workbench/MultiAgentRuntimePanel";

interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
}

interface NodeStatus {
  nodeId: string;
  nodeType: string;
  status: "running" | "complete" | "error";
}

interface GatewaySocketEvent {
  type?: string;
  content?: string;
  message?: string;
  node_id?: string;
  node_type?: string;
  error?: string;
  status?: string;
}

interface Props {
  agentId: number;
}

export default function TestChatPanel({ agentId }: Props) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState("");
  const [thinkingContent, setThinkingContent] = useState("");
  const [thinking, setThinking] = useState(false);
  const [wsStatus, setWsStatus] = useState<"connected" | "disconnected" | "reconnecting">("disconnected");
  const [nodeStatuses, setNodeStatuses] = useState<NodeStatus[]>([]);
  const [runTraceUrl, setRunTraceUrl] = useState<string>("");
  const [runtimeEvents, setRuntimeEvents] = useState<AtlasRuntimeEvent[]>([]);
  const socketRef = useRef<ReturnType<typeof createChatSocket> | null>(null);
  const msgIdRef = useRef(0);
  const streamRef = useRef("");
  const thinkRef = useRef("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const atlasRuntime = useAtlasRuntimeExecution(agentId, {
    onEvent: (event) => {
      setRuntimeEvents((previous) => [...previous, event].slice(-160));
      if (event.type === "node.started") {
        const nodeId = String(event.payload.node || event.payload.name || "runtime");
        setNodeStatuses((previous) => [...previous.filter((item) => item.nodeId !== nodeId), { nodeId, nodeType: "runtime", status: "running" }]);
      } else if (event.type === "node.completed") {
        const nodeId = String(event.payload.node || event.payload.name || "runtime");
        setNodeStatuses((previous) => previous.map((item) => item.nodeId === nodeId ? { ...item, status: "complete" } : item));
      } else if (event.type === "token.delta" || event.type === "response.token") {
        const token = typeof event.payload.token === "string" ? event.payload.token : typeof event.payload.content === "string" ? event.payload.content : "";
        streamRef.current += token;
        setStreaming(streamRef.current);
        setThinking(false);
      } else if (event.type === "run.completed") {
        const content = typeof event.payload.output === "string" ? event.payload.output : streamRef.current;
        streamRef.current = "";
        setStreaming("");
        setThinking(false);
        msgIdRef.current += 1;
        setMessages((previous) => [...previous, { id: msgIdRef.current, role: "assistant", content }]);
        setNodeStatuses([]);
      } else if (event.type === "run.failed") {
        setThinking(false);
        toast.error(String(event.payload.message || event.payload.error || "Atlas Runtime 执行失败"));
      }
    },
    onError: (message) => {
      setThinking(false);
      toast.error(message);
    },
  });

  const connect = useCallback(() => {
    socketRef.current?.disconnect();
    setMessages([]);
    setStreaming("");
    setNodeStatuses([]);
    setRunTraceUrl("");
    setRuntimeEvents([]);
    streamRef.current = "";

    if (atlasRuntime.embedded) {
      setWsStatus(atlasRuntime.mappingError ? "disconnected" : "connected");
      if (atlasRuntime.mappingError) toast.error(atlasRuntime.mappingError);
      return;
    }

    const socket = createChatSocket(agentId);
    socket.onMessage((data: GatewaySocketEvent) => {
      if (data.type === "thinking") {
        setThinking(true);
      } else if (data.type === "thinking_content" && data.content) {
        setThinking(true);
        thinkRef.current += data.content;
        setThinkingContent(thinkRef.current);
      } else if (data.type === "token" && data.content) {
        setThinking(false);
        streamRef.current += data.content;
        setStreaming(streamRef.current);
      } else if (data.type === "done") {
        setThinking(false);
        const content = streamRef.current;
        const thinkText = thinkRef.current;
        streamRef.current = "";
        thinkRef.current = "";
        setStreaming("");
        setThinkingContent("");
        msgIdRef.current++;
        const fullContent = thinkText ? `<think>${thinkText}</think>\n${content}` : content;
        setMessages((prev) => [...prev, { id: msgIdRef.current, role: "assistant", content: fullContent }]);
        setNodeStatuses([]);
        // The run summary row is written at run end; fetch the latest trace link.
        api.getMonitoringTraces(agentId)
          .then((r) => {
            const latest = r.traces?.[0];
            setRunTraceUrl(latest?.trace_url || "");
          })
          .catch(() => {});
      } else if (data.type === "error") {
        setThinking(false);
        streamRef.current = "";
        thinkRef.current = "";
        setStreaming("");
        setThinkingContent("");
        toast.error(data.message || "对话出错");
      } else if (data.type === "node_start") {
        const nodeId = data.node_id || "runtime";
        const nodeType = data.node_type || "runtime";
        setNodeStatuses((prev) => [
          ...prev.filter((n) => n.nodeId !== nodeId),
          { nodeId, nodeType, status: "running" },
        ]);
      } else if (data.type === "node_complete") {
        setNodeStatuses((prev) =>
          prev.map((n) => n.nodeId === data.node_id ? { ...n, status: "complete" } : n)
        );
      } else if (data.type === "node_error") {
        // A node threw mid-run (e.g. the model gateway has no channel for the
        // configured model). Surface it instead of silently leaving the panel
        // spinning — clear the in-flight loading state and toast the reason.
        setThinking(false);
        streamRef.current = "";
        thinkRef.current = "";
        setStreaming("");
        setThinkingContent("");
        setNodeStatuses((prev) =>
          prev.map((n) => n.nodeId === data.node_id ? { ...n, status: "error" } : n)
        );
        toast.error(
          data.error
            ? `节点 ${data.node_type || data.node_id} 执行失败：${data.error}`
            : `节点 ${data.node_type || data.node_id} 执行失败`
        );
      } else if (data.type === "node_status" && data.status === "skipped") {
        setNodeStatuses((prev) =>
          prev.map((n) => n.nodeId === data.node_id ? { ...n, status: "error" } : n)
        );
      }
    });
    socket.onStatus(setWsStatus);
    socket.connect();
    socketRef.current = socket;
  }, [agentId, atlasRuntime.embedded, atlasRuntime.mappingError]);

  useEffect(() => {
    return () => { socketRef.current?.disconnect(); };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, streaming]);

  const handleOpen = () => {
    setOpen(true);
    connect();
  };

  const handleClose = () => {
    socketRef.current?.disconnect();
    setOpen(false);
    setMessages([]);
    setStreaming("");
    setNodeStatuses([]);
    setRuntimeEvents([]);
  };

  const handleSend = () => {
    if (!input.trim() || wsStatus !== "connected") return;
    msgIdRef.current++;
    setMessages((prev) => [...prev, { id: msgIdRef.current, role: "user", content: input }]);
    if (atlasRuntime.embedded) {
      setThinking(true);
      setRuntimeEvents([]);
      if (!atlasRuntime.start(input, [`agentgateway:agent:${agentId}`, "agentgateway:dag-test-chat"])) setThinking(false);
    } else {
      socketRef.current?.send(input);
    }
    setInput("");
  };

  if (!open) {
    return (
      <button
        onClick={handleOpen}
        className="absolute right-4 bottom-4 z-40 flex items-center gap-2 px-3 py-2 rounded-lg bg-primary text-primary-foreground shadow-lg hover:opacity-90 transition-opacity"
      >
        <MessageSquare className="w-4 h-4" />
        <span className="text-sm">测试对话</span>
      </button>
    );
  }

  return (
    <div className="absolute right-0 top-0 bottom-0 w-[420px] max-w-[92%] z-40 bg-card border-l border-border flex flex-col shadow-xl">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">测试对话</span>
          <Badge variant={wsStatus === "connected" ? "default" : "destructive"} className="text-xs px-1.5 py-0">
            {atlasRuntime.embedded && wsStatus === "connected" ? "Atlas Runtime" : wsStatus === "connected" ? "已连接" : "断开"}
          </Badge>
        </div>
        <Button variant="ghost" size="icon" className="h-6 w-6" onClick={handleClose}>
          <X className="w-3.5 h-3.5" />
        </Button>
      </div>

      {/* Node execution status */}
      {nodeStatuses.length > 0 && (
        <div className="flex items-center gap-1 px-3 py-1.5 border-b border-border bg-muted/50 text-xs overflow-x-auto shrink-0">
          {nodeStatuses.map((ns) => (
            <span
              key={ns.nodeId}
              className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded ${
                ns.status === "running" ? "bg-blue-100 text-blue-700" :
                ns.status === "complete" ? "bg-green-100 text-green-700" :
                "bg-red-100 text-red-700"
              }`}
            >
              {ns.status === "running" && <Loader2 className="w-2.5 h-2.5 animate-spin" />}
              {ns.nodeType}:{ns.nodeId}
            </span>
          ))}
        </div>
      )}

      {/* Messages */}
      {atlasRuntime.embedded && (
        <MultiAgentRuntimePanel events={runtimeEvents} runId={atlasRuntime.runId} running={atlasRuntime.running} />
      )}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.length === 0 && !streaming && !thinking && !thinkingContent && (
          <p className="text-xs text-muted-foreground text-center mt-8">发送消息开始测试当前 DAG</p>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[85%] rounded-lg px-2.5 py-1.5 text-sm ${
              m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted"
            }`}>
              <MessageContent content={m.content} />
            </div>
          </div>
        ))}
        {thinkingContent && (
          <div className="flex justify-start">
            <div className="max-w-[85%] rounded-lg px-2.5 py-1.5 text-xs bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800">
              <details open>
                <summary className="cursor-pointer text-amber-700 dark:text-amber-300 font-medium">思考中...</summary>
                <div className="mt-1 whitespace-pre-wrap break-words text-amber-600 dark:text-amber-400 max-h-40 overflow-y-auto">{thinkingContent}</div>
              </details>
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
        {thinking && !thinkingContent && !streaming && (
          <div className="flex justify-start">
            <div className="rounded-lg px-2.5 py-1.5 bg-muted">
              <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="flex flex-col gap-1 px-3 py-2 border-t border-border shrink-0">
        {atlasRuntime.pendingInterrupt && (
          <div className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
            <p className="mb-2 font-medium">工具写操作等待授权</p>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => atlasRuntime.resolveInterrupt("approve")}>授权</Button>
              <Button size="sm" variant="destructive" onClick={() => atlasRuntime.resolveInterrupt("deny")}>拒绝</Button>
            </div>
          </div>
        )}
        {runTraceUrl && (
          <a
            href={runTraceUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs text-blue-500 hover:underline self-start"
          >
            <ExternalLink className="w-3 h-3" /> 本次运行 trace ↗
          </a>
        )}
        <div className="flex items-center gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
            placeholder="输入消息..."
            className="flex-1 text-sm bg-transparent border border-border rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-ring"
            disabled={wsStatus !== "connected" || atlasRuntime.running || Boolean(atlasRuntime.pendingInterrupt)}
          />
          <Button size="sm" className="h-7 px-2" onClick={atlasRuntime.running ? atlasRuntime.cancel : handleSend} disabled={wsStatus !== "connected" || (!atlasRuntime.running && !input.trim())}>
            {atlasRuntime.running ? "取消" : "发送"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function MessageContent({ content }: { content: string }) {
  const thinkMatch = content.match(/^<think>([\s\S]*?)<\/think>\s*([\s\S]*)$/);
  if (thinkMatch) {
    const thinkText = thinkMatch[1].trim();
    const responseText = thinkMatch[2].trim();
    return (
      <div>
        {thinkText && (
          <details className="mb-1.5">
            <summary className="cursor-pointer text-xs text-amber-600 dark:text-amber-400 font-medium">💭 思考过程</summary>
            <div className="mt-1 text-xs text-muted-foreground whitespace-pre-wrap break-words max-h-32 overflow-y-auto border-l-2 border-amber-300 pl-2">{thinkText}</div>
          </details>
        )}
        <div className="whitespace-pre-wrap break-words">{responseText}</div>
      </div>
    );
  }
  return <div className="whitespace-pre-wrap break-words">{content}</div>;
}
