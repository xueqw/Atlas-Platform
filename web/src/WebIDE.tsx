import { useEffect, useState } from 'react'
import {
  createAppDraft,
  createDraftFile,
  deleteDraftFile,
  evaluateDraftApp,
  getDraftFile,
  listDraftFiles,
  renameDraftFile,
  runDraftApp,
  saveDraftFile,
  validateDraftManifest,
  type AppFile,
  type EvaluationCase,
  type EvaluationResult,
  type ManifestValidation,
  type AppDraft
} from './api'

type FileMap = Record<string, string>
type Panel = 'overview' | 'workflow' | 'code' | 'prompt' | 'knowledge' | 'tools' | 'tests' | 'security'
type ManifestValue = string | boolean | string[]

type ManifestDraft = {
  name: string
  description: string
  entry: string
  runtime: string
  model: string
  prompt: string
  knowledge_bases: string[]
  skills: string[]
  connectors: string[]
  permissions: {
    network: boolean
    filesystem: string
    secrets: string[]
  }
}

const defaultManifest: ManifestDraft = {
  name: 'demo-agent-app',
  description: 'AI agent app draft',
  entry: 'main.py',
  runtime: 'python',
  model: 'gpt-4.1-mini',
  prompt: 'You are a reliable business AI agent. Give clear, actionable answers grounded in the available context.',
  knowledge_bases: [],
  skills: [],
  connectors: [],
  permissions: {
    network: false,
    filesystem: 'sandbox',
    secrets: []
  }
}

const fallbackFiles: FileMap = {
  'main.py': `def main(input_text: str):
    return "Hello Atlas: " + input_text


if __name__ == "__main__":
    print(main("test input"))
`,
  'manifest.json': JSON.stringify(defaultManifest, null, 2),
  'tests.json': JSON.stringify([
    { name: 'Basic greeting', input: 'Atlas', expected: 'Hello Atlas: Atlas' }
  ], null, 2)
}

const fallbackFileTree: AppFile[] = [
  { path: 'main.py', name: 'main.py', type: 'file' },
  { path: 'manifest.json', name: 'manifest.json', type: 'file' },
  { path: 'tests.json', name: 'tests.json', type: 'file' }
]

const panels: Array<{ key: Panel; label: string; desc: string }> = [
  { key: 'overview', label: 'App Overview', desc: 'Name, description, entry point, and runtime' },
  { key: 'workflow', label: 'Workflow', desc: 'Input, processing, output, and evaluation' },
  { key: 'code', label: 'Code', desc: 'File tree, editor, and draft saves' },
  { key: 'prompt', label: 'Prompt', desc: 'System prompt and model' },
  { key: 'knowledge', label: 'Knowledge & Skills', desc: 'Knowledge base and skill dependencies' },
  { key: 'tools', label: 'Connector Tools', desc: 'External tools and write-action policies' },
  { key: 'tests', label: 'Evaluation', desc: 'Test inputs, expected results, and pass rate' },
  { key: 'security', label: 'Security & Release', desc: 'Permissions, secrets, and sandbox checks' }
]

function parseList(value: string) {
  return value
    .split(/[\n,]/)
    .map(item => item.trim())
    .filter(Boolean)
}

function stringifyList(value: string[]) {
  return value.join('\n')
}

function safeJsonParse<T>(text: string, fallback: T): T {
  try {
    return JSON.parse(text) as T
  } catch {
    return fallback
  }
}

function normalizeManifest(raw: unknown): ManifestDraft {
  const data = (raw && typeof raw === 'object' ? raw : {}) as Partial<ManifestDraft>
  const permissions = data.permissions || defaultManifest.permissions

  return {
    ...defaultManifest,
    ...data,
    knowledge_bases: Array.isArray(data.knowledge_bases) ? data.knowledge_bases : [],
    skills: Array.isArray(data.skills) ? data.skills : [],
    connectors: Array.isArray(data.connectors) ? data.connectors : [],
    permissions: {
      ...defaultManifest.permissions,
      ...permissions,
      secrets: Array.isArray(permissions.secrets) ? permissions.secrets : []
    }
  }
}

