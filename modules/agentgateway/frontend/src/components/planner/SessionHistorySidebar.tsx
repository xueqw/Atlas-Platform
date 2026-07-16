"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Loader2, Plus, History, Trash2 } from "lucide-react";

export interface SessionListItem {
  conversation_id: string;
  session_title: string;
  stage: string;
  linked_agent_id: number | null;
  last_updated_at: string;
  created_at: string;
}

const STAGE_LABEL: Record<string, string> = {
  clarifying: "澄清中",
  drafting: "方案起草",
  awaiting_confirmation: "待确认",
  ready_to_apply: "可创建",
  applied: "已应用",
};

function formatRelative(iso: string): string {
  try {
    const t = new Date(iso).getTime();
    const diff = Date.now() - t;
    const min = Math.floor(diff / 60000);
    if (min < 1) return "刚刚";
    if (min < 60) return `${min} 分钟前`;
    const h = Math.floor(min / 60);
    if (h < 24) return `${h} 小时前`;
    const d = Math.floor(h / 24);
    if (d < 30) return `${d} 天前`;
    return new Date(iso).toLocaleDateString("zh-CN");
  } catch {
    return iso;
  }
}

interface Props {
  activeConversationId: string | null;
  onSelect: (conversationId: string) => void;
  onNew: () => void;
  onDelete?: (conversationId: string, title: string) => void;
  refreshKey?: number;
  // conversation_ids with an in-flight generation — shown with a spinner.
  workingConversationIds?: string[];
}

export default function SessionHistorySidebar({ activeConversationId, onSelect, onNew, onDelete, refreshKey, workingConversationIds }: Props) {
  const [items, setItems] = useState<SessionListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const working = new Set(workingConversationIds || []);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    api.listPlannerSessions()
      .then((rows) => { if (!cancelled) setItems(rows); })
      .catch((e) => { if (!cancelled) setError(e?.message || "加载会话失败"); });
    return () => { cancelled = true; };
  }, [refreshKey]);

  return (
    <aside className="w-60 xl:w-64 border-r flex flex-col bg-card/40 shrink-0 min-h-0">
      <div className="px-3 py-2.5 border-b flex items-center gap-2">
        <History className="w-3.5 h-3.5 text-muted-foreground" />
        <span className="text-xs font-medium">规划历史</span>
        <div className="flex-1" />
        <button
          onClick={onNew}
          className="text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded-md hover:bg-muted/60 text-muted-foreground hover:text-foreground transition-colors"
          title="新建规划"
        >
          <Plus className="w-3 h-3" /> 新建
        </button>
      </div>
      <div className="flex-1 overflow-y-auto">
        {items === null && !error && (
          <div className="flex items-center justify-center py-6 text-muted-foreground">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          </div>
        )}
        {error && (
          <div className="px-3 py-3 text-[11px] text-destructive">{error}</div>
        )}
        {items && items.length === 0 && (
          <div className="px-3 py-6 text-[11px] text-muted-foreground text-center">
            还没有规划会话
          </div>
        )}
        {items && items.map((it) => {
          const active = it.conversation_id === activeConversationId;
          return (
            <div
              key={it.conversation_id}
              className={cn(
                "group relative w-full border-b border-border/40 hover:bg-muted/50 transition-colors",
                active && "bg-primary/10 hover:bg-primary/15"
              )}
            >
              <button
                onClick={() => onSelect(it.conversation_id)}
                className="w-full text-left px-3 py-2"
              >
                <div className="flex items-center gap-2 mb-1">
                  {working.has(it.conversation_id) && (
                    <Loader2 className="w-3 h-3 animate-spin text-primary shrink-0" aria-label="工作中" />
                  )}
                  <span className={cn(
                    "text-xs font-medium truncate flex-1 pr-6",
                    active ? "text-primary" : "text-foreground"
                  )}>
                    {it.session_title || "未命名会话"}
                  </span>
                </div>
                <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                  <span className="px-1.5 py-0.5 rounded bg-muted/60">
                    {STAGE_LABEL[it.stage] || it.stage}
                  </span>
                  {it.linked_agent_id !== null && (
                    <span className="px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                      #{it.linked_agent_id}
                    </span>
                  )}
                  <span>· {formatRelative(it.last_updated_at)}</span>
                </div>
              </button>
              {onDelete && (
                <button
                  onClick={(e) => { e.stopPropagation(); onDelete(it.conversation_id, it.session_title || "未命名会话"); }}
                  className="absolute right-2 top-2 p-1 rounded-md opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-all"
                  title="删除会话"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          );
        })}
      </div>
    </aside>
  );
}
