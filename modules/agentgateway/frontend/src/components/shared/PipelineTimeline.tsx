"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { PipelineNodeStatus } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { FileText, Cpu, Database, ExternalLink, Clock, Wrench } from "lucide-react";

const NODE_META: Record<string, { label: string; icon: typeof FileText }> = {
  p: { label: "P: 提示词", icon: FileText },
  m: { label: "M: 模型", icon: Cpu },
  k: { label: "K: 知识库", icon: Database },
  t: { label: "T: 工具", icon: Wrench },
};

const STATUS_LABEL: Record<string, string> = {
  pending: "等待中",
  in_progress: "进行中",
  complete: "已完成",
  failed: "失败",
  optional: "可选",
};

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  pending: "secondary",
  in_progress: "default",
  complete: "default",
  failed: "destructive",
  optional: "outline",
};

function elapsedMs(started: string | null, completed: string | null): string {
  if (!started) return "";
  const end = completed ? new Date(completed) : new Date();
  const ms = end.getTime() - new Date(started).getTime();
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

interface Props {
  agentId: number;
  selectedNode: string;
  onNodeSelect: (nodeName: string) => void;
}

export default function PipelineTimeline({ agentId, selectedNode, onNodeSelect }: Props) {
  const [nodes, setNodes] = useState<PipelineNodeStatus[]>([]);

  useEffect(() => {
    let active = true;
    const poll = () => {
      if (!active) return;
      api.getPipelineStatus(agentId).then((data) => {
        if (active) setNodes(data.nodes);
      }).catch(() => {});
    };
    poll();
    const interval = setInterval(poll, 2000);
    return () => { active = false; clearInterval(interval); };
  }, [agentId]);

  return (
    <div className="flex flex-col gap-0 py-4">
      {nodes.map((node, i) => {
        const meta = NODE_META[node.node_name] || { label: node.node_name, icon: FileText };
        const Icon = meta.icon;
        const isSelected = selectedNode === node.node_name;
        const isLast = i === nodes.length - 1;

        return (
          <div key={node.node_name} className="relative">
            {/* Connecting line */}
            {!isLast && (
              <div className="absolute left-[19px] top-12 bottom-0 w-0.5 bg-border" />
            )}

            <button
              className={`w-full text-left p-3 rounded-lg transition-colors flex items-start gap-3 ${
                isSelected ? "bg-accent" : "hover:bg-muted/50"
              }`}
              onClick={() => onNodeSelect(node.node_name)}
            >
              {/* Icon circle */}
              <div
                className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 relative z-10 ${
                  node.status === "complete"
                    ? "bg-primary text-primary-foreground"
                    : node.status === "failed"
                    ? "bg-destructive text-destructive-foreground"
                    : node.status === "in_progress"
                    ? "bg-blue-500 text-white animate-pulse"
                    : "bg-muted text-muted-foreground"
                }`}
              >
                <Icon className="w-4 h-4" />
              </div>

              {/* Node info */}
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{meta.label}</div>
                <div className="flex items-center gap-2 mt-1">
                  <Badge variant={STATUS_VARIANT[node.status]}>
                    {STATUS_LABEL[node.status] || node.status}
                  </Badge>
                  {node.quality_score !== null && node.quality_score !== undefined && (
                    <span className="text-xs text-muted-foreground">
                      质量分: {(node.quality_score * 100).toFixed(0)}%
                    </span>
                  )}
                </div>
                {node.started_at && (
                  <div className="flex items-center gap-1 mt-1 text-xs text-muted-foreground">
                    <Clock className="w-3 h-3" />
                    {elapsedMs(node.started_at, node.completed_at)}
                  </div>
                )}
                {node.trace_url && (
                  <a
                    href={node.trace_url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center gap-1 mt-1 text-xs text-blue-500 hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <ExternalLink className="w-3 h-3" /> 在 Langfuse 中查看
                  </a>
                )}
              </div>
            </button>
          </div>
        );
      })}
    </div>
  );
}