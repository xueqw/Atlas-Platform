"use client";
import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Plus, Play, CheckCircle, XCircle, Trash2, Loader2, GitCompare, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import type {
  EvaluationSuiteItem, EvaluationCaseItem, EvaluationRunItem,
  EvaluationRunDetail, EvaluationCompareResult,
} from "@/lib/types";

interface Props {
  agentId: number;
  onClose: () => void;
}

function parseSummary(summary: string): Record<string, number | null> {
  try { return JSON.parse(summary || "{}"); } catch { return {}; }
}

function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${Math.round(v * 100)}%`;
}

export default function EvaluationSuitePanel({ agentId, onClose }: Props) {
  const [suites, setSuites] = useState<EvaluationSuiteItem[]>([]);
  const [activeSuite, setActiveSuite] = useState<number | null>(null);
  const [cases, setCases] = useState<EvaluationCaseItem[]>([]);
  const [runs, setRuns] = useState<EvaluationRunItem[]>([]);
  const [runDetail, setRunDetail] = useState<EvaluationRunDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [compare, setCompare] = useState<EvaluationCompareResult | null>(null);
  const [baselineId, setBaselineId] = useState<string>("");
  const [candidateId, setCandidateId] = useState<string>("");

  // dialogs
  const [suiteDialog, setSuiteDialog] = useState(false);
  const [suiteName, setSuiteName] = useState("");
  const [suiteDesc, setSuiteDesc] = useState("");
  const [suiteType, setSuiteType] = useState("general");
  const [caseDialog, setCaseDialog] = useState(false);
  const [caseName, setCaseName] = useState("");
  const [caseInput, setCaseInput] = useState("");
  const [caseKeywords, setCaseKeywords] = useState("");
  const [caseJudge, setCaseJudge] = useState("");
  const [caseKey, setCaseKey] = useState(false);

  const loadSuites = useCallback(() => {
    api.listSuites(agentId).then((s) => {
      setSuites(s);
      if (s.length > 0 && activeSuite == null) setActiveSuite(s[0].id);
    }).catch(() => {});
  }, [agentId, activeSuite]);

  useEffect(() => { loadSuites(); }, [loadSuites]);

  const loadSuiteData = useCallback((suiteId: number) => {
    api.listSuiteCases(agentId, suiteId).then(setCases).catch(() => setCases([]));
    api.listEvaluationRuns(agentId, suiteId).then(setRuns).catch(() => setRuns([]));
    setRunDetail(null);
    setCompare(null);
    setBaselineId("");
    setCandidateId("");
  }, [agentId]);

  useEffect(() => {
    if (activeSuite != null) loadSuiteData(activeSuite);
  }, [activeSuite, loadSuiteData]);

  const handleCreateSuite = async () => {
    if (!suiteName.trim()) { toast.error("套件名称不能为空"); return; }
    try {
      // suite_type drives the default dimension template the backend pre-fills.
      const created = await api.createSuite(agentId, {
        name: suiteName, description: suiteDesc, suite_type: suiteType,
      });
      setSuiteDialog(false);
      setSuiteName(""); setSuiteDesc(""); setSuiteType("general");
      await loadSuites();
      setActiveSuite(created.id);
      toast.success("套件已创建");
    } catch (e) { toast.error((e as Error).message || "创建失败"); }
  };

  const handleDeleteSuite = async (id: number) => {
    try {
      await api.deleteSuite(agentId, id);
      const remaining = suites.filter((s) => s.id !== id);
      setSuites(remaining);
      setActiveSuite(remaining[0]?.id ?? null);
      toast.success("套件已删除");
    } catch (e) { toast.error((e as Error).message || "删除失败"); }
  };

  const handleCreateCase = async () => {
    if (activeSuite == null) return;
    if (!caseName.trim() || !caseInput.trim()) { toast.error("名称和输入不能为空"); return; }
    try {
      await api.createSuiteCase(agentId, {
        suite_id: activeSuite,
        name: caseName,
        input_message: caseInput,
        expected_keywords: JSON.stringify(caseKeywords ? caseKeywords.split(",").map((s) => s.trim()).filter(Boolean) : []),
        judge_prompt: caseJudge,
        is_key: caseKey,
      });
      setCaseDialog(false);
      setCaseName(""); setCaseInput(""); setCaseKeywords(""); setCaseJudge(""); setCaseKey(false);
      loadSuiteData(activeSuite);
      loadSuites();
      toast.success("用例已添加");
    } catch (e) { toast.error((e as Error).message || "添加失败"); }
  };

  const handleRun = async () => {
    if (activeSuite == null) return;
    setRunning(true);
    try {
      await api.startEvaluationRun(agentId, activeSuite);
      const updated = await api.listEvaluationRuns(agentId, activeSuite);
      setRuns(updated);
      if (updated[0]) {
        const detail = await api.getEvaluationRun(agentId, updated[0].id);
        setRunDetail(detail);
      }
      toast.success("评估完成");
    } catch (e) { toast.error((e as Error).message || "评估失败"); }
    setRunning(false);
  };

  const handleViewRun = async (runId: number) => {
    try {
      const detail = await api.getEvaluationRun(agentId, runId);
      setRunDetail(detail);
      setCompare(null);
    } catch (e) { toast.error((e as Error).message || "加载失败"); }
  };

  const handleCompare = async () => {
    if (!baselineId || !candidateId) { toast.error("请选择两个运行"); return; }
    if (baselineId === candidateId) { toast.error("请选择不同的两个运行"); return; }
    try {
      const result = await api.compareEvaluationRuns(agentId, Number(baselineId), Number(candidateId));
      setCompare(result);
      setRunDetail(null);
    } catch (e) { toast.error((e as Error).message || "对比失败"); }
  };

  // PLACEHOLDER_RENDER
  const summary = runDetail ? parseSummary(runDetail.summary) : null;

  return (
    <div className="fixed inset-0 z-50 bg-background/95 flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b shrink-0">
        <h2 className="text-lg font-semibold">评估与回归</h2>
        <Button variant="ghost" size="sm" onClick={onClose}>关闭</Button>
      </div>

      <div className="flex flex-1 min-h-0">
        {/* Left: suites */}
        <div className="w-[220px] shrink-0 border-r flex flex-col">
          <div className="flex items-center justify-between p-3 border-b">
            <span className="text-sm font-medium">套件</span>
            <Button variant="ghost" size="icon" onClick={() => setSuiteDialog(true)}>
              <Plus className="w-4 h-4" />
            </Button>
          </div>
          <ScrollArea className="flex-1">
            {suites.length === 0 ? (
              <p className="text-xs text-muted-foreground p-3">暂无套件</p>
            ) : suites.map((s) => (
              <button
                key={s.id}
                onClick={() => setActiveSuite(s.id)}
                className={`w-full text-left px-3 py-2 text-sm border-b hover:bg-muted/50 flex items-center justify-between group ${activeSuite === s.id ? "bg-muted" : ""}`}
              >
                <span className="truncate">
                  {s.name}
                  <span className="text-xs text-muted-foreground ml-1">({s.case_count})</span>
                </span>
                <Trash2
                  className="w-3.5 h-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 shrink-0"
                  onClick={(e) => { e.stopPropagation(); handleDeleteSuite(s.id); }}
                />
              </button>
            ))}
          </ScrollArea>
        </div>

        {/* Right: cases + runs + results */}
        <div className="flex-1 min-w-0 overflow-y-auto p-4 space-y-6">
          {activeSuite == null ? (
            <p className="text-sm text-muted-foreground text-center py-12">请先创建或选择一个套件</p>
          ) : (
            <>
              {/* Cases */}
              <section className="space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="font-medium text-sm">用例 ({cases.length})</h3>
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" onClick={() => setCaseDialog(true)}>
                      <Plus className="w-4 h-4 mr-1" />添加用例
                    </Button>
                    <Button size="sm" onClick={handleRun} disabled={running || cases.length === 0}>
                      {running ? <Loader2 className="w-4 h-4 mr-1 animate-spin" /> : <Play className="w-4 h-4 mr-1" />}
                      {running ? "评估中..." : "运行评估"}
                    </Button>
                  </div>
                </div>
                {cases.length === 0 ? (
                  <p className="text-xs text-muted-foreground">暂无用例，点击"添加用例"创建</p>
                ) : (
                  <div className="space-y-1">
                    {cases.map((c) => (
                      <div key={c.id} className="flex items-center gap-2 text-sm border rounded px-3 py-2">
                        <span className="font-medium">{c.name}</span>
                        {c.is_key && <Badge variant="secondary" className="text-xs">关键</Badge>}
                        <span className="text-muted-foreground text-xs truncate flex-1">{c.input_message}</span>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              {/* SECTION_RUNS */}
              {/* Runs list + compare picker */}
              <section className="space-y-2">
                <h3 className="font-medium text-sm">运行历史 ({runs.length})</h3>
                {runs.length === 0 ? (
                  <p className="text-xs text-muted-foreground">还没有评估运行</p>
                ) : (
                  <>
                    <div className="space-y-1">
                      {runs.map((r) => {
                        const s = parseSummary(r.summary);
                        return (
                          <button
                            key={r.id}
                            onClick={() => handleViewRun(r.id)}
                            className="w-full text-left flex items-center gap-3 text-sm border rounded px-3 py-2 hover:bg-muted/50"
                          >
                            {r.passed ? <CheckCircle className="w-4 h-4 text-green-500 shrink-0" /> : <XCircle className="w-4 h-4 text-red-500 shrink-0" />}
                            <span className="font-mono text-xs">#{r.id}</span>
                            <Badge variant="outline" className="text-xs">DAG v{r.dag_version}</Badge>
                            <Badge variant="outline" className="text-xs">P v{r.prompt_version}</Badge>
                            {r.model && <Badge variant="outline" className="text-xs">{r.model}</Badge>}
                            <span className="text-muted-foreground text-xs">通过率 {pct(s.pass_rate as number)}</span>
                            {s.key_pass_rate != null && <span className="text-muted-foreground text-xs">关键 {pct(s.key_pass_rate as number)}</span>}
                            <span className="text-muted-foreground text-xs ml-auto">{new Date(r.created_at).toLocaleString("zh-CN")}</span>
                          </button>
                        );
                      })}
                    </div>

                    {/* Compare picker */}
                    <div className="flex items-end gap-2 pt-2">
                      <div className="flex-1">
                        <Label className="text-xs">基线运行</Label>
                        <Select value={baselineId} onValueChange={(v) => setBaselineId(v ?? "")}>
                          <SelectTrigger><SelectValue placeholder="选择基线" /></SelectTrigger>
                          <SelectContent>
                            {runs.map((r) => <SelectItem key={r.id} value={String(r.id)}>#{r.id} (DAG v{r.dag_version} / P v{r.prompt_version})</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="flex-1">
                        <Label className="text-xs">对比运行</Label>
                        <Select value={candidateId} onValueChange={(v) => setCandidateId(v ?? "")}>
                          <SelectTrigger><SelectValue placeholder="选择对比" /></SelectTrigger>
                          <SelectContent>
                            {runs.map((r) => <SelectItem key={r.id} value={String(r.id)}>#{r.id} (DAG v{r.dag_version} / P v{r.prompt_version})</SelectItem>)}
                          </SelectContent>
                        </Select>
                      </div>
                      <Button size="sm" variant="outline" onClick={handleCompare}>
                        <GitCompare className="w-4 h-4 mr-1" />对比
                      </Button>
                    </div>
                  </>
                )}
              </section>

              {/* Run detail */}
              {runDetail && summary && (
                <section className="space-y-2 border-t pt-4">
                  <h3 className="font-medium text-sm">运行 #{runDetail.id} 详情</h3>
                  <div className="flex flex-wrap gap-2 text-xs">
                    <Badge variant="outline">总用例 {String(summary.total_cases ?? 0)}</Badge>
                    <Badge variant="outline">通过率 {pct(summary.pass_rate as number)}</Badge>
                    {summary.key_pass_rate != null && <Badge variant="outline">关键通过率 {pct(summary.key_pass_rate as number)}</Badge>}
                    <Badge variant="outline">平均分 {summary.avg_score != null ? (summary.avg_score as number).toFixed(2) : "—"}</Badge>
                    <Badge variant="outline">平均延迟 {String(summary.latency_avg_ms ?? 0)}ms</Badge>
                    <Badge variant="outline">Token 入/出 {String(summary.token_input ?? 0)}/{String(summary.token_output ?? 0)}</Badge>
                  </div>
                  <div className="space-y-2">
                    {runDetail.case_results.map((cr) => (
                      <div key={cr.id} className="border rounded px-3 py-2 space-y-1.5">
                        <div className="flex items-center gap-2 text-sm">
                          {cr.passed ? <CheckCircle className="w-4 h-4 text-green-500 shrink-0" /> : <XCircle className="w-4 h-4 text-red-500 shrink-0" />}
                          <span className="font-medium">{cr.case_name}</span>
                          {cr.is_key && <Badge variant="secondary" className="text-xs">关键</Badge>}
                          <span className="text-muted-foreground text-xs truncate flex-1">{cr.output}</span>
                          {(cr.langfuse_score_count ?? cr.langfuse_score_ids?.length ?? 0) > 0 ? (
                            <Badge variant="outline" className="text-[10px] px-1 py-0 shrink-0">
                              已写 {cr.langfuse_score_count ?? cr.langfuse_score_ids?.length} 条 score
                            </Badge>
                          ) : (
                            <Badge variant="secondary" className="text-[10px] px-1 py-0 shrink-0">未写 score</Badge>
                          )}
                          {cr.trace_url && (
                            <a
                              href={cr.trace_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-xs text-blue-500 hover:underline flex items-center gap-0.5 shrink-0"
                              onClick={(e) => e.stopPropagation()}
                            >
                              trace <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                        {cr.dimension_results && cr.dimension_results.length > 0 ? (
                          <div className="flex flex-col gap-1 pl-6">
                            {cr.dimension_results.map((d, i) => (
                              <div key={`${cr.id}-${d.dimension}-${i}`} className="flex items-start gap-2 text-xs">
                                {d.skipped
                                  ? <span className="w-3.5 h-3.5 shrink-0 text-muted-foreground">—</span>
                                  : d.passed
                                    ? <CheckCircle className="w-3.5 h-3.5 text-green-500 shrink-0 mt-0.5" />
                                    : <XCircle className="w-3.5 h-3.5 text-red-500 shrink-0 mt-0.5" />}
                                <span className="font-medium">{d.dimension}</span>
                                <Badge variant="outline" className="text-[10px] px-1 py-0">{d.type}</Badge>
                                {d.required && <Badge variant="destructive" className="text-[10px] px-1 py-0">必过</Badge>}
                                {d.skipped
                                  ? <span className="text-muted-foreground">已跳过</span>
                                  : <span className="font-mono text-muted-foreground">{d.score.toFixed(2)} / 阈值 {d.threshold.toFixed(2)}</span>}
                                {d.reason && <span className="text-muted-foreground truncate flex-1">· {d.reason}</span>}
                              </div>
                            ))}
                          </div>
                        ) : (
                          <span className="text-muted-foreground text-xs font-mono pl-6">{cr.scores}</span>
                        )}
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {/* Comparison result */}
              {compare && (
                <section className="space-y-3 border-t pt-4">
                  <h3 className="font-medium text-sm">版本对比</h3>
                  <div className="flex gap-4 text-xs">
                    <div className="flex-1 border rounded p-2">
                      <p className="font-medium mb-1">基线 #{compare.baseline.run_id}</p>
                      <p className="text-muted-foreground">DAG v{compare.baseline.dag_version} · P v{compare.baseline.prompt_version} · {compare.baseline.model || "—"}</p>
                    </div>
                    <div className="flex-1 border rounded p-2">
                      <p className="font-medium mb-1">对比 #{compare.candidate.run_id}</p>
                      <p className="text-muted-foreground">DAG v{compare.candidate.dag_version} · P v{compare.candidate.prompt_version} · {compare.candidate.model || "—"}</p>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2 text-xs">
                    <DeltaBadge label="平均分" value={compare.deltas.avg_score} />
                    <DeltaBadge label="通过率" value={compare.deltas.pass_rate} isPct />
                    <DeltaBadge label="关键通过率" value={compare.deltas.key_pass_rate} isPct />
                    <DeltaBadge label="延迟ms" value={compare.deltas.latency_avg_ms} invert />
                  </div>
                  <div>
                    <p className="text-sm font-medium mb-1 text-red-500">回归用例 ({compare.regressions.length})</p>
                    {compare.regressions.length === 0 ? (
                      <p className="text-xs text-muted-foreground">无回归</p>
                    ) : (
                      <div className="space-y-1">
                        {compare.regressions.map((r) => (
                          <div key={r.case_id} className="flex items-center gap-2 text-sm border border-red-200 rounded px-3 py-2">
                            <XCircle className="w-4 h-4 text-red-500 shrink-0" />
                            <span>{r.case_name}</span>
                            {r.is_key && <Badge variant="destructive" className="text-xs">关键</Badge>}
                            <span className="text-xs text-muted-foreground ml-auto">通过 → 未通过</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  {compare.improvements.length > 0 && (
                    <div>
                      <p className="text-sm font-medium mb-1 text-green-600">改善用例 ({compare.improvements.length})</p>
                      <div className="space-y-1">
                        {compare.improvements.map((r) => (
                          <div key={r.case_id} className="flex items-center gap-2 text-sm border border-green-200 rounded px-3 py-2">
                            <CheckCircle className="w-4 h-4 text-green-500 shrink-0" />
                            <span>{r.case_name}</span>
                            <span className="text-xs text-muted-foreground ml-auto">未通过 → 通过</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </div>

      {/* DIALOGS */}
      <Dialog open={suiteDialog} onOpenChange={setSuiteDialog}>
        <DialogContent>
          <DialogHeader><DialogTitle>新建套件</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label>名称</Label><Input value={suiteName} onChange={(e) => setSuiteName(e.target.value)} placeholder="套件名称" /></div>
            <div>
              <Label>套件类型</Label>
              <Select value={suiteType} onValueChange={(v) => setSuiteType(v ?? "general")}>
                <SelectTrigger><SelectValue placeholder="选择类型" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="general">通用 (general)</SelectItem>
                  <SelectItem value="planner">规划 (planner)</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground mt-1">选择类型会注入对应的默认维度模板。</p>
            </div>
            <div><Label>描述</Label><Textarea value={suiteDesc} onChange={(e) => setSuiteDesc(e.target.value)} rows={2} placeholder="可选" /></div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSuiteDialog(false)}>取消</Button>
            <Button onClick={handleCreateSuite}>创建</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={caseDialog} onOpenChange={setCaseDialog}>
        <DialogContent>
          <DialogHeader><DialogTitle>添加用例</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <div><Label>名称</Label><Input value={caseName} onChange={(e) => setCaseName(e.target.value)} placeholder="用例名称" /></div>
            <div><Label>输入消息</Label><Textarea value={caseInput} onChange={(e) => setCaseInput(e.target.value)} rows={3} placeholder="用户输入" /></div>
            <div><Label>期望关键词（逗号分隔）</Label><Input value={caseKeywords} onChange={(e) => setCaseKeywords(e.target.value)} placeholder="关键词1, 关键词2" /></div>
            <div><Label>评分标准（LLM-as-judge，可选）</Label><Textarea value={caseJudge} onChange={(e) => setCaseJudge(e.target.value)} rows={2} placeholder="例如：回答应礼貌且包含具体步骤" /></div>
            <button type="button" onClick={() => setCaseKey((v) => !v)} className="flex items-center gap-2 text-sm">
              <span className={`w-4 h-4 rounded border flex items-center justify-center ${caseKey ? "bg-primary border-primary" : "border-muted-foreground"}`}>
                {caseKey && <CheckCircle className="w-3 h-3 text-primary-foreground" />}
              </span>
              标记为关键用例
            </button>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCaseDialog(false)}>取消</Button>
            <Button onClick={handleCreateCase}>添加</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function DeltaBadge({ label, value, isPct, invert }: { label: string; value: number | null; isPct?: boolean; invert?: boolean }) {
  if (value == null) return <Badge variant="outline">{label} —</Badge>;
  const display = isPct ? `${value > 0 ? "+" : ""}${Math.round(value * 100)}%` : `${value > 0 ? "+" : ""}${value}`;
  // For most metrics positive is good; for latency (invert) negative is good.
  const good = invert ? value < 0 : value > 0;
  const neutral = value === 0;
  const cls = neutral ? "" : good ? "text-green-600 border-green-300" : "text-red-600 border-red-300";
  return <Badge variant="outline" className={cls}>{label} {display}</Badge>;
}
