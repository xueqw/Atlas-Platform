"use client";
import { useState } from "react";
import { FileText, FileJson, FileCheck2, History, Eye, X, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { getApiBase } from "@/lib/runtime-env";

export interface PlanFileItem {
  path: string;
  filename?: string;
  kind: string;
  size?: number;
  updated_at?: string;
  summary?: string;
}

interface Props {
  conversationId: string | null;
  files: PlanFileItem[];
}

const KIND_LABEL: Record<string, string> = {
  requirements: "需求摘要",
  architecture: "架构说明",
  proposal: "结构化方案",
  decisions: "确认记录",
  final: "最终交付",
};

const KIND_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  requirements: FileText,
  architecture: FileText,
  proposal: FileJson,
  decisions: History,
  final: FileCheck2,
};

export default function PlanFileViewer({ conversationId, files }: Props) {
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!conversationId || files.length === 0) {
    return null;
  }

  // De-duplicate by filename, keeping latest entry's metadata.
  const dedup = new Map<string, PlanFileItem>();
  for (const f of files) {
    const key = f.filename || f.path;
    dedup.set(key, f);
  }
  const items = Array.from(dedup.values()).sort((a, b) =>
    (a.kind || "").localeCompare(b.kind || "")
  );

  async function open(filename: string) {
    setActiveFile(filename);
    setContent(null);
    setError(null);
    setLoading(true);
    try {
      const res = await fetch(
        `${getApiBase()}/api/planner/sessions/${encodeURIComponent(conversationId!)}/files/${encodeURIComponent(filename)}`
      );
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      setContent(data.content || "");
    } catch (e) {
      setError((e as Error)?.message || "读取失败");
    } finally {
      setLoading(false);
    }
  }

  function close() {
    setActiveFile(null);
    setContent(null);
    setError(null);
  }

  return (
    <div className="space-y-1.5">
      <h4 className="text-xs font-semibold text-foreground/80">规划产物</h4>
      <div className="space-y-1">
        {items.map((f) => {
          const Icon = KIND_ICON[f.kind] || FileText;
          const label = KIND_LABEL[f.kind] || f.kind;
          return (
            <button
              key={f.filename || f.path}
              onClick={() => open(f.filename || f.path.split("/").pop() || "")}
              className={cn(
                "w-full flex items-start gap-2 px-2.5 py-1.5 rounded-lg",
                "bg-muted/40 hover:bg-muted/70 border border-border/40 transition-colors text-left"
              )}
            >
              <Icon className="w-3.5 h-3.5 text-primary mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-xs font-medium truncate">{label}</div>
                <div className="text-[10px] text-muted-foreground truncate">
                  {f.filename || f.path}
                </div>
                {f.summary && (
                  <div className="text-[10px] text-muted-foreground/80 truncate mt-0.5">
                    {f.summary}
                  </div>
                )}
              </div>
              <Eye className="w-3 h-3 text-muted-foreground/60 shrink-0 mt-1" />
            </button>
          );
        })}
      </div>

      {activeFile && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4"
          onClick={close}
        >
          <div
            className="bg-background border border-border rounded-xl shadow-2xl max-w-3xl w-full max-h-[80vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 px-4 py-2.5 border-b">
              <FileText className="w-4 h-4 text-primary" />
              <span className="text-sm font-medium">{activeFile}</span>
              <div className="flex-1" />
              <button onClick={close} className="p-1 rounded hover:bg-muted/60">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 overflow-auto p-4">
              {loading && (
                <div className="flex items-center justify-center py-8 text-muted-foreground">
                  <Loader2 className="w-4 h-4 animate-spin" />
                </div>
              )}
              {error && (
                <div className="text-sm text-destructive">{error}</div>
              )}
              {content !== null && !error && (
                <pre className="text-xs whitespace-pre-wrap break-words font-mono leading-relaxed">
                  {content}
                </pre>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
