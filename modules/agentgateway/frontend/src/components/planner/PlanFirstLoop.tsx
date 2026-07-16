"use client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CheckCircle2, XCircle, AlertTriangle } from "lucide-react";
import type { DraftAgent, CompileResult } from "@/lib/planner-session";

/**
 * Plan-first builder loop block (T7): confirm proposal → draft agent → compile.
 * Sits alongside (not replacing) the legacy "创建到工作台" direct-apply button.
 */
export default function PlanFirstLoop({
  proposalId,
  proposalStatus,
  draftAgent,
  compileResult,
  onConfirm,
  onCompile,
}: {
  proposalId: number | null;
  proposalStatus: string | null;
  draftAgent: DraftAgent | null;
  compileResult: CompileResult | null;
  onConfirm: (proposalId: number) => void;
  onCompile: (draftAgentId: number) => void;
}) {
  if (!proposalId) return null;
  const confirmed = proposalStatus === "confirmed" || proposalStatus === "applied";

  return (
    <div className="pt-3 mt-3 border-t border-border/40 space-y-3">
      <div className="text-[11px] font-medium text-muted-foreground">Plan-first 闭环（草案 → 编译）</div>

      {/* Step 1: confirm proposal → derive draft */}
      {!draftAgent && (
        <Button
          variant="secondary"
          className="w-full rounded-xl"
          onClick={() => onConfirm(proposalId)}
          disabled={confirmed}
        >
          {confirmed ? "方案已确认" : "确认并生成草案"}
        </Button>
      )}

      {/* Step 2: draft agent summary + compile */}
      {draftAgent && (
        <div className="rounded-xl border border-border/50 p-3 space-y-2 bg-muted/30">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium">{draftAgent.name || "草案智能体"}</span>
            <Badge variant="secondary" className="text-[10px] px-1.5 py-0 h-4">{draftAgent.status}</Badge>
            <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-4">{draftAgent.runtime_mode}</Badge>
          </div>
          {draftAgent.capability_refs && draftAgent.capability_refs.length > 0 && (
            <div className="text-[10px] text-muted-foreground">
              能力：{draftAgent.capability_refs.map((c) => c.name).filter(Boolean).join("、")}
            </div>
          )}
          <Button
            size="sm"
            variant="outline"
            className="w-full rounded-lg"
            onClick={() => onCompile(draftAgent.id)}
          >
            编译 / 试运行
          </Button>
        </div>
      )}

      {/* Step 3: compile result */}
      {compileResult && (
        <div className="rounded-xl border border-border/50 p-3 space-y-1.5 text-xs">
          <div className="flex items-center gap-1.5 font-medium">
            {compileResult.success
              ? <CheckCircle2 className="w-3.5 h-3.5 text-green-600 shrink-0" />
              : <XCircle className="w-3.5 h-3.5 text-red-600 shrink-0" />}
            <span>{compileResult.success ? "编译通过" : "编译未通过"}</span>
            {compileResult.dryrun?.ran && (
              <Badge variant={compileResult.dryrun.ok ? "secondary" : "outline"} className="text-[10px] px-1.5 py-0 h-4">
                {compileResult.dryrun.ok ? "试运行通过" : "试运行告警"}
              </Badge>
            )}
          </div>
          {compileResult.errors.map((e, i) => (
            <div key={`e${i}`} className="flex gap-1.5 text-red-600">
              <XCircle className="w-3 h-3 shrink-0 mt-0.5" />{e}
            </div>
          ))}
          {compileResult.warnings.map((w, i) => (
            <div key={`w${i}`} className="flex gap-1.5 text-amber-600">
              <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />{w}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
