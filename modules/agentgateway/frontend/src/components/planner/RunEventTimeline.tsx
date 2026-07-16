"use client";
import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";
import type { RunEvent } from "@/lib/planner-session";

// Normalized step labels (T8 RUN_STEPS) for the white-box progress display.
export const STEP_LABELS: Record<string, string> = {
  intent_inference: "理解需求",
  context_load: "读取上下文",
  memory_recall: "召回记忆",
  expert_retrieval: "检索专家模板",
  capability_match: "匹配能力",
  proposal_compose: "生成方案",
};
export const STEP_ORDER = [
  "intent_inference", "context_load", "memory_recall",
  "expert_retrieval", "capability_match", "proposal_compose",
];

/**
 * Run-event timeline (T7 consuming T8 stream): shows the normalized planner
 * stages with status + the stuck-step-explainable message, plus the proposal's
 * context_used_explanation. Renders nothing when there are no events.
 */
export default function RunEventTimeline({
  runEvents,
  contextUsed,
}: {
  runEvents: RunEvent[];
  contextUsed?: string[];
}) {
  if ((!runEvents || runEvents.length === 0) && (!contextUsed || contextUsed.length === 0)) {
    return null;
  }
  const byStep = new Map(runEvents.map((e) => [e.step, e]));
  const ordered = STEP_ORDER.filter((s) => byStep.has(s));

  return (
    <div className="rounded-xl border border-border/40 p-3 space-y-2">
      {ordered.length > 0 && (
        <>
          <div className="text-[11px] font-medium text-muted-foreground">运行过程</div>
          <div className="space-y-1">
            {ordered.map((step) => {
              const ev = byStep.get(step)!;
              return (
                <div key={step} className="flex items-start gap-2 text-xs">
                  {ev.status === "completed" ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-green-600 shrink-0 mt-0.5" />
                  ) : ev.status === "failed" ? (
                    <XCircle className="w-3.5 h-3.5 text-red-600 shrink-0 mt-0.5" />
                  ) : ev.status === "running" ? (
                    <Loader2 className="w-3.5 h-3.5 text-primary shrink-0 mt-0.5 animate-spin" />
                  ) : (
                    <Circle className="w-3.5 h-3.5 text-muted-foreground shrink-0 mt-0.5" />
                  )}
                  <div className="min-w-0">
                    <span className="font-medium">{STEP_LABELS[step] || step}</span>
                    {ev.message && <span className="text-muted-foreground"> — {ev.message}</span>}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

      {contextUsed && contextUsed.length > 0 && (
        <div className="pt-2 border-t border-border/40">
          <div className="text-[11px] font-medium text-muted-foreground mb-1">本次用了哪些上下文</div>
          <ul className="space-y-0.5">
            {contextUsed.map((c, i) => (
              <li key={i} className="text-[11px] text-muted-foreground flex gap-1.5">
                <span className="text-primary shrink-0">•</span>{c}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
