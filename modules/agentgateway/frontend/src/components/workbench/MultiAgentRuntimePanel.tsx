"use client";

import { useMemo } from "react";
import { CheckCircle2, GitBranch, Loader2, Network, ShieldCheck } from "lucide-react";
import type { AtlasRuntimeEvent } from "@/lib/atlas-runtime";

type WorkerView = Readonly<{
  workerId: string;
  role: string;
  objective: string;
  status: string;
}>;

type RuntimeTeamView = Readonly<{
  strategy: string;
  goal: string;
  workers: readonly WorkerView[];
  reviewerId: string;
  reviewStatus: string;
  verdict: string;
  runStatus: string;
}>;

const strategyLabels: Record<string, string> = {
  react: "ReAct",
  "plan-execute-review": "Plan · Execute · Review",
  "multi-agent-plan-execute-review": "Multi-Agent · Plan · Execute · Review",
};

const statusLabels: Record<string, string> = {
  created: "已创建",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  paused: "等待确认",
  pending: "等待中",
};

export default function MultiAgentRuntimePanel({ events, runId, running }: {
  events: readonly AtlasRuntimeEvent[];
  runId: string | null;
  running: boolean;
}) {
  const view = useMemo(() => projectRuntimeTeam(events, running), [events, running]);
  if (!runId || events.length === 0) return null;
  const react = view.strategy === "react";

  return (
    <section className="mx-3 mt-2 overflow-hidden rounded-xl border border-slate-200 bg-slate-50 shadow-sm dark:border-slate-700 dark:bg-slate-950/40" aria-label="Atlas 多智能体运行视图">
      <header className="flex h-auto items-start justify-between gap-3 border-0 bg-slate-950 px-3 py-2.5 text-white">
        <div className="min-w-0">
          <span className="flex items-center gap-1.5 text-[9px] font-semibold tracking-[0.16em] text-blue-300">
            <i className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" /> ATLAS RUNTIME
          </span>
          <strong className="mt-1 block truncate text-xs">{react ? "单智能体执行" : "多智能体协作"}</strong>
          <small className="mt-0.5 block text-[9px] text-slate-400">{strategyLabels[view.strategy] || view.strategy || "正在选择执行框架"}</small>
        </div>
        <span className="rounded-full border border-white/10 bg-white/5 px-2 py-1 text-[9px] text-slate-300">
          {statusLabels[view.runStatus] || view.runStatus}
        </span>
      </header>

      <div className="space-y-2 p-2.5">
        <AgentRow
          icon={<Network className="h-3.5 w-3.5" />}
          label={react ? "PRIMARY AGENT" : "ORCHESTRATOR"}
          title={react ? "ReAct Agent" : "任务编排者"}
          description={view.goal || (react ? "推理、行动、观察" : "拆解目标并生成 DAG")}
          active={running && (react || view.workers.length === 0)}
        />

        {react ? (
          <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-blue-200 bg-blue-50/70 py-2 text-[9px] font-semibold tracking-wide text-blue-700 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-300">
            <span>THINK</span><i>→</i><span>ACT</span><i>→</i><span>OBSERVE</span><i>↺</i>
          </div>
        ) : (
          <>
            <FlowLabel icon={<GitBranch className="h-3 w-3" />} text="SUBAGENTS" />
            <div className="grid grid-cols-2 gap-1.5">
              {view.workers.length > 0 ? view.workers.map((worker, index) => (
                <article key={worker.workerId} className="min-w-0 rounded-lg border border-slate-200 bg-white p-2 dark:border-slate-700 dark:bg-slate-900">
                  <div className="flex items-center gap-1.5">
                    <span className="grid h-6 w-6 shrink-0 place-items-center rounded-md bg-blue-50 text-[10px] font-bold text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                      {String.fromCharCode(65 + (index % 26))}
                    </span>
                    <div className="min-w-0 flex-1">
                      <small className="block text-[7px] font-semibold tracking-wider text-slate-400">WORKER {index + 1}</small>
                      <strong className="block truncate text-[10px] text-slate-700 dark:text-slate-200">{worker.role}</strong>
                    </div>
                    <StatusDot status={worker.status} />
                  </div>
                  <p className="mt-1.5 line-clamp-2 text-[9px] leading-4 text-slate-500 dark:text-slate-400">{worker.objective}</p>
                  <small className="mt-1 block text-[8px] font-medium text-blue-600 dark:text-blue-400">{statusLabels[worker.status] || worker.status}</small>
                </article>
              )) : (
                <div className="col-span-2 flex items-center justify-center gap-2 rounded-lg border border-dashed border-slate-300 py-4 text-[9px] text-slate-500 dark:border-slate-700">
                  <Loader2 className="h-3 w-3 animate-spin" /> 正在创建 Subagents
                </div>
              )}
            </div>

            <FlowLabel icon={<ShieldCheck className="h-3 w-3" />} text="INDEPENDENT REVIEW" />
            <AgentRow
              icon={<ShieldCheck className="h-3.5 w-3.5" />}
              label="REVIEWER SUBAGENT"
              title={view.reviewerId || "等待创建 Reviewer"}
              description={view.verdict ? `审查裁决：${view.verdict}` : "独立核验目标、结果、证据和产物"}
              active={view.reviewStatus === "running"}
              verdict={view.verdict}
            />
          </>
        )}
      </div>
    </section>
  );
}

