"use client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Loader2 } from "lucide-react";

interface ProposalNode {
  id: string;
  type: string;
  config: Record<string, unknown>;
  description?: string;
}

interface ProposalEdge {
  source: string;
  target: string;
  targetHandle?: string;
}

interface Proposal {
  architecture_summary: string;
  nodes: ProposalNode[];
  edges: ProposalEdge[];
  rationale: string;
  tuning_hints?: string[];
}

interface Props {
  proposal: Proposal;
  onApply: () => void;
  applying: boolean;
}

const NODE_TYPE_LABELS: Record<string, string> = {
  agent: "🤖 智能体", p: "📝 提示词", m: "🧠 模型",
  k: "📚 知识库", t: "🔧 工具", mem: "💾 记忆",
  i: "📥 输入", o: "📤 输出", c: "🔀 条件", x: "💻 代码", h: "🌐 HTTP",
};

export default function ProposalCard({ proposal, onApply, applying }: Props) {
  return (
    <div className="space-y-4">
      {/* Architecture Summary */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">架构摘要</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm">{proposal.architecture_summary}</p>
        </CardContent>
      </Card>

      {/* Nodes */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">节点组成</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {proposal.nodes.map((node) => (
            <div key={node.id} className="flex items-start gap-2 p-2 rounded border border-border bg-muted/30">
              <Badge variant="outline" className="text-xs shrink-0">
                {NODE_TYPE_LABELS[node.type] || node.type}
              </Badge>
              <div className="min-w-0">
                <p className="text-xs font-medium">{node.id}</p>
                {node.description && (
                  <p className="text-xs text-muted-foreground truncate">{node.description}</p>
                )}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Rationale */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">推荐理由</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">{proposal.rationale}</p>
        </CardContent>
      </Card>

      {/* Tuning Hints */}
      {proposal.tuning_hints && proposal.tuning_hints.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">调优建议</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="text-xs text-muted-foreground space-y-1">
              {proposal.tuning_hints.map((hint, i) => (
                <li key={i}>• {hint}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <Separator />

      {/* Actions */}
      <Button className="w-full" onClick={onApply} disabled={applying}>
        {applying && <Loader2 className="w-4 h-4 mr-2 animate-spin" />}
        创建到工作台
      </Button>
    </div>
  );
}
