"use client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Loader2, CheckCircle2, Circle, ArrowRight, ExternalLink } from "lucide-react";
import PlanFileViewer, { type PlanFileItem } from "./PlanFileViewer";
import PlanFirstLoop from "./PlanFirstLoop";
import PlanProgress from "./PlanProgress";
import RunEventTimeline from "./RunEventTimeline";
import type { RunEvent, ProposalResult, DraftAgent, CompileResult, PlanState } from "@/lib/planner-session";

interface ProposalNode {
  id: string;
  type: string;
  config: Record<string, unknown>;
  description?: string;
}

interface Proposal {
  architecture_summary: string;
  nodes?: ProposalNode[];
  edges?: Array<{ source: string; target: string; targetHandle?: string }>;
  rationale: string;
  tuning_hints?: string[];
  capabilities_to_create?: string[];
}

type PlannerStage = "clarifying" | "drafting" | "awaiting_confirmation" | "ready_to_apply" | "applied";

interface PlannerMemory {
  requirement_summary: string;
  confirmed_constraints: string[];
  task_classification: string;
  latest_proposal_summary: string;
  user_feedback: string[];
}

interface Props {
  stage: PlannerStage;
  stageInfo: { label: string; desc: string };
  proposal: Proposal | null;
  onApply: () => void;
  applying: boolean;
  memory?: PlannerMemory | null;
  conversationId?: string | null;
  files?: PlanFileItem[];
  finalSummary?: string | null;
  linkedAgentId?: number | null;
  onOpenDag?: () => void;
  // Plan-first builder loop (T7)
  runEvents?: RunEvent[];
  proposalResult?: ProposalResult | null;
  draftAgent?: DraftAgent | null;
  compileResult?: CompileResult | null;
  onConfirmProposal?: (proposalId: number) => void;
  onCompileDraft?: (draftAgentId: number) => void;
  // Plan+Loop mode (planner-plan-loop-refactor)
  planState?: PlanState | null;
  platformSkills?: Array<{ id: number; name: string; description: string }>;
  platformConnectors?: Array<{ id: number; name: string; description: string; connected: boolean }>;
  selectedPlatformCapabilityIds?: number[];
  onTogglePlatformCapability?: (id: number) => void;
  onOpenCapabilities?: () => void;
}

const NODE_TYPE_LABELS: Record<string, string> = {
  agent: "智能体", p: "提示词", m: "模型",
  k: "知识库", t: "工具", mem: "记忆",
  i: "输入", o: "输出", c: "条件", x: "代码", h: "HTTP",
};

const STAGES: PlannerStage[] = ["clarifying", "drafting", "awaiting_confirmation", "ready_to_apply"];
const STAGE_SHORT: Record<PlannerStage, string> = {
  clarifying: "澄清需求",
  drafting: "生成方案",
  awaiting_confirmation: "确认方案",
  ready_to_apply: "创建",
  applied: "已应用",
};