function AgentRow({ icon, label, title, description, active, verdict }: {
  icon: React.ReactNode;
  label: string;
  title: string;
  description: string;
  active: boolean;
  verdict?: string;
}) {
  return (
    <article className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white p-2 dark:border-slate-700 dark:bg-slate-900">
      <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-slate-900 text-white dark:bg-slate-700">{icon}</span>
      <div className="min-w-0 flex-1">
        <small className="block text-[7px] font-semibold tracking-[0.12em] text-slate-400">{label}</small>
        <strong className="block truncate text-[10px] text-slate-700 dark:text-slate-200">{title}</strong>
        <p className="truncate text-[8px] text-slate-500 dark:text-slate-400">{description}</p>
      </div>
      {verdict ? <span className={verdictClass(verdict)}>{verdict}</span> : active ? <Loader2 className="h-3 w-3 animate-spin text-blue-500" /> : <CheckCircle2 className="h-3 w-3 text-emerald-500" />}
    </article>
  );
}

function FlowLabel({ icon, text }: { icon: React.ReactNode; text: string }) {
  return <div className="flex items-center gap-1.5 px-1 text-[8px] font-semibold tracking-[0.12em] text-slate-400">{icon}<span>{text}</span><i className="h-px flex-1 bg-slate-200 dark:bg-slate-700" /></div>;
}

function StatusDot({ status }: { status: string }) {
  const color = status === "succeeded" ? "bg-emerald-500" : status === "failed" ? "bg-red-500" : status === "running" ? "bg-blue-500 animate-pulse" : "bg-slate-300";
  return <i className={`h-1.5 w-1.5 shrink-0 rounded-full ${color}`} />;
}

function verdictClass(verdict: string): string {
  const base = "rounded-full px-1.5 py-0.5 text-[8px] font-bold";
  if (verdict === "PASS") return `${base} bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300`;
  if (["REVISE", "REPLAN"].includes(verdict)) return `${base} bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300`;
  return `${base} bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300`;
}

function projectRuntimeTeam(events: readonly AtlasRuntimeEvent[], running: boolean): RuntimeTeamView {
  let strategy = "";
  let goal = "";
  let reviewerId = "";
  let reviewStatus = "pending";
  let verdict = "";
  let runStatus = running ? "running" : "pending";
  const workers = new Map<string, WorkerView>();

  for (const event of events) {
    if (event.type === "strategy.selected") strategy = stringValue(event.payload.selected);
    if (event.type === "plan.created" || event.type === "plan.updated") {
      const plan = recordValue(event.payload.plan);
      goal = stringValue(plan?.goal);
      const plannedWorkers = Array.isArray(plan?.workers) ? plan.workers : [];
      for (const raw of plannedWorkers) {
        const worker = recordValue(raw);
        const workerId = stringValue(worker?.worker_id);
        if (!workerId) continue;
        workers.set(workerId, {
          workerId,
          role: stringValue(worker?.role) || workerId,
          objective: stringValue(worker?.objective) || "执行已分配任务",
          status: workers.get(workerId)?.status || "created",
        });
      }
    }
    if (event.type === "worker.created") {
      const workerId = stringValue(event.payload.worker_id);
      const role = stringValue(event.payload.role);
      if (!workerId) continue;
      if (role === "reviewer") {
        reviewerId = workerId;
        reviewStatus = "created";
      } else {
        const previous = workers.get(workerId);
        workers.set(workerId, {
          workerId,
          role: role || previous?.role || workerId,
          objective: previous?.objective || "执行 Orchestrator 分配的子任务",
          status: "created",
        });
      }
    }
    if (event.type === "worker.completed") {
      const workerId = stringValue(event.payload.worker_id);
      if (!workerId) continue;
      const previous = workers.get(workerId);
      workers.set(workerId, {
        workerId,
        role: previous?.role || workerId,
        objective: previous?.objective || "执行 Orchestrator 分配的子任务",
        status: stringValue(event.payload.status) || "succeeded",
      });
    }
    if (event.type === "review.requested") {
      reviewerId = stringValue(event.payload.reviewer_id) || reviewerId;
      reviewStatus = "running";
    }
    if (event.type === "review.completed") {
      reviewerId = stringValue(event.payload.reviewer_id) || reviewerId;
      verdict = stringValue(event.payload.verdict);
      reviewStatus = "succeeded";
    }
    if (event.type === "run.started" || event.type === "run.resumed") runStatus = "running";
    if (event.type === "run.paused" || event.type === "run.escalated") runStatus = "paused";
    if (event.type === "run.completed") runStatus = "succeeded";
    if (event.type === "run.failed") runStatus = "failed";
    if (event.type === "run.cancelled") runStatus = "cancelled";
  }

  return { strategy, goal, workers: [...workers.values()], reviewerId, reviewStatus, verdict, runStatus };
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