function statusText(validation: ManifestValidation | null, serverMode: boolean) {
  if (!serverMode) return 'Mock mode'
  if (!validation) return 'Draft connected'
  return validation.ok ? 'Validation passed' : 'Needs fixes'
}

export default function WebIDE({ notice, initialDraft }: { notice: (text: string) => void; initialDraft?: AppDraft | null }) {
  const [panel, setPanel] = useState<Panel>('overview')
  const [advancedMode, setAdvancedMode] = useState(false)
  const [appName, setAppName] = useState('demo-agent-app')
  const [draftId, setDraftId] = useState('')
  const [serverMode, setServerMode] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [evaluating, setEvaluating] = useState(false)

  const [fileTree, setFileTree] = useState<AppFile[]>(fallbackFileTree)
  const [files, setFiles] = useState<FileMap>(fallbackFiles)
  const [selectedFile, setSelectedFile] = useState('main.py')
  const [currentContent, setCurrentContent] = useState(fallbackFiles['main.py'])
  const [newFilePath, setNewFilePath] = useState('')
  const [renamePath, setRenamePath] = useState('')

  const [manifest, setManifest] = useState<ManifestDraft>(defaultManifest)
  const [validation, setValidation] = useState<ManifestValidation | null>(null)
  const [inputText, setInputText] = useState('Atlas')
  const [logs, setLogs] = useState('Ready. Save the draft and run a preview to see sandbox logs here.')
  const [testCases, setTestCases] = useState<EvaluationCase[]>([
    { name: 'Basic greeting', input: 'Atlas', expected: 'Hello Atlas: Atlas' }
  ])
  const [evaluation, setEvaluation] = useState<EvaluationResult | null>(null)

  useEffect(() => {
    if (initialDraft?.id) {
      void loadExistingDraft(initialDraft)
    } else {
      void initDraft()
    }
  }, [initialDraft?.id])

  useEffect(() => {
    if (selectedFile === 'manifest.json') {
      setManifest(normalizeManifest(safeJsonParse(currentContent, manifest)))
    }
  }, [currentContent, selectedFile])

  async function loadExistingDraft(draft: AppDraft) {
    setLoading(true)
    setEvaluation(null)

    try {
      setDraftId(draft.id)
      setAppName(draft.name || appName)
      setServerMode(true)

      const tree = await listDraftFiles(draft.id)
      setFileTree(tree.length ? tree : fallbackFileTree)
      const firstPath = tree.find(item => item.path === 'manifest.json')?.path || tree[0]?.path || 'main.py'
      await openFile(draft.id, firstPath)
      await refreshValidation(draft.id)
      notice(`Opened ${draft.name}`)
    } catch (error) {
      notice('Could not open the draft')
    } finally {
      setLoading(false)
    }
  }

  async function initDraft() {
    setLoading(true)
    setEvaluation(null)

    try {
      const draft = await createAppDraft(appName)
      setDraftId(draft.id)
      setAppName(draft.name || appName)

      const tree = await listDraftFiles(draft.id)
      setFileTree(tree.length ? tree : fallbackFileTree)
      setServerMode(true)

      const firstPath = tree.find(item => item.path === 'manifest.json')?.path || tree[0]?.path || 'main.py'
      await openFile(draft.id, firstPath)
      await refreshValidation(draft.id)
      notice('Web IDE draft connected')
    } catch (error) {
      setServerMode(false)
      setFileTree(fallbackFileTree)
      setFiles(fallbackFiles)
      setSelectedFile('main.py')
      setCurrentContent(fallbackFiles['main.py'])
      setManifest(defaultManifest)
      setValidation(null)
      notice('API unavailable; using mock files')
    } finally {
      setLoading(false)
    }
  }

  async function refreshFiles(nextDraftId = draftId) {
    if (!serverMode || !nextDraftId) return
    const tree = await listDraftFiles(nextDraftId)
    setFileTree(tree)
  }

  async function refreshValidation(nextDraftId = draftId) {
    if (!serverMode || !nextDraftId) return
    try {
      const result = await validateDraftManifest(nextDraftId)
      setValidation(result)
      setManifest(normalizeManifest(result.manifest))
    } catch (error) {
      setValidation({ ok: false, manifest: {}, errors: ['Could not parse manifest.json'], warnings: [] })
    }
  }

  async function openFile(targetDraftId: string, path: string) {
    setSelectedFile(path)
    setRenamePath(path)

    if (!targetDraftId) {
      const content = files[path] || ''
      setCurrentContent(content)
      return
    }

    try {
      const result = await getDraftFile(targetDraftId, path)
      setCurrentContent(result.content)
      setFiles(old => ({ ...old, [path]: result.content }))

      if (path === 'tests.json') {
        const parsed = safeJsonParse<EvaluationCase[]>(result.content, testCases)
        if (Array.isArray(parsed)) setTestCases(parsed)
      }
    } catch (error) {
      setCurrentContent(files[path] || '')
      notice('Could not read the file; using the local cache')
    }
  }

  function updateFile(value: string) {
    setCurrentContent(value)
    setFiles(old => ({ ...old, [selectedFile]: value }))
  }

  async function saveFile(path = selectedFile, content = currentContent) {
    setSaving(true)

    try {
      if (serverMode && draftId) {
        await saveDraftFile(draftId, path, content)
        if (path === 'manifest.json') await refreshValidation()
        notice(`Saved ${path}`)
      } else {
        setFiles(old => ({ ...old, [path]: content }))
        notice('Saved to the browser mock cache')
      }
    } finally {
      setSaving(false)
    }
  }

  async function createFile() {
    const path = newFilePath.trim()
    if (!path) return

    const content = path.endsWith('.json') ? '{}\n' : ''

    if (serverMode && draftId) {
      await createDraftFile(draftId, path, content)
      await refreshFiles()
      await openFile(draftId, path)
    } else {
      setFileTree(old => [...old, { path, name: path.split('/').pop() || path, type: 'file' }])
      setFiles(old => ({ ...old, [path]: content }))
      setSelectedFile(path)
      setCurrentContent(content)
    }

    setNewFilePath('')
    notice(`Created ${path}`)
  }

  async function renameFile() {
    const nextPath = renamePath.trim()
    if (!nextPath || nextPath === selectedFile) return

    if (selectedFile === 'main.py' || selectedFile === 'manifest.json') {
      notice('Core files cannot be renamed')
      return
    }

    if (serverMode && draftId) {
      await renameDraftFile(draftId, selectedFile, nextPath)
      await refreshFiles()
      await openFile(draftId, nextPath)
    } else {
      setFileTree(old => old.map(item => item.path === selectedFile ? { ...item, path: nextPath, name: nextPath.split('/').pop() || nextPath } : item))
      setFiles(old => {
        const next = { ...old, [nextPath]: old[selectedFile] || currentContent }
        delete next[selectedFile]
        return next
      })
      setSelectedFile(nextPath)
    }

    notice(`Renamed to ${nextPath}`)
  }

  async function removeFile() {
    if (selectedFile === 'main.py' || selectedFile === 'manifest.json') {
      notice('Core files cannot be deleted')
      return
    }

    if (!window.confirm(`Delete ${selectedFile}?`)) return

    if (serverMode && draftId) {
      await deleteDraftFile(draftId, selectedFile)
      await refreshFiles()
      const nextFile = fileTree.find(item => item.path !== selectedFile)?.path || 'main.py'
      await openFile(draftId, nextFile)
    } else {
      setFileTree(old => old.filter(item => item.path !== selectedFile))
      setSelectedFile('main.py')
      setCurrentContent(files['main.py'] || fallbackFiles['main.py'])
    }

    notice('File deleted')
  }

  function updateManifest<K extends keyof ManifestDraft>(key: K, value: ManifestDraft[K]) {
    const next = { ...manifest, [key]: value }
    setManifest(next)
    const content = JSON.stringify(next, null, 2)
    setFiles(old => ({ ...old, 'manifest.json': content }))
    if (selectedFile === 'manifest.json') setCurrentContent(content)
  }

  function updatePermission(key: keyof ManifestDraft['permissions'], value: ManifestValue) {
    const next = {
      ...manifest,
      permissions: {
        ...manifest.permissions,
        [key]: value
      }
    }
    setManifest(next)
    const content = JSON.stringify(next, null, 2)
    setFiles(old => ({ ...old, 'manifest.json': content }))
    if (selectedFile === 'manifest.json') setCurrentContent(content)
  }

  async function saveManifest() {
    const content = JSON.stringify(manifest, null, 2)
    await saveFile('manifest.json', content)
  }

  async function runPreview() {
    setRunning(true)
    setLogs('Saving the draft and starting the sandbox preview...')

    try {
      if (selectedFile) await saveFile(selectedFile, currentContent)
      await saveManifest()

      if (serverMode && draftId) {
        const result = await runDraftApp(draftId, inputText)
        setLogs(result.logs || 'The run completed without logs.')
        notice(result.ok ? 'Preview completed' : 'Run failed. Check the logs.')
      } else {
        setLogs(`Mock run\n> input: ${inputText}\nHello Atlas: ${inputText}`)
      }
    } catch (error) {
      setLogs(error instanceof Error ? error.message : 'Run failed')
      notice('Run failed')
    } finally {
      setRunning(false)
    }
  }

  async function runEvaluation() {
    setEvaluating(true)
    setEvaluation(null)

    try {
      await saveManifest()
      await saveFile('tests.json', JSON.stringify(testCases, null, 2))

      if (serverMode && draftId) {
        const result = await evaluateDraftApp(draftId, testCases)
        setEvaluation(result)
        notice(`Evaluation complete: ${result.passed}/${result.total}`)
      } else {
        setEvaluation({
          ok: true,
          passed: testCases.length,
          total: testCases.length,
          pass_rate: 1,
          results: testCases.map(item => ({ ...item, ok: true, output: `Hello Atlas: ${item.input}`, logs: 'Mock evaluation', elapsed_ms: 0 }))
        })
      }
    } finally {
      setEvaluating(false)
    }
  }

  function updateTestCase(index: number, patch: Partial<EvaluationCase>) {
    setTestCases(old => old.map((item, i) => i === index ? { ...item, ...patch } : item))
  }

  function renderOverview() {
    return (
      <div className="webide-panel overview-panel">
        <div className="panel-title">
          <span>Overview</span>
          <h2>App overview</h2>
          <p>Define how this agent appears in the workspace and future app catalog.</p>
        </div>

        <div className="form-grid">
          <label>
            App name
            <input value={manifest.name} onChange={e => updateManifest('name', e.target.value)} />
          </label>
          <label>
            Entry file
            <input value={manifest.entry} onChange={e => updateManifest('entry', e.target.value)} />
          </label>
          <label>
            Runtime
            <select value={manifest.runtime} onChange={e => updateManifest('runtime', e.target.value)}>
              <option value="python">Python Sandbox</option>
            </select>
          </label>
          <label>
            Recommended model
            <input value={manifest.model} onChange={e => updateManifest('model', e.target.value)} />
          </label>
        </div>

        <label className="full-field">
          App description
          <textarea value={manifest.description} onChange={e => updateManifest('description', e.target.value)} />
        </label>

        <div className="metrics-row">
          <div><strong>{fileTree.length}</strong><span>Draft files</span></div>
          <div><strong>{validation?.errors.length || 0}</strong><span>Blocking issues</span></div>
          <div><strong>{validation?.warnings.length || 0}</strong><span>Release warnings</span></div>
          <div><strong>{serverMode ? 'Online' : 'Mock'}</strong><span>API status</span></div>
        </div>
      </div>
    )
  }

  function renderWorkflow() {
    return (
      <div className="webide-panel workflow-panel">
        <div className="panel-title">
          <span>Workflow</span>
          <h2>Runtime flow</h2>
          <p>Validate the input, processing, output, and evaluation loop before publishing.</p>
        </div>
        <div className="flow-line">
          <div><b>1</b><strong>User input</strong><span>{inputText || 'Waiting for input'}</span></div>
          <div><b>2</b><strong>Manifest validation</strong><span>{validation?.ok ? 'Passed' : 'Needs review'}</span></div>
          <div><b>3</b><strong>Python sandbox</strong><span>{manifest.entry}</span></div>
          <div><b>4</b><strong>Runtime logs</strong><span>Preview panel</span></div>
        </div>
      </div>
    )
  }

  function renderCode() {
    return (
      <div className="code-workspace">
        <aside className="file-rail">
          <div className="rail-head">
            <strong>Files</strong>
            <span>{fileTree.length}</span>
          </div>
          <div className="new-file-row">
            <input placeholder="src/helper.py" value={newFilePath} onChange={e => setNewFilePath(e.target.value)} />
            <button onClick={createFile}>New</button>
          </div>
          <div className="file-list">
            {fileTree.map(file => (
              <button key={file.path} className={selectedFile === file.path ? 'active' : ''} onClick={() => openFile(draftId, file.path)}>
                <span>{file.name.endsWith('.json') ? '{}' : 'py'}</span>
                {file.path}
              </button>
            ))}
          </div>
        </aside>

        <main className="editor-shell">
          <div className="editor-toolbar">
            <input value={renamePath} onChange={e => setRenamePath(e.target.value)} />
            <button onClick={renameFile}>Rename</button>
            <button onClick={removeFile}>Delete</button>
            <button className="primary" onClick={() => saveFile()} disabled={saving}>{saving ? 'Saving' : 'Save file'}</button>
          </div>
          <textarea value={currentContent} onChange={e => updateFile(e.target.value)} spellCheck={false} />
        </main>
      </div>
    )
  }

  function renderPrompt() {
    return (
      <div className="webide-panel prompt-panel">
        <div className="panel-title">
          <span>Prompt</span>
          <h2>Prompt and model</h2>
          <p>Keep the agent's role, boundaries, and response style in the manifest for reuse and release.</p>
        </div>
        <label className="full-field">
          System prompt
          <textarea className="large-textarea" value={manifest.prompt} onChange={e => updateManifest('prompt', e.target.value)} />
        </label>
        <label>
          Model
          <input value={manifest.model} onChange={e => updateManifest('model', e.target.value)} />
        </label>
      </div>
    )
  }

  function renderKnowledge() {
    return (
      <div className="webide-panel split-panel">
        <div>
          <div className="panel-title">
            <span>Knowledge</span>
            <h2>Knowledge bases</h2>
            <p>Declare the knowledge sources this agent requires. Access controls are applied in the workspace.</p>
          </div>
          <textarea value={stringifyList(manifest.knowledge_bases)} onChange={e => updateManifest('knowledge_bases', parseList(e.target.value))} placeholder="policy-kb&#10;sales-playbook" />
        </div>
        <div>
          <div className="panel-title compact">
            <span>Skills</span>
            <h2>Skills</h2>
          </div>
          <textarea value={stringifyList(manifest.skills)} onChange={e => updateManifest('skills', parseList(e.target.value))} placeholder="meeting-summary&#10;proposal-writer" />
        </div>
      </div>
    )
  }

  function renderTools() {
    return (
      <div className="webide-panel tools-panel">
        <div className="panel-title">
          <span>Tools</span>
          <h2>Connectors and tools</h2>
          <p>Declare external systems used by the agent. Write actions require approval and audit controls.</p>
        </div>
        <div className="connector-grid">
          {['feishu', 'github', 'bing', 'gaode'].map(item => {
            const active = manifest.connectors.includes(item)
            return (
              <button key={item} className={active ? 'active' : ''} onClick={() => updateManifest('connectors', active ? manifest.connectors.filter(v => v !== item) : [...manifest.connectors, item])}>
                <strong>{item}</strong>
                <span>{active ? 'Enabled' : 'Disabled'}</span>
              </button>
            )
          })}
        </div>
      </div>
    )
  }

  function renderTests() {
    return (
      <div className="webide-panel tests-panel">
        <div className="panel-title row-title">
          <div>
            <span>Evaluation</span>
            <h2>Evaluation</h2>
            <p>Use representative cases to check output stability before publishing.</p>
          </div>
          <button className="primary" onClick={runEvaluation} disabled={evaluating}>{evaluating ? 'Evaluating' : 'Run evaluation'}</button>
        </div>

        <div className="case-list">
          {testCases.map((item, index) => (
            <div className="case-row" key={index}>
              <input value={item.name} onChange={e => updateTestCase(index, { name: e.target.value })} />
              <input value={item.input} onChange={e => updateTestCase(index, { input: e.target.value })} />
              <input value={item.expected} onChange={e => updateTestCase(index, { expected: e.target.value })} />
              <button onClick={() => setTestCases(old => old.filter((_, i) => i !== index))}>Delete</button>
            </div>
          ))}
        </div>
        <button onClick={() => setTestCases(old => [...old, { name: `Case ${old.length + 1}`, input: '', expected: '' }])}>Add case</button>

        {evaluation && (
          <div className="evaluation-box">
            <strong>Pass rate {(evaluation.pass_rate * 100).toFixed(0)}%</strong>
            <span>{evaluation.passed}/{evaluation.total} passed</span>
            {evaluation.results.map(item => (
              <p key={item.name} className={item.ok ? 'pass' : 'fail'}>{item.ok ? 'Passed' : 'Failed'} - {item.name} - {item.elapsed_ms}ms</p>
            ))}
          </div>
        )}
      </div>
    )
  }

  function renderSecurity() {
    return (
      <div className="webide-panel security-panel">
        <div className="panel-title">
          <span>Security</span>
          <h2>Security and release checks</h2>
          <p>Review permissions, network access, secrets, and the entry point before publishing.</p>
        </div>
        <label className="toggle-line">
          <input type="checkbox" checked={manifest.permissions.network} onChange={e => updatePermission('network', e.target.checked)} />
          Requires outbound network access
        </label>
        <label>
          File system access
          <select value={manifest.permissions.filesystem} onChange={e => updatePermission('filesystem', e.target.value)}>
            <option value="sandbox">Sandbox directory only</option>
            <option value="readonly">Read-only workspace</option>
          </select>
        </label>
        <label className="full-field">
          Declared secrets
          <textarea value={stringifyList(manifest.permissions.secrets)} onChange={e => updatePermission('secrets', parseList(e.target.value))} placeholder="OPENAI_API_KEY&#10;FEISHU_APP_SECRET" />
        </label>
        <div className="check-list">
          {(validation?.errors || []).map(item => <p className="fail" key={item}>Blocking: {item}</p>)}
          {(validation?.warnings || []).map(item => <p className="warn" key={item}>Warning: {item}</p>)}
          {validation?.ok && <p className="pass">Manifest validation passed. The draft is ready for evaluation.</p>}
        </div>
      </div>
    )
  }

  function renderAgentUse() {
    const visibleLogs = logs.replace(/^> .*\n/gm, '').trim()
    const ready = serverMode && draftId && validation?.ok !== false

    return (
      <div className="agent-use-shell">
        <section className="agent-use-main">
          <div className="agent-use-hero">
            <span>READY TO USE AGENT</span>
            <h2>{manifest.name || appName}</h2>
            <p>{manifest.description || 'This agent is ready to run, evaluate, and use as a deployment prototype.'}</p>
            <div className="agent-use-badges">
              <b>{ready ? 'Ready' : 'Preparing'}</b>
              <b>{manifest.model}</b>
              <b>{manifest.skills.length || 0} Skills</b>
              <b>{fileTree.length} Files</b>
            </div>
          </div>

          <div className="agent-use-chat">
            <div className="agent-chat-row assistant">
              <span>A</span>
              <div>
                <strong>{manifest.name || 'Atlas Agent'}</strong>
                <p>I am ready. Enter a task and I will respond using the generated prompt, skills, and sandbox code.</p>
              </div>
            </div>

            <label className="agent-use-input">
              What should this agent do?
              <textarea
                value={inputText}
                onChange={e => setInputText(e.target.value)}
                placeholder="For example: Explain our return policy from the knowledge base and draft a customer response."
              />
            </label>

            <div className="agent-use-actions">
              <button className="primary" onClick={runPreview} disabled={running || !inputText.trim()}>
                {running ? 'Agent is working...' : 'Run agent'}
              </button>
              <button onClick={() => setInputText('Summarize this request and recommend the next steps.')}>Try example</button>
            </div>

            <div className="agent-answer-card">
              <div className="answer-title">
                <span>Agent output</span>
                <b>{running ? 'Running' : logs.includes('exit code') ? 'Complete' : 'Waiting for input'}</b>
              </div>
              <pre>{visibleLogs || 'The agent response will appear here after you run it.'}</pre>
            </div>
          </div>
        </section>

        <aside className="agent-use-side">
          <div className="agent-side-card">
            <span>Release readiness</span>
            <h3>Your agent is ready</h3>
            <p>Atlas generated the configuration, code, skill spec, and tests. Run it now or open advanced editing when needed.</p>
          </div>

          <div className="agent-side-card">
            <span>Capabilities</span>
            <div className="skill-tags use-tags">
              {manifest.skills.length ? manifest.skills.map(item => <span key={item}>{item}</span>) : <span>Release ready</span>}
              {manifest.connectors.map(item => <span key={item}>{item}</span>)}
            </div>
          </div>

          <div className="agent-side-card">
            <span>App files</span>
            <div className="file-chips">
              {fileTree.map(file => <b key={file.path}>{file.name}</b>)}
            </div>
          </div>

          <button className="advanced-toggle" onClick={() => setAdvancedMode(true)}>
            Advanced editing: code and configuration
          </button>
        </aside>
      </div>
    )
  }

  function renderPanel() {
    if (panel === 'overview') return renderOverview()
    if (panel === 'workflow') return renderWorkflow()
    if (panel === 'code') return renderCode()
    if (panel === 'prompt') return renderPrompt()
    if (panel === 'knowledge') return renderKnowledge()
    if (panel === 'tools') return renderTools()
    if (panel === 'tests') return renderTests()
    return renderSecurity()
  }

  return (
    <section className={`webide-page atlas ${advancedMode ? 'advanced-open' : 'agent-use-mode'}`}>
      <header className="webide-header use-header">
        <div>
          <span className="eyebrow">Agent Ready</span>
          <h1>{advancedMode ? 'Advanced Web IDE' : 'Run your agent'}</h1>
          <p>{advancedMode ? 'Edit code, prompts, and release settings here.' : 'This agent is generated and ready to run without manual configuration.'}</p>
        </div>
        <div className="actions">
          {advancedMode ? (
            <button onClick={() => setAdvancedMode(false)}>Back to agent</button>
          ) : (
            <button onClick={() => setAdvancedMode(true)}>Advanced editing</button>
          )}
          <button onClick={initDraft} disabled={loading}>{loading ? 'Loading' : 'New agent'}</button>
          <button className="primary" onClick={runPreview} disabled={running || !inputText.trim()}>{running ? 'Running' : 'Run agent'}</button>
        </div>
      </header>

      <div className="status-bar use-status-bar">
        <div className={validation?.ok ? 'status-pill online' : 'status-pill'}>{statusText(validation, serverMode)}</div>
        <span>{manifest.name || appName}</span>
        <span>{manifest.model}</span>
        <span>{advancedMode ? 'Advanced editing open' : 'Ready to use'}</span>
      </div>

      {advancedMode ? (
        <div className="builder-shell">
          <aside className="nav">
            {panels.map(item => (
              <button key={item.key} className={panel === item.key ? 'active' : ''} onClick={() => setPanel(item.key)}>
                <strong>{item.label}</strong>
                <span>{item.desc}</span>
              </button>
            ))}
          </aside>

          <main className="center">{renderPanel()}</main>

          <aside className="debug">
            <div className="preview-head">
              <div>
                <h3>Runtime preview</h3>
                <span>Sandbox logs and errors</span>
              </div>
              <button onClick={runPreview} disabled={running}>{running ? 'Running' : 'Run'}</button>
            </div>
            <label>
              Input
              <textarea className="run-input" value={inputText} onChange={e => setInputText(e.target.value)} />
            </label>
            <pre>{logs}</pre>
          </aside>
        </div>
      ) : renderAgentUse()}
    </section>
  )
}