export default function PlannerPanel({ stage, stageInfo, proposal, onApply, applying, memory, conversationId, files, finalSummary, linkedAgentId, onOpenDag, proposalResult, draftAgent, compileResult, onConfirmProposal, onCompileDraft, planState, platformSkills = [], platformConnectors = [], selectedPlatformCapabilityIds = [], onTogglePlatformCapability, onOpenCapabilities }: Props) {
  const currentIdx = STAGES.indexOf(stage);
  const showOpenDag = stage === "applied" && linkedAgentId != null;

  return (
    <div className="p-4 space-y-4">
      {/* Applied session: direct entry to the linked DAG / workbench page */}
      {showOpenDag && (
        <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-3 space-y-2">
          <p className="text-xs text-muted-foreground">
            该会话已应用为智能体 <span className="font-medium text-emerald-600 dark:text-emerald-400">#{linkedAgentId}</span>
          </p>
          <Button
            className="w-full rounded-xl"
            variant="outline"
            onClick={onOpenDag}
          >
            <ExternalLink className="w-4 h-4 mr-2 shrink-0" />
            <span>打开 DAG / 查看工作台</span>
          </Button>
        </div>
      )}

      {/* Stage Progress */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">规划进度</h3>
        <div className="flex items-center gap-1">
          {STAGES.map((s, i) => (
            <div key={s} className="flex items-center gap-1">
              <div className={`flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium transition-colors ${
                i < currentIdx ? "bg-primary/10 text-primary" :
                i === currentIdx ? "bg-primary text-primary-foreground" :
                "bg-muted text-muted-foreground"
              }`}>
                {i < currentIdx
                  ? <CheckCircle2 key="done" className="w-2.5 h-2.5 shrink-0" />
                  : <Circle key="todo" className="w-2.5 h-2.5 shrink-0" />}
                <span>{STAGE_SHORT[s]}</span>
              </div>
              {i < STAGES.length - 1 && <ArrowRight className="w-2.5 h-2.5 text-muted-foreground/40" />}
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">{stageInfo.desc}</p>
      </div>

      {(platformSkills.length > 0 || platformConnectors.length > 0) && (
        <Section title="已接入平台能力">
          <p className="text-[10px] leading-relaxed text-muted-foreground">按需添加到本次规划；未选择的能力不会注入规划上下文。</p>
          {platformSkills.length > 0 && (
            <div className="space-y-1.5">
              <p className="text-[10px] font-medium text-muted-foreground">Skills · {platformSkills.length}</p>
              {platformSkills.slice(0, 4).map((skill) => {
                const selected = selectedPlatformCapabilityIds.includes(skill.id);
                return <div key={skill.id} className="flex gap-2 rounded-lg border border-border/60 bg-muted/30 px-2.5 py-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-medium truncate">{skill.name}</p>
                    {skill.description && <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground line-clamp-2">{skill.description}</p>}
                  </div>
                  <button
                    type="button"
                    onClick={() => onTogglePlatformCapability?.(skill.id)}
                    className={selected ? "h-6 shrink-0 rounded-md bg-primary px-2 text-[10px] text-primary-foreground" : "h-6 shrink-0 rounded-md border border-border bg-background px-2 text-[10px] hover:bg-muted"}
                  >
                    {selected ? "已添加" : "添加"}
                  </button>
                </div>
              })}
            </div>
          )}
          {platformConnectors.length > 0 && (
            <div className="mt-3 space-y-1.5">
              <p className="text-[10px] font-medium text-muted-foreground">连接器 · {platformConnectors.length}</p>
              <div className="flex flex-wrap gap-1">
                {platformConnectors.map((connector) => {
                  const selected = selectedPlatformCapabilityIds.includes(connector.id);
                  return <button key={connector.id} type="button" onClick={() => onTogglePlatformCapability?.(connector.id)} className={selected ? "inline-flex max-w-full items-center gap-1 rounded-md border border-primary bg-primary/10 px-2 py-1 text-[10px] text-primary" : "inline-flex max-w-full items-center gap-1 rounded-md border border-border px-2 py-1 text-[10px] hover:bg-muted"}>
                    <span className={connector.connected ? "h-1.5 w-1.5 rounded-full bg-emerald-500" : "h-1.5 w-1.5 rounded-full bg-muted-foreground/40"} />
                    <span className="truncate">{connector.name}</span>
                    <span>{selected ? "已添加" : "+"}</span>
                  </button>;
                })}
              </div>
            </div>
          )}
          {onOpenCapabilities && (
            <Button variant="outline" size="sm" className="mt-3 h-7 w-full text-xs" onClick={onOpenCapabilities}>
              管理能力库
            </Button>
          )}
        </Section>
      )}

      {/* Memory: Requirement Summary & Constraints */}
      {memory && (memory.requirement_summary || memory.confirmed_constraints.length > 0 || memory.task_classification) && (
        <div className="space-y-3">
          {memory.requirement_summary && (
            <Section title="需求摘要">
              <p className="text-xs text-muted-foreground leading-relaxed">{memory.requirement_summary}</p>
            </Section>
          )}
          {memory.task_classification && (
            <Section title="任务分型">
              <Badge variant="outline" className="text-[10px]">{memory.task_classification}</Badge>
            </Section>
          )}
          {memory.confirmed_constraints.length > 0 && (
            <Section title="已确认约束">
              <ul className="space-y-0.5">
                {memory.confirmed_constraints.map((c, i) => (
                  <li key={i} className="text-xs text-muted-foreground flex gap-1.5">
                    <span className="text-green-600 shrink-0">✓</span>{c}
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </div>
      )}

      {/* Final summary (compact, written to final.md) */}
      {finalSummary && (
        <Section title="本次交付">
          <p className="text-xs text-muted-foreground leading-relaxed whitespace-pre-wrap">{finalSummary}</p>
        </Section>
      )}

      {/* Plan-with-file: stable artifacts on disk, opened on demand */}
      {files && files.length > 0 && (
        <PlanFileViewer conversationId={conversationId || null} files={files} />
      )}

      {/* Empty state */}
      {!proposal && !memory?.requirement_summary && (
        <div className="rounded-xl border border-dashed border-border/60 p-4 text-center space-y-2">
          <p className="text-xs text-muted-foreground">
            {stage === "clarifying" && "正在收集需求信息，方案将在澄清完成后生成"}
            {stage === "drafting" && "正在生成架构方案..."}
            {stage === "awaiting_confirmation" && "请在对话中确认方案"}
          </p>
        </div>
      )}

      {/* Proposal content */}
      {proposal && (
        <>
          {/* Architecture Summary */}
          <Section title="架构摘要">
            <p className="text-sm">{proposal.architecture_summary}</p>
          </Section>

          {/* Node Structure */}
          {(proposal.nodes?.length ?? 0) > 0 && (
            <Section title="节点组成">
              <div className="space-y-1.5">
                {(proposal.nodes ?? []).map((node) => (
                  <div key={node.id} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-muted/40 border border-border/40">
                    <Badge variant="secondary" className="text-[10px] px-1.5 py-0 h-4 shrink-0">
                      {NODE_TYPE_LABELS[node.type] || node.type}
                    </Badge>
                    <span className="text-xs truncate">{node.description || node.id}</span>
                  </div>
                ))}
              </div>
              {(proposal.edges?.length ?? 0) > 0 && (
                <div className="mt-2 text-[10px] text-muted-foreground">
                  {proposal.edges?.length} 条连接
                </div>
              )}
            </Section>
          )}

          {/* Capabilities that need to be created before apply */}
          {(proposal.capabilities_to_create?.length ?? 0) > 0 && (
            <Section title="待创建能力">
              <div className="space-y-1.5">
                {(proposal.capabilities_to_create ?? []).map((name) => (
                  <div key={name} className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-amber-50 dark:bg-amber-950/30 border border-amber-200/60 dark:border-amber-800/40">
                    <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-4 shrink-0 border-amber-400 text-amber-700 dark:text-amber-300">待创建</Badge>
                    <span className="text-xs truncate">{name}</span>
                  </div>
                ))}
              </div>
              <p className="mt-1.5 text-[10px] text-muted-foreground">这些能力当前不在能力库中，创建到工作台后需先补齐其配置。</p>
            </Section>
          )}

          {/* Rationale */}
          <Section title="推荐理由">
            <p className="text-xs text-muted-foreground leading-relaxed">{proposal.rationale}</p>
          </Section>

          {/* Tuning Hints */}
          {proposal.tuning_hints && proposal.tuning_hints.length > 0 && (
            <Section title="调优建议">
              <ul className="space-y-1">
                {proposal.tuning_hints.map((hint, i) => (
                  <li key={i} className="text-xs text-muted-foreground flex gap-1.5">
                    <span className="text-primary shrink-0">•</span>
                    {hint}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {/* Action */}
          <div className="pt-2">
            <Button
              className="w-full rounded-xl"
              onClick={onApply}
              disabled={applying || stage !== "ready_to_apply"}
            >
              {applying && <Loader2 className="w-4 h-4 mr-2 animate-spin shrink-0" />}
              <span>{stage === "ready_to_apply" ? "创建到工作台" : "等待方案确认..."}</span>
            </Button>
          </div>

          {/* Plan-first loop (T7): confirm → draft → compile, alongside the
              legacy direct-apply button above. */}
          {onConfirmProposal && onCompileDraft && proposalResult && (
            <PlanFirstLoop
              proposalId={proposalResult.id}
              proposalStatus={proposalResult.status}
              draftAgent={draftAgent ?? null}
              compileResult={compileResult ?? null}
              onConfirm={onConfirmProposal}
              onCompile={onCompileDraft}
            />
          )}

          {/* Plan+Loop mode: show Plan Progress while plan is active (not completed) */}
          {planState && planState.status !== "completed" && (
            <PlanProgress planState={planState} />
          )}

          {/* Context-used explanation (T7). The step-by-step run-event timeline
              now renders inline in the conversation (deerflow page); here we only
              surface "本次用了哪些上下文" tied to the finished proposal. */}
          {!planState && (
            <RunEventTimeline
              runEvents={[]}
              contextUsed={
                (proposalResult?.proposal?.context_used_explanation as string[] | undefined) ?? undefined
              }
            />
          )}
        </>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <h4 className="text-xs font-semibold text-foreground/80">{title}</h4>
      {children}
    </div>
  );
}
