import { FormEvent, useEffect, useState } from 'react'
import {
  createEvaluationCase,
  createEvaluationSuite,
  compareEvaluationRuns,
  deleteEvaluationCase,
  listEvaluationRuns,
  listEvaluationSuites,
  runEvaluationSuite,
} from './api'
import type { Agent, EvaluationRunComparison, EvaluationRunHistory, EvaluationRunV2, EvaluationSuite } from './types'

export default function AgentEvaluationPanel({ agent, onClose, notice }: {
  agent: Agent
  onClose: () => void
  notice: (text: string) => void
}) {
  const [suites, setSuites] = useState<EvaluationSuite[]>([])
  const [selected, setSelected] = useState('')
  const [run, setRun] = useState<EvaluationRunV2 | null>(null)
  const [runs, setRuns] = useState<EvaluationRunHistory[]>([])
  const [baselineRunId, setBaselineRunId] = useState('')
  const [candidateRunId, setCandidateRunId] = useState('')
  const [comparison, setComparison] = useState<EvaluationRunComparison | null>(null)
  const [busy, setBusy] = useState(false)
  const active = suites.find(suite => suite.id === (selected || suites[0]?.id))
  const suiteRuns = runs.filter(item => item.suite_id === active?.id)

  async function reload() {
    const [items, history] = await Promise.all([listEvaluationSuites(agent.id), listEvaluationRuns(agent.id)])
    setSuites(items)
    setRuns(history)
    if (!selected && items[0]) setSelected(items[0].id)
  }

  useEffect(() => {
    void reload().catch(() => notice('加载评测集失败'))
  }, [agent.id])

  async function addSuite() {
    const item = await createEvaluationSuite(agent.id, {
      name: `发布回归 ${suites.length + 1}`,
      pass_threshold: 1,
      is_release_gate: true,
    })
    await reload()
    setSelected(item.id)
  }

  async function addCase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!active) return
    const form = event.currentTarget
    const data = new FormData(form)
    const keywords = String(data.get('keywords') || '').split(',').map(value => value.trim()).filter(Boolean)
    const rubric = String(data.get('rubric') || '').trim()
    const scorers: Record<string, unknown> = {}
    if (keywords.length) scorers.keywords = keywords
    if (rubric) scorers.judge = {
      rubric,
      threshold: Number(data.get('judge_threshold') || 4),
      model: String(data.get('judge_model') || '').trim(),
    }
    await createEvaluationCase(agent.id, active.id, {
      name: String(data.get('name')),
      input_text: String(data.get('input')),
      expected_text: String(data.get('expected') || ''),
      scorers,
      is_key: data.get('is_key') === 'on',
    })
    form.reset()
    await reload()
  }

  async function execute() {
    if (!active) return
    setBusy(true)
    try {
      const result = await runEvaluationSuite(agent.id, active.id)
      setRun(result)
      await reload()
      notice(`评测完成：${result.summary.passed}/${result.summary.total}`)
    } catch (error) {
      notice(error instanceof Error ? error.message : '评测失败')
    } finally {
      setBusy(false)
    }
  }

  async function compareRuns() {
    if (!baselineRunId || !candidateRunId) return notice('请选择两次评测运行')
    if (baselineRunId === candidateRunId) return notice('请选择不同的运行记录')
    try {
      setComparison(await compareEvaluationRuns(agent.id, baselineRunId, candidateRunId))
    } catch (error) {
      notice(error instanceof Error ? error.message : '评测对比失败')
    }
  }

  return <div className="run-drawer-mask" onClick={onClose}>
    <aside className="run-drawer deploy-config-drawer" onClick={event => event.stopPropagation()}>
      <header><strong>效果评测 · {agent.name}</strong><button className="rd-close" onClick={onClose}>×</button></header>
      <div className="deploy-config-body">
        <section className="deploy-config-section">
          <h4>评测集</h4>
          <div className="deploy-visibility-options">
            {suites.map(suite => <button key={suite.id} className={active?.id === suite.id ? 'solid' : ''} onClick={() => { setSelected(suite.id); setRun(null); setComparison(null); setBaselineRunId(''); setCandidateRunId('') }}>{suite.name}</button>)}
            <button onClick={addSuite}>＋ 新建</button>
          </div>
          {active && <p>通过阈值 {Math.round(active.pass_threshold * 100)}% · {active.is_release_gate ? '阻断发布' : '仅观察'}</p>}
        </section>

        {active && <>
          <section className="deploy-config-section">
            <h4>测试样例<small>{active.cases.length} 条</small></h4>
            {active.cases.map(item => <div className="deploy-checkbox" key={item.id}>
              <span><b>{item.name}{item.is_key ? ' · 关键' : ''}</b><small>{item.input_text} · 期望：{item.expected_text || '仅运行成功'}</small></span>
              <button className="danger-link" onClick={async () => { await deleteEvaluationCase(agent.id, active.id, item.id); await reload() }}>删除</button>
            </div>)}
            <form onSubmit={addCase}>
              <label>名称<input name="name" required placeholder="例如：订单查询" /></label>
              <label>输入<textarea name="input" required placeholder="用户问题" /></label>
              <label>期望包含<input name="expected" placeholder="可留空" /></label>
              <label>关键词<input name="keywords" placeholder="逗号分隔" /></label>
              <label>LLM Judge Rubric<textarea name="rubric" placeholder="留空则不启用。例如：回答准确、完整，不能编造事实。" /></label>
              <div className="config-row">
                <label>Judge 通过分<select name="judge_threshold" defaultValue="4"><option value="3">3 / 5</option><option value="4">4 / 5</option><option value="4.5">4.5 / 5</option></select></label>
                <label>Judge 模型<input name="judge_model" placeholder="留空使用默认模型" /></label>
              </div>
              <label className="deploy-toggle-line"><input name="is_key" type="checkbox" />关键样例必须通过</label>
              <button className="solid">添加样例</button>
            </form>
          </section>
          <button className="solid" disabled={busy || !active.cases.length} onClick={execute}>{busy ? '评测运行中…' : '运行评测'}</button>
        </>}

        {run && <section className="deploy-config-section">
          <h4>{run.ok ? '评测通过' : '评测未通过'}<small>{Math.round(run.summary.pass_rate * 100)}%</small></h4>
          {run.results.map(result => <details key={result.case_id} open={!result.ok}>
            <summary>{result.ok ? '✓' : '✗'} {result.name} · {result.elapsed_ms}ms</summary>
            <p>{result.output || result.error}</p>
            {result.scores.map(score => <div key={score.dimension}>{score.passed ? '✓' : '✗'} {score.dimension}：{score.reason}</div>)}
          </details>)}
        </section>}
        {active && <section className="deploy-config-section">
          <h4>运行历史<small>{suiteRuns.length} 次</small></h4>
          {!suiteRuns.length && <p>运行评测后会保存当前版本、得分和逐样例结果。</p>}
          {suiteRuns.map(item => <button className="evaluation-run-history" type="button" key={item.id} onClick={() => setRun(item)}>
            <span>{item.ok ? '通过' : '未通过'} · {Math.round(item.summary.pass_rate * 100)}%</span>
            <small>{item.agent_version_id ? `版本 ${item.agent_version_id.slice(0, 8)}` : '未绑定版本'} · {new Date(item.created_at).toLocaleString()}</small>
          </button>)}
          {suiteRuns.length >= 2 && <div className="evaluation-compare-controls">
            <select value={baselineRunId} onChange={event => setBaselineRunId(event.target.value)}><option value="">基线运行</option>{suiteRuns.map(item => <option key={item.id} value={item.id}>{item.id.slice(0, 8)} · {Math.round(item.summary.pass_rate * 100)}%</option>)}</select>
            <select value={candidateRunId} onChange={event => setCandidateRunId(event.target.value)}><option value="">对比运行</option>{suiteRuns.map(item => <option key={item.id} value={item.id}>{item.id.slice(0, 8)} · {Math.round(item.summary.pass_rate * 100)}%</option>)}</select>
            <button type="button" onClick={compareRuns}>比较</button>
          </div>}
          {comparison && <div className="evaluation-comparison-result">
            <b>通过率 {comparison.deltas.pass_rate == null ? '无变化数据' : `${comparison.deltas.pass_rate >= 0 ? '+' : ''}${Math.round(comparison.deltas.pass_rate * 100)}%`}</b>
            <span>平均耗时 {comparison.deltas.average_latency_ms == null ? '无数据' : `${comparison.deltas.average_latency_ms >= 0 ? '+' : ''}${Math.round(comparison.deltas.average_latency_ms)} ms`}</span>
            <p>{comparison.regressions.length ? `退化：${comparison.regressions.map(item => item.name).join('、')}` : '没有发现退化样例'}</p>
            <p>{comparison.improvements.length ? `改进：${comparison.improvements.map(item => item.name).join('、')}` : '没有新增通过样例'}</p>
          </div>}
        </section>}
      </div>
    </aside>
  </div>
}
