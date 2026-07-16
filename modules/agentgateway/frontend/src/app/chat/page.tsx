"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Agent, Conversation } from "@/lib/types";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";

export default function ChatPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);

  const load = () => {
    Promise.all([
      api.listAgents(),
      api.listConversations(),
    ]).then(([a, c]) => {
      // The chat surface doubles as the local test entry for newly planned
      // agents; show drafts/testing agents too so users can exercise them before
      // the release gate publishes them.
      setAgents(a.filter((x) => x.status !== "deprecated"));
      setConversations(c);
      setLoading(false);
    });
  };

  useEffect(() => { load(); }, []);

  const handleDeleteConv = async (id: number) => {
    try {
      await api.deleteConversation(id);
      setConversations((prev) => prev.filter((c) => c.id !== id));
      toast.success("会话已删除");
    } catch {
      toast.error("删除失败");
    }
  };

  if (loading) {
    return <div className="flex-1 flex items-center justify-center text-muted-foreground">加载中...</div>;
  }

  return (
    <div className="flex-1 flex max-w-5xl mx-auto w-full p-6 gap-6">
      {/* Conversations sidebar */}
      <div className="w-72 flex-shrink-0">
        <h2 className="text-lg font-semibold mb-3">会话列表</h2>
        <ScrollArea className="h-[calc(100vh-140px)]">
          <div className="space-y-2 pr-3">
            {conversations.length === 0 && (
              <p className="text-sm text-muted-foreground">暂无会话</p>
            )}
            {conversations.map((c) => (
              <Card key={c.id} className="group relative">
                <Link href={`/chat/${c.agent_id}?conversation=${c.id}`}>
                  <CardHeader className="py-3 px-4">
                    <CardTitle className="text-sm">{c.title}</CardTitle>
                    {c.last_message_preview && (
                      <CardDescription className="text-xs truncate">
                        {c.last_message_preview}
                      </CardDescription>
                    )}
                  </CardHeader>
                </Link>
                <Button
                  variant="ghost"
                  size="icon"
                  className="absolute top-1 right-1 w-6 h-6 opacity-0 group-hover:opacity-100"
                  onClick={(e) => {
                    e.preventDefault();
                    handleDeleteConv(c.id);
                  }}
                >
                  <Trash2 className="w-3 h-3" />
                </Button>
              </Card>
            ))}
          </div>
        </ScrollArea>
      </div>

      <Separator orientation="vertical" />

      {/* Agent selection */}
      <div className="flex-1">
        <h2 className="text-lg font-semibold mb-3">选择智能体</h2>
        {agents.length === 0 ? (
          <p className="text-muted-foreground">暂无可测试的智能体</p>
        ) : (
          <div className="grid gap-3">
            {agents.map((agent) => (
              <Link key={agent.id} href={`/chat/${agent.id}`}>
                <Card className="hover:border-primary transition-colors cursor-pointer">
                  <CardHeader>
                    <CardTitle>{agent.name}</CardTitle>
                    <CardDescription>
                      {agent.description || "暂无描述"} · {agent.model?.model_name || "未配置模型"}
                    </CardDescription>
                  </CardHeader>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}