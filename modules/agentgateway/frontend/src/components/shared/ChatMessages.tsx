"use client";
import { useEffect, useRef } from "react";
import type { Message } from "@/lib/types";
import { Loader2 } from "lucide-react";

interface Props {
  messages: Message[];
  streamingContent?: string;
  thinking?: boolean;
}

export function ChatMessages({ messages, streamingContent, thinking }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingContent, thinking]);

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-4">
      {messages.map((msg) => (
        <div
          key={msg.id}
          className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
        >
          <div
            className={`max-w-[80%] rounded-lg px-4 py-2 ${
              msg.role === "user"
                ? "bg-primary text-primary-foreground"
                : "bg-muted"
            }`}
          >
            <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
          </div>
        </div>
      ))}
      {thinking && !streamingContent && (
        <div className="flex justify-start">
          <div className="max-w-[80%] rounded-lg px-4 py-2 bg-muted">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin" />
              智能体正在思考...
            </div>
          </div>
        </div>
      )}
      {streamingContent && (
        <div className="flex justify-start">
          <div className="max-w-[80%] rounded-lg px-4 py-2 bg-muted">
            <p className="text-sm whitespace-pre-wrap">
              {streamingContent}
              <span className="inline-block w-2 h-4 bg-foreground ml-1 animate-pulse" />
            </p>
          </div>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}