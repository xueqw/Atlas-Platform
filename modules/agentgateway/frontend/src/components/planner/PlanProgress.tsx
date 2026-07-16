"use client";
/**
 * PlanProgress — renders the Plan+Loop step progress as a two-layer timeline.
 *
 * Layer 1: step list with status icons (✅ done, 🔄 running, ⏸ waiting, ⬜ pending)
 * Layer 2 (future): expandable step details showing tool calls / artifacts
 *
 * Only rendered when planState is non-null (Plan+Loop mode).
 * Legacy sessions (planState === null) skip this component entirely.
 */
import { CheckCircle2, Circle, Loader2, PauseCircle, XCircle, ChevronDown } from "lucide-react";
import { useState } from "react";
import type { PlanState, PlanStepState, PlanStepStatus } from "@/lib/planner-session";

interface Props {
  planState: PlanState;
}

const STATUS_ICON: Record<PlanStepStatus, React.ReactNode> = {
  done: <CheckCircle2 className="w-4 h-4 text-green-500" />,
  running: <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />,
  waiting_user: <PauseCircle className="w-4 h-4 text-amber-500" />,
  failed: <XCircle className="w-4 h-4 text-red-500" />,
  pending: <Circle className="w-4 h-4 text-gray-300" />,
  blocked: <Circle className="w-4 h-4 text-gray-300" />,
  skipped: <Circle className="w-4 h-4 text-gray-300 line-through" />,
};

const STATUS_LABEL: Record<PlanStepStatus, string> = {
  done: "完成",
  running: "执行中",
  waiting_user: "等待确认",
  failed: "失败",
  pending: "待执行",
  blocked: "阻塞",
  skipped: "跳过",
};

export default function PlanProgress({ planState }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const { steps } = planState;

  // Only show steps that have been triggered (not pending/blocked)
  const triggeredSteps = steps.filter(
    (s) => s.status !== "pending" && s.status !== "blocked"
  );

  if (triggeredSteps.length === 0) return null;

  const doneSteps = triggeredSteps.filter((s) => s.status === "done");
  // Auto-collapse: when >3 steps are done, fold early completed ones
  const shouldAutoFold = doneSteps.length > 3;
  const foldCount = shouldAutoFold ? doneSteps.length - 2 : 0;

  const visibleSteps = shouldAutoFold && !collapsed
    ? triggeredSteps.slice(foldCount)
    : triggeredSteps;

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs text-gray-500 mb-2">
        <span className="font-medium">构建计划</span>
        <span>
          {doneSteps.length}/{steps.length} 步完成
        </span>
      </div>

      {/* Folded summary */}
      {shouldAutoFold && !collapsed && foldCount > 0 && (
        <button
          onClick={() => setCollapsed(true)}
          className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 py-0.5"
        >
          <ChevronDown className="w-3 h-3" />
          {foldCount} 个步骤已完成
        </button>
      )}

      {/* Step list — only triggered steps */}
      {(collapsed ? triggeredSteps : visibleSteps).map((step) => (
        <div
          key={step.id}
          className={`flex items-center gap-2 py-1 px-2 rounded text-sm ${
            step.status === "running" ? "bg-blue-50" :
            step.status === "waiting_user" ? "bg-amber-50" :
            step.status === "failed" ? "bg-red-50" :
            ""
          }`}
        >
          {STATUS_ICON[step.status] || STATUS_ICON.pending}
          <span className={step.status === "done" ? "text-gray-500" : "text-gray-800"}>
            {step.title}
          </span>
          {step.status === "failed" && step.error && (
            <span className="text-xs text-red-500 truncate max-w-[200px]" title={step.error}>
              {step.error}
            </span>
          )}
        </div>
      ))}

      {/* Plan status footer */}
      {planState.status === "completed" && (
        <div className="text-xs text-green-600 mt-2">✅ 计划执行完成</div>
      )}
      {planState.status === "failed" && (
        <div className="text-xs text-red-600 mt-2">❌ 计划执行失败</div>
      )}
      {planState.stop_reason === "budget_exhausted" && (
        <div className="text-xs text-amber-600 mt-2">⚠️ 执行预算已用尽</div>
      )}
    </div>
  );
}
