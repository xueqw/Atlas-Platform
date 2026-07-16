"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const CATEGORY_NAMES: Record<string, string> = {
  core: "核心节点",
  knowledge: "知识节点",
  flow: "流程控制",
  external: "外部集成",
};

const DISPLAY_NAMES: Record<string, string> = {
  i: "输入", o: "输出", agent: "智能体", mem: "记忆",
  k: "知识库", t: "工具", c: "条件", x: "代码", h: "HTTP", a: "审批",
};

const CATEGORY_ORDER = ["core", "knowledge", "flow", "external"];

interface NodeTypeInfo {
  node_type: string;
  display_name: string;
  category: string;
  config_schema: string;
  input_keys: string;
  output_keys: string;
  enabled: boolean;
}

export default function NodePalette() {
  const [nodeTypes, setNodeTypes] = useState<NodeTypeInfo[]>([]);

  useEffect(() => {
    api.getNodeTypes().then((data) => setNodeTypes(data)).catch(() => {});
  }, []);

  const HIDDEN_FROM_PALETTE = ["p", "m"];

  const grouped = new Map<string, NodeTypeInfo[]>();
  for (const nt of nodeTypes) {
    if (HIDDEN_FROM_PALETTE.includes(nt.node_type)) continue;
    let cat = nt.category || "core";
    if (cat === "tool") cat = "core";
    if (!grouped.has(cat)) grouped.set(cat, []);
    grouped.get(cat)!.push(nt);
  }

  const orderedCategories = CATEGORY_ORDER.filter((c) => grouped.has(c));

  const onDragStart = (event: React.DragEvent, nodeType: string) => {
    event.dataTransfer.setData("application/node-type", nodeType);
    event.dataTransfer.effectAllowed = "move";
  };

  return (
    <div className="p-3 space-y-4">
      <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">节点面板</h3>
      {orderedCategories.map((cat) => (
        <div key={cat}>
          <p className="text-xs text-muted-foreground mb-1.5">{CATEGORY_NAMES[cat] || cat}</p>
          <div className="space-y-1">
            {grouped.get(cat)!.map((nt) => (
              <div
                key={nt.node_type}
                draggable
                onDragStart={(e) => onDragStart(e, nt.node_type)}
                className="flex items-center justify-center px-2.5 py-1.5 rounded-md border border-border bg-card hover:bg-accent cursor-grab active:cursor-grabbing transition-colors text-sm"
              >
                <span>{DISPLAY_NAMES[nt.node_type] || nt.display_name}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
      {orderedCategories.length === 0 && (
        <p className="text-xs text-muted-foreground">加载中...</p>
      )}
    </div>
  );
}
