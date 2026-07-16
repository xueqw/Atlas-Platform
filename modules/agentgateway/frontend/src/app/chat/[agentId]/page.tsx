"use client";
import { Suspense, useEffect, useState, useRef, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type { Agent, Message } from "@/lib/types";
import { createChatSocket } from "@/lib/ws";
import { ChatMessages } from "@/components/shared/ChatMessages";
import { ChatInput } from "@/components/shared/ChatInput";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

export default function AgentChatPage() {
  return (
    <Suspense fallback={<div className="flex-1 flex items-center justify-center text-muted-foreground">加载中...</div>}>
      <AgentChatPageInner />
    </Suspense>
  );
}

function AgentChatPageInner() {
  const params = useParams();
  const searchParams = useSearchParams();
  const agentId = Number(params.agentId);
  // History detail page passes ?conversation=<id>; new chats (from an agent
  // card) have none. When present we hydrate past messages and pin new turns
  // to the same conversation server-side.
  const conversationParam = searchParams.get("conversation");
  const conversationId = conversationParam ? Number(conversationParam) : null;

  const [agent, setAgent] = useState<Agent | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [streamingContent, setStreamingContent] = useState("");
  const [thinking, setThinking] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(conversationId != null);
  const [wsStatus, setWsStatus] = useState<"connected" | "disconnected" | "reconnecting">("disconnected");
  const socketRef = useRef<ReturnType<typeof createChatSocket> | null>(null);
  const msgIdRef = useRef(0);
  const streamingRef = useRef("");

  useEffect(() => {
    api.getAgent(agentId).then(setAgent).catch(() => toast.error("加载智能体失败"));
  }, [agentId]);

  const nextId = () => { msgIdRef.current += 1; return msgIdRef.current; };

  const connectChat = useCallback(() => {
    setStreamingContent("");
    streamingRef.current = "";

    const socket = createChatSocket(agentId);
    socket.onMessage((data) => {
      if (data.type === "thinking") {
        setThinking(true);
      } else if (data.type === "token" && data.content) {
        setThinking(false);
        streamingRef.current += data.content;
        setStreamingContent(streamingRef.current);
      } else if (data.type === "done") {
        setThinking(false);
        const content = streamingRef.current;
        streamingRef.current = "";
        setStreamingContent("");
        setMessages((msgs) => [
          ...msgs,
          { id: nextId(), conversation_id: conversationId ?? 0, role: "assistant", content, created_at: new Date().toISOString() },
        ]);
      } else if (data.type === "error") {
        setThinking(false);
        toast.error(data.message || "对话出错");
        streamingRef.current = "";
        setStreamingContent("");
      }
    });
    socket.onStatus(setWsStatus);
    socket.connect();
    socketRef.current = socket;

    return () => socket.disconnect();
  }, [agentId, conversationId]);

  // Hydrate history once when arriving from the conversation list, before
  // wiring the socket. New-chat entries (no conversationId) skip straight to
  // the live socket with an empty thread.
  useEffect(() => {
    if (conversationId == null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset thread for the new-chat path (no history to fetch); driven by conversationId change
      setMessages([]);
      setHistoryLoading(false);
      return;
    }
    let cancelled = false;
    setHistoryLoading(true);
    api.getMessages(conversationId)
      .then((rows) => {
        if (cancelled) return;
        msgIdRef.current = rows.length;
        setMessages(rows);
      })
      .catch(() => { if (!cancelled) toast.error("加载历史消息失败"); })
      .finally(() => { if (!cancelled) setHistoryLoading(false); });
    return () => { cancelled = true; };
  }, [conversationId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- connectChat opens the socket; history is seeded by the hydrate effect above
    const cleanup = connectChat();
    return () => cleanup?.();
  }, [connectChat]);

  const sendMessage = (content: string) => {
    setMessages((prev) => [
      ...prev,
      { id: nextId(), conversation_id: conversationId ?? 0, role: "user", content, created_at: new Date().toISOString() },
    ]);
    // Pin the turn to the resumed conversation so history + new messages stay
    // in one thread; omitted for fresh chats so the server opens a new one.
    socketRef.current?.send(content, conversationId != null ? { conversation_id: conversationId } : undefined);
  };

  return (
    <div className="flex-1 flex flex-col max-w-4xl mx-auto w-full p-6">
      {/* Header */}
      <div className="flex items-center gap-4 mb-4">
        <Link href="/chat">
          <Button variant="ghost" size="icon">
            <ArrowLeft className="w-4 h-4" />
          </Button>
        </Link>
        <h1 className="text-xl font-bold">{agent?.name || "对话"}</h1>
        <Badge variant={wsStatus === "connected" ? "default" : "destructive"}>
          {wsStatus === "connected" ? "已连接" : wsStatus === "reconnecting" ? "重连中..." : "已断开"}
        </Badge>
      </div>

      {/* Agent info */}
      {agent && (
        <div className="text-xs text-muted-foreground mb-3">
          {agent.prompt_config?.role_name} · {agent.model?.model_name}
        </div>
      )}

      {/* Chat */}
      {historyLoading ? (
        <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">加载历史消息...</div>
      ) : (
        <ChatMessages messages={messages} streamingContent={streamingContent} thinking={thinking} />
      )}
      <ChatInput onSend={sendMessage} disabled={wsStatus !== "connected"} />
    </div>
  );
}