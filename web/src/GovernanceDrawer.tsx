import { FormEvent, useEffect, useState } from 'react'
import { createGovernedMemory, deleteGovernedMemory, discoverSkills, listAgents, listGovernedMemories } from './api'
import type { Agent, GovernedMemory, SkillDiscovery } from './types'

export default function GovernanceDrawer({ agent, onClose, notice }: { agent?: Agent; onClose: () => void; notice: (text: string) => void }) {
  const [tab, setTab] = useState<'memory' | 'skills' | 'team'>('memory')
  const [memory, setMemory] = useState<GovernedMemory | null>(null)
  const [discovery, setDiscovery] = useState<SkillDiscovery | null>(null)
  const [loading, setLoading] = useState(false)
  const [draft, setDraft] = useState({ subject: '', predicate: '偏好', object_value: '', evidence: '' })
  const refresh = async () => { setLoading(true); try { const [m, s] = await Promise.all([agent ? listGovernedMemories(agent.id) : Promise.resolve(null), discoverSkills()]); setMemory(m); setDiscovery(s) } catch (e) { notice(e instanceof Error ? e.message : '治理数据加载失败') } finally { setLoading(false) } }
  useEffect(() => { refresh() }, [agent?.id])
  async function add(event: FormEvent) { event.preventDefault(); if (!agent || !draft.subject.trim() || !draft.object_value.trim()) return; try { await createGovernedMemory(agent.id, draft); setDraft({ subject: '', predicate: '偏好', object_value: '', evidence: '' }); await refresh(); notice('已写入治理记忆') } catch (e) { notice(e instanceof Error ? e.message : '写入失败') } }
  async function remove(id: string) { if (!agent) return; try { await deleteGovernedMemory(agent.id, id); await refresh(); notice('已追加删除记录') } catch (e) { notice(e instanceof Error ? e.message : '删除失败') } }
  return <div className="governance-mask" onClick={onClose}><aside className="governance-drawer" onClick={e => e.stopPropagation()}>
    <header><div><small>WORKBENCH GOVERNANCE</small><strong>运行治理</strong></div><button title="关闭治理面板" onClick={onClose}>×</button></header>
    <div className="governance-agent"><i>{agent ? '●' : '○'}</i><span><b>{agent?.name || '未选择智能体'}</b><small>{agent ? '治理范围按当前智能体隔离' : '从输入框选择一个智能体后可管理其记忆'}</small></span></div>
    <nav className="governance-tabs"><button className={tab === 'memory' ? 'on' : ''} onClick={() => setTab('memory')}>记忆</button><button className={tab === 'skills' ? 'on' : ''} onClick={() => setTab('skills')}>Skills</button><button className={tab === 'team' ? 'on' : ''} onClick={() => setTab('team')}>团队</button></nav>
    {loading ? <p className="governance-empty">正在同步治理数据…</p> : tab === 'memory' ? <div className="governance-body">{!agent ? <p className="governance-empty">选择智能体后可管理它的受治理记忆。</p> : <><div className="governance-note"><b>非可信上下文</b><span>事实保留来源与时间，不会覆盖系统规则。</span></div><form className="memory-form" onSubmit={add}><input value={draft.subject} onChange={e => setDraft({ ...draft, subject: e.target.value })} placeholder="主体，例如：客户 A"/><input value={draft.predicate} onChange={e => setDraft({ ...draft, predicate: e.target.value })} placeholder="关系，例如：偏好"/><textarea value={draft.object_value} onChange={e => setDraft({ ...draft, object_value: e.target.value })} placeholder="记忆内容"/><input value={draft.evidence} onChange={e => setDraft({ ...draft, evidence: e.target.value })} placeholder="证据或来源（可选）"/><button disabled={!draft.subject.trim() || !draft.object_value.trim()}>添加事实</button></form><div className="memory-list">{memory?.facts.length ? memory.facts.map(f => <article key={f.fact_id}><div><b>{f.statement}</b><small>{f.evidence || '未提供证据'} · 置信度 {Math.round(f.confidence * 100)}%</small><em>{new Date(f.transaction_from).toLocaleString()}</em></div><button title="追加 tombstone 删除记录" onClick={() => remove(f.fact_id)}>×</button></article>) : <p className="governance-empty">还没有可用的记忆事实。</p>}</div></>}</div> : tab === 'skills' ? <div className="governance-body skill-discovery">{discovery?.skills.length ? discovery.skills.map(s => <article key={s.id}><span>{s.category_path.join(' / ')}</span><b>{s.name}</b><small>{s.summary || '暂无摘要'}</small>{s.do_not_use_when?.length ? <em>不用于：{s.do_not_use_when.join('、')}</em> : null}</article>) : <p className="governance-empty">暂无可发现的 Skill。</p>}</div> : <div className="governance-body team-panel"><div className="team-status"><i>●</i><span><b>运行时基础已启用</b><small>WorkerSpec、授权票据与 DAG 约束在后端可测试。</small></span></div><dl><div><dt>Worker 上限</dt><dd>6</dd></div><div><dt>并行任务</dt><dd>3</dd></div><div><dt>重规划次数</dt><dd>2</dd></div></dl><p>真实 Orchestrator 接入后，这里会显示任务 DAG、授权暂停与结果工件。</p></div>}
  </aside></div>
}

export function GovernanceConsole() {
  const [open, setOpen] = useState(false)
  const [agents, setAgents] = useState<Agent[]>([])
  const [agentId, setAgentId] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => { if (open) listAgents().then(rows => { setAgents(rows); setAgentId(current => current || rows[0]?.id || '') }).catch(() => {}) }, [open])
  const agent = agents.find(item => item.id === agentId)
  return <><button className="governance-launch" onClick={() => setOpen(true)}>◈ <span>治理</span></button>{open && <GovernanceDrawer agent={agent} onClose={() => setOpen(false)} notice={text => { setNotice(text); window.setTimeout(() => setNotice(''), 2200) }}/>} {open && <select className="governance-agent-select" value={agentId} onChange={e => setAgentId(e.target.value)} aria-label="选择治理智能体"><option value="">选择智能体</option>{agents.filter(item => item.status !== 'archived').map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select>}{notice && <div className="governance-toast">{notice}</div>}</>
}
