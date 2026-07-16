"use client";
import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";

const NODE_COLORS: Record<string, string> = {
  i: "border-emerald-500 bg-emerald-50 dark:bg-emerald-950",
  p: "border-blue-500 bg-blue-50 dark:bg-blue-950",
  m: "border-purple-500 bg-purple-50 dark:bg-purple-950",
  o: "border-rose-500 bg-rose-50 dark:bg-rose-950",
  k: "border-amber-500 bg-amber-50 dark:bg-amber-950",
  t: "border-cyan-500 bg-cyan-50 dark:bg-cyan-950",
  c: "border-orange-500 bg-orange-50 dark:bg-orange-950",
  x: "border-green-500 bg-green-50 dark:bg-green-950",
  h: "border-indigo-500 bg-indigo-50 dark:bg-indigo-950",
  a: "border-red-500 bg-red-50 dark:bg-red-950",
  mem: "border-pink-500 bg-pink-50 dark:bg-pink-950",
  agent: "border-violet-600 bg-violet-50 dark:bg-violet-950",
};

const NODE_LABELS: Record<string, string> = {
  i: "输入", p: "提示词", m: "模型", o: "输出",
  k: "知识库", t: "工具", c: "条件", x: "代码",
  h: "HTTP", a: "审批", mem: "记忆", agent: "智能体",
};

const STATUS_INDICATOR: Record<string, string> = {
  idle: "",
  executing: "animate-pulse ring-2 ring-yellow-400",
  complete: "ring-2 ring-green-400",
  error: "ring-2 ring-red-400",
};

const AGENT_TOP_INPUTS = [
  { id: "prompt", label: "P", color: "!bg-blue-500", left: "35%" },
  { id: "model", label: "M", color: "!bg-purple-500", left: "65%" },
];

const AGENT_BOTTOM_INPUTS = [
  { id: "knowledge", label: "K", color: "!bg-amber-500", left: "25%" },
  { id: "tools", label: "T", color: "!bg-cyan-500", left: "50%" },
  { id: "memory", label: "Mem", color: "!bg-pink-500", left: "75%" },
];

type NodeConfigLike = Record<string, string | number | boolean | undefined> | undefined;

function pickIdentity(config: NodeConfigLike, key: string): string | null {
  const v = config?.[key];
  if (typeof v !== "string") return null;
  const trimmed = v.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function deriveNodeLabel(type: string, config: NodeConfigLike): string {
  const fallback = NODE_LABELS[type] || type;
  switch (type) {
    case "agent":
    case "p":
      return pickIdentity(config, "role_name") ?? fallback;
    case "m":
      return pickIdentity(config, "model_name") ?? fallback;
    case "k":
      return pickIdentity(config, "knowledge_name") ?? fallback;
    case "t":
      return pickIdentity(config, "tool_name") ?? fallback;
    default:
      return fallback;
  }
}

function truncateLabel(label: string, max = 24): string {
  return label.length > max ? `${label.slice(0, max)}…` : label;
}

function DAGNode({ data, selected }: NodeProps) {
  const nodeType = String(data.type || "?");
  const colorClass = NODE_COLORS[nodeType] || "border-gray-400 bg-gray-50 dark:bg-gray-900";
  const config = data.config as NodeConfigLike;
  const fullLabel = String(data.label || deriveNodeLabel(nodeType, config));
  const label = truncateLabel(fullLabel);
  const status = String(data.status || "idle");
  const isAgent = nodeType === "agent";

  // P, M nodes: output on bottom (connects down to Agent top)
  // K, T, Mem nodes: output on top (connects up to Agent bottom)
  const outputOnBottom = ["p", "m"].includes(nodeType);
  const outputOnTop = ["k", "t", "mem"].includes(nodeType);
  const hasInput = !["i", "p", "m", "k", "t", "mem"].includes(nodeType) && nodeType !== "agent";
  const hasOutputRight = !isAgent && !outputOnBottom && !outputOnTop && nodeType !== "o";

  return (
    <div
      title={fullLabel}
      className={`rounded-lg border-2 shadow-sm transition-all ${colorClass} ${STATUS_INDICATOR[status]} ${selected ? "ring-2 ring-primary" : ""} ${isAgent ? "min-w-[220px] px-4 py-3" : "min-w-[120px] px-3 py-2"}`}
    >
      {/* Agent: top inputs (P, M) */}
      {isAgent && AGENT_TOP_INPUTS.map((input) => (
        <Handle
          key={input.id}
          type="target"
          position={Position.Top}
          id={input.id}
          className={`!w-3 !h-3 ${input.color} !border-2 !border-white`}
          style={{ left: input.left }}
        />
      ))}

      {/* Agent: bottom inputs (K, T, Mem) */}
      {isAgent && AGENT_BOTTOM_INPUTS.map((input) => (
        <Handle
          key={input.id}
          type="target"
          position={Position.Bottom}
          id={input.id}
          className={`!w-3 !h-3 ${input.color} !border-2 !border-white`}
          style={{ left: input.left }}
        />
      ))}

      {/* Agent: input left, output right */}
      {isAgent && (
        <Handle type="target" position={Position.Left} id="input" className="!w-3 !h-3 !bg-emerald-500 !border-2 !border-white" />
      )}
      {isAgent && (
        <Handle type="source" position={Position.Right} className="!w-3 !h-3 !bg-violet-500 !border-2 !border-white" />
      )}

      {/* P, M nodes: single output on bottom */}
      {outputOnBottom && (
        <Handle type="source" position={Position.Bottom} className="!w-2.5 !h-2.5 !bg-gray-400 !border-0" />
      )}

      {/* K, T nodes: single output on top */}
      {outputOnTop && (
        <Handle type="source" position={Position.Top} className="!w-2.5 !h-2.5 !bg-gray-400 !border-0" />
      )}

      {/* Other regular nodes: input left, output right */}
      {hasInput && (
        <Handle type="target" position={Position.Left} className="!w-2.5 !h-2.5 !bg-gray-400 !border-0" />
      )}
      {hasOutputRight && (
        <Handle type="source" position={Position.Right} className="!w-2.5 !h-2.5 !bg-gray-400 !border-0" />
      )}

      <div className="flex items-center justify-center">
        <span className={`font-medium ${isAgent ? "text-base" : "text-sm"}`}>{isAgent ? "🤖 " : ""}{label}</span>
      </div>

      {status === "executing" && (
        <div className="mt-1 text-xs text-yellow-600 dark:text-yellow-400">执行中...</div>
      )}
      {status === "complete" && (
        <div className="mt-1 text-xs text-green-600 dark:text-green-400">完成</div>
      )}
      {status === "error" && (
        <div className="mt-1 text-xs text-red-600 dark:text-red-400">错误</div>
      )}
    </div>
  );
}

export const nodeTypes = {
  dagNode: memo(DAGNode),
};

export function buildDAGNode(
  id: string,
  nodeType: string,
  position: { x: number; y: number },
  config?: Record<string, string | number | boolean | undefined>,
) {
  return {
    id,
    type: "dagNode",
    position,
    data: {
      type: nodeType,
      label: deriveNodeLabel(nodeType, config),
      status: "idle",
      config: config ?? {},
    },
  };
}
