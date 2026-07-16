"use client";
import { useState, useRef, useEffect } from "react";
import type { Message, PromptVersion } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ChatInput } from "@/components/shared/ChatInput";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Loader2 } from "lucide-react";
import { createChatSocket } from "@/lib/ws";
import { api } from "@/lib/api";
import { toast } from "sonner";

interface PanelState {
  messages: Message[];
  streamingContent: string;
  thinking: boolean;
  status: "connected" | "disconnected" | "reconnecting";
  responseTime: number | null;
}

interface CompareChatViewProps {
  agentId: number;
  versions: PromptVersion[];
  currentPrompt: string;
  onClose: () => void;
}

export default function CompareChatView({ agentId, versions, currentPrompt, onClose }: CompareChatViewProps) {
  const [leftVersionId, setLeftVersionId] = useState<string>("current");
  const [rightVersionId, setRightVersionId] = useState<string>(
    versions.length > 0 ? String(versions[0].id) : "current"
  );
  const [left, setLeft] = useState<PanelState>({
    messages: [], streamingContent: "", thinking: false, status: "disconnected", responseTime: null,
  });
  const [right, setRight] = useState<PanelState>({
    messages: [], streamingContent: "", thinking: false, status: "disconnected", responseTime: null,
  });
  const [sending, setSending] = useState(false);
  const [bothDone, setBothDone] = useState(false);

  const leftSocketRef = useRef<ReturnType<typeof createChatSocket> | null>(null);
  const rightSocketRef = useRef<ReturnType<typeof createChatSocket> | null>(null);
  const leftStartRef = useRef<number>(0);
  const rightStartRef = useRef<number>(0);
  const leftMsgIdRef = useRef(0);
  const rightMsgIdRef = useRef(0);
  const leftStreamRef = useRef("");
  const rightStreamRef = useRef("");

  const getPromptForVersion = async (versionId: string): Promise<string | undefined> => {
    if (versionId === "current") return undefined;
    try {
      const v = await api.getPromptVersion(agentId, Number(versionId));
      const cfg = JSON.parse(v.prompt_config);
      return cfg.system_prompt;
    } catch {
      return undefined;
    }
  };

  // Set up sockets
  useEffect(() => {
    const leftSocket = createChatSocket(agentId);
    const rightSocket = createChatSocket(agentId);

    leftSocket.onStatus((s) => setLeft((prev) => ({ ...prev, status: s })));
    rightSocket.onStatus((s) => setRight((prev) => ({ ...prev, status: s })));

    const makeHandler = (panel: "left" | "right") => (data: { type: string; content?: string; message?: string }) => {
      const setter = panel === "left" ? setLeft : setRight;
      const streamRef = panel === "left" ? leftStreamRef : rightStreamRef;
      const msgIdRef = panel === "left" ? leftMsgIdRef : rightMsgIdRef;
      const startRef = panel === "left" ? leftStartRef : rightStartRef;

      if (data.type === "thinking") {
        setter((prev) => ({ ...prev, thinking: true }));
        startRef.current = Date.now();
      } else if (data.type === "token" && data.content) {
        setter((prev) => ({ ...prev, thinking: false }));
        streamRef.current += data.content;
        setter((prev) => ({ ...prev, streamingContent: streamRef.current }));
      } else if (data.type === "done") {
        setter((prev) => ({
          ...prev,
          thinking: false,
          streamingContent: "",
          responseTime: Date.now() - startRef.current,
          messages: [
            ...prev.messages,
            {
              id: msgIdRef.current + 1,
              conversation_id: 0,
              role: "assistant" as const,
              content: streamRef.current,
              created_at: new Date().toISOString(),
            },
          ],
        }));
        msgIdRef.current += 1;
        streamRef.current = "";
      } else if (data.type === "error") {
        setter((prev) => ({ ...prev, thinking: false, streamingContent: "" }));
        toast.error(data.message || "对话出错");
      }
    };

    leftSocket.onMessage(makeHandler("left"));
    rightSocket.onMessage(makeHandler("right"));

    leftSocket.connect();
    rightSocket.connect();

    leftSocketRef.current = leftSocket;
    rightSocketRef.current = rightSocket;

    return () => {
      leftSocket.disconnect();
      rightSocket.disconnect();
    };
  }, [agentId]);

  const handleSend = async (content: string) => {
    setSending(true);
    setBothDone(false);

    const leftPrompt = await getPromptForVersion(leftVersionId);
    const rightPrompt = await getPromptForVersion(rightVersionId);

    const leftUserMsg = { id: leftMsgIdRef.current + 1, conversation_id: 0, role: "user" as const, content, created_at: new Date().toISOString() };
    const rightUserMsg = { id: rightMsgIdRef.current + 1, conversation_id: 0, role: "user" as const, content, created_at: new Date().toISOString() };
    leftMsgIdRef.current += 1;
    rightMsgIdRef.current += 1;

    setLeft((prev) => ({ ...prev, messages: [...prev.messages, leftUserMsg], responseTime: null }));
    setRight((prev) => ({ ...prev, messages: [...prev.messages, rightUserMsg], responseTime: null }));

    if (leftPrompt) {
      leftSocketRef.current?.send(content, { system_prompt: leftPrompt });
    } else {
      leftSocketRef.current?.send(content);
    }

    if (rightPrompt) {
      rightSocketRef.current?.send(content, { system_prompt: rightPrompt });
    } else {
      rightSocketRef.current?.send(content);
    }

    // Check when both done (simple polling approach)
    setTimeout(() => setBothDone(true), 5000);
    setSending(false);
  };

  const Panel = ({ state, label, versionId, onVersionChange, color }: {
    state: PanelState;
    label: string;
    versionId: string;
    onVersionChange: (v: string | null) => void;
    color: "blue" | "green";
  }) => (
    <div className="flex-1 flex flex-col border rounded-lg overflow-hidden">
      <div className="flex items-center justify-between p-2 border-b bg-muted/30">
        <div className="flex items-center gap-2">
          <Badge variant={color === "blue" ? "default" : "secondary"}>{label}</Badge>
          {state.status === "connected" ? (
            <span className="w-2 h-2 rounded-full bg-green-500" title="已连接" />
          ) : (
            <span className="w-2 h-2 rounded-full bg-red-500" title="已断开" />
          )}
        </div>
        <Select value={versionId} onValueChange={onVersionChange}>
          <SelectTrigger className="h-7 text-xs w-[160px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="current">当前版本</SelectItem>
            {versions.map((v) => (
              <SelectItem key={v.id} value={String(v.id)}>
                v{v.version_number}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <ScrollArea className="flex-1 p-3">
        <div className="space-y-3">
          {state.messages.map((m) => (
            <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[90%] rounded-lg px-3 py-1.5 text-sm ${
                m.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted"
              }`}>
                <div className="whitespace-pre-wrap">{m.content}</div>
              </div>
            </div>
          ))}
          {state.thinking && !state.streamingContent && (
            <div className="flex justify-start">
              <div className="max-w-[90%] rounded-lg px-3 py-1.5 bg-muted">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  思考中...
                </div>
              </div>
            </div>
          )}
          {state.streamingContent && (
            <div className="flex justify-start">
              <div className="max-w-[90%] rounded-lg px-3 py-1.5 text-sm bg-muted">
                <div className="whitespace-pre-wrap">{state.streamingContent}</div>
              </div>
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );

  // Comparison summary
  const leftLastContent = left.messages.filter((m) => m.role === "assistant").pop()?.content || "";
  const rightLastContent = right.messages.filter((m) => m.role === "assistant").pop()?.content || "";
  const leftWords = leftLastContent.length;
  const rightWords = rightLastContent.length;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between p-2 border-b shrink-0">
        <h3 className="text-sm font-medium">版本对比</h3>
        <Button variant="ghost" size="sm" onClick={onClose}>关闭</Button>
      </div>
      <div className="flex-1 flex gap-3 p-3 min-h-0">
        <Panel
          state={left}
          label="A"
          versionId={leftVersionId}
          onVersionChange={(v) => setLeftVersionId(v || "current")}
          color="blue"
        />
        <Panel
          state={right}
          label="B"
          versionId={rightVersionId}
          onVersionChange={(v) => setRightVersionId(v || "current")}
          color="green"
        />
      </div>
      <div className="shrink-0 border-t">
        {/* Comparison summary */}
        {bothDone && leftLastContent && rightLastContent && (
          <div className="px-3 py-2 border-b bg-muted/20">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <div className="flex items-center gap-4">
                <span>A: {leftWords} 字</span>
                <span>B: {rightWords} 字</span>
              </div>
              <div className="flex items-center gap-4">
                {left.responseTime != null && <span>A 响应: {(left.responseTime / 1000).toFixed(1)}s</span>}
                {right.responseTime != null && <span>B 响应: {(right.responseTime / 1000).toFixed(1)}s</span>}
              </div>
              <Badge variant="outline">
                {leftWords > rightWords ? "A 更长" : rightWords > leftWords ? "B 更长" : "长度相同"}
              </Badge>
            </div>
          </div>
        )}
        <ChatInput onSend={handleSend} disabled={sending} />
      </div>
    </div>
  );
}