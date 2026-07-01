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
  description: '企业内部智能应用草稿',
  entry: 'main.py',
  runtime: 'python',
  model: 'qwen-turbo',
  prompt: '你是一个可靠的企业智能体，请根据输入给出清晰、可执行的回答。',
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
    { name: '基础问候', input: 'Atlas', expected: 'Hello Atlas: Atlas' }
  ], null, 2)
}

const fallbackFileTree: AppFile[] = [
  { path: 'main.py', name: 'main.py', type: 'file' },
  { path: 'manifest.json', name: 'manifest.json', type: 'file' },
  { path: 'tests.json', name: 'tests.json', type: 'file' }
]

const panels: Array<{ key: Panel; label: string; desc: string }> = [
  { key: 'overview', label: '应用概览', desc: '名称、描述、入口与运行时' },
  { key: 'workflow', label: '工作流', desc: '输入、处理、输出链路' },
  { key: 'code', label: '代码文件', desc: '文件树、编辑器、草稿保存' },
  { key: 'prompt', label: 'Prompt', desc: '系统提示词与模型' },
  { key: 'knowledge', label: '知识与 Skill', desc: '知识库、Skill 依赖声明' },
  { key: 'tools', label: '连接器工具', desc: '外部工具与写操作策略' },
  { key: 'tests', label: '效果评测', desc: '样例输入、期望结果、通过率' },
  { key: 'security', label: '安全发布', desc: '权限、secrets、沙箱检查' }
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
  if (!serverMode) return 'Mock 模式'
  if (!validation) return '草稿已连接'
  return validation.ok ? '校验通过' : '需要修复'
}

export default function WebIDE({ notice, initialDraft }: { notice: (text: string) => void; initialDraft?: AppDraft | null }) {
  const [panel, setPanel] = useState<Panel>('overview')
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
  const [logs, setLogs] = useState('等待运行。保存草稿后点击“运行预览”，这里会显示沙箱日志。')
  const [testCases, setTestCases] = useState<EvaluationCase[]>([
    { name: '基础问候', input: 'Atlas', expected: 'Hello Atlas: Atlas' }
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
      notice(`??? ${draft.name} ??`)
    } catch (error) {
      notice('打开草稿失败')
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
      notice('Web IDE 草稿已连接')
    } catch (error) {
      setServerMode(false)
      setFileTree(fallbackFileTree)
      setFiles(fallbackFiles)
      setSelectedFile('main.py')
      setCurrentContent(fallbackFiles['main.py'])
      setManifest(defaultManifest)
      setValidation(null)
      notice('服务端接口不可用，当前使用 Mock 文件')
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
      setValidation({ ok: false, manifest: {}, errors: ['manifest.json 无法解析'], warnings: [] })
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
      notice('读取文件失败，已使用本地缓存')
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
        notice(`已保存 ${path}`)
      } else {
        setFiles(old => ({ ...old, [path]: content }))
        notice('已保存到前端 Mock 缓存')
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
    notice(`已创建 ${path}`)
  }

  async function renameFile() {
    const nextPath = renamePath.trim()
    if (!nextPath || nextPath === selectedFile) return

    if (selectedFile === 'main.py' || selectedFile === 'manifest.json') {
      notice('核心文件不能重命名')
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

    notice(`已重命名为 ${nextPath}`)
  }

  async function removeFile() {
    if (selectedFile === 'main.py' || selectedFile === 'manifest.json') {
      notice('核心文件不能删除')
      return
    }

    if (!window.confirm(`确认删除 ${selectedFile}？`)) return

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

    notice('文件已删除')
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
    setLogs('正在保存草稿并启动沙箱预览...')

    try {
      if (selectedFile) await saveFile(selectedFile, currentContent)
      await saveManifest()

      if (serverMode && draftId) {
        const result = await runDraftApp(draftId, inputText)
        setLogs(result.logs || '运行结束，但没有返回日志。')
        notice(result.ok ? '运行预览完成' : '运行失败，请查看日志')
      } else {
        setLogs(`Mock run\n> input: ${inputText}\nHello Atlas: ${inputText}`)
      }
    } catch (error) {
      setLogs(error instanceof Error ? error.message : '运行失败')
      notice('运行失败')
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
        notice(`评测完成：${result.passed}/${result.total}`)
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
          <h2>应用概览</h2>
          <p>这里决定应用在工作台和应用市场中的基本身份。</p>
        </div>

        <div className="form-grid">
          <label>
            应用名称
            <input value={manifest.name} onChange={e => updateManifest('name', e.target.value)} />
          </label>
          <label>
            入口文件
            <input value={manifest.entry} onChange={e => updateManifest('entry', e.target.value)} />
          </label>
          <label>
            运行时
            <select value={manifest.runtime} onChange={e => updateManifest('runtime', e.target.value)}>
              <option value="python">Python Sandbox</option>
            </select>
          </label>
          <label>
            推荐模型
            <input value={manifest.model} onChange={e => updateManifest('model', e.target.value)} />
          </label>
        </div>

        <label className="full-field">
          应用描述
          <textarea value={manifest.description} onChange={e => updateManifest('description', e.target.value)} />
        </label>

        <div className="metrics-row">
          <div><strong>{fileTree.length}</strong><span>草稿文件</span></div>
          <div><strong>{validation?.errors.length || 0}</strong><span>阻断问题</span></div>
          <div><strong>{validation?.warnings.length || 0}</strong><span>发布提醒</span></div>
          <div><strong>{serverMode ? '在线' : 'Mock'}</strong><span>后端状态</span></div>
        </div>
      </div>
    )
  }

  function renderWorkflow() {
    return (
      <div className="webide-panel workflow-panel">
        <div className="panel-title">
          <span>Workflow</span>
          <h2>运行链路</h2>
          <p>先把“输入 - 处理 - 输出 - 评测”这条链路跑通，后续再接工作台预设和发布。</p>
        </div>
        <div className="flow-line">
          <div><b>1</b><strong>用户输入</strong><span>{inputText || '等待输入'}</span></div>
          <div><b>2</b><strong>Manifest 校验</strong><span>{validation?.ok ? '通过' : '待检查'}</span></div>
          <div><b>3</b><strong>Python 沙箱</strong><span>{manifest.entry}</span></div>
          <div><b>4</b><strong>运行日志</strong><span>右侧预览面板</span></div>
        </div>
      </div>
    )
  }

  function renderCode() {
    return (
      <div className="code-workspace">
        <aside className="file-rail">
          <div className="rail-head">
            <strong>文件</strong>
            <span>{fileTree.length}</span>
          </div>
          <div className="new-file-row">
            <input placeholder="src/helper.py" value={newFilePath} onChange={e => setNewFilePath(e.target.value)} />
            <button onClick={createFile}>新建</button>
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
            <button onClick={renameFile}>重命名</button>
            <button onClick={removeFile}>删除</button>
            <button className="primary" onClick={() => saveFile()} disabled={saving}>{saving ? '保存中' : '保存文件'}</button>
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
          <h2>提示词与模型</h2>
          <p>把应用的人设、边界、输出风格沉淀到 manifest，便于后续发布和复用。</p>
        </div>
        <label className="full-field">
          系统提示词
          <textarea className="large-textarea" value={manifest.prompt} onChange={e => updateManifest('prompt', e.target.value)} />
        </label>
        <label>
          模型
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
            <h2>知识库</h2>
            <p>声明应用依赖的知识库，后续由工作台和权限系统接入真实资源。</p>
          </div>
          <textarea value={stringifyList(manifest.knowledge_bases)} onChange={e => updateManifest('knowledge_bases', parseList(e.target.value))} placeholder="policy-kb&#10;sales-playbook" />
        </div>
        <div>
          <div className="panel-title compact">
            <span>Skills</span>
            <h2>Skill 能力</h2>
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
          <h2>连接器与工具声明</h2>
          <p>声明应用需要哪些外部连接器。涉及写操作时，发布前必须进入确认和审计。</p>
        </div>
        <div className="connector-grid">
          {['feishu', 'github', 'bing', 'gaode'].map(item => {
            const active = manifest.connectors.includes(item)
            return (
              <button key={item} className={active ? 'active' : ''} onClick={() => updateManifest('connectors', active ? manifest.connectors.filter(v => v !== item) : [...manifest.connectors, item])}>
                <strong>{item}</strong>
                <span>{active ? '已启用' : '未启用'}</span>
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
            <h2>效果评测</h2>
            <p>用样例输入检查应用输出是否稳定，这是后续发布前的最小验收。</p>
          </div>
          <button className="primary" onClick={runEvaluation} disabled={evaluating}>{evaluating ? '评测中' : '运行评测'}</button>
        </div>

        <div className="case-list">
          {testCases.map((item, index) => (
            <div className="case-row" key={index}>
              <input value={item.name} onChange={e => updateTestCase(index, { name: e.target.value })} />
              <input value={item.input} onChange={e => updateTestCase(index, { input: e.target.value })} />
              <input value={item.expected} onChange={e => updateTestCase(index, { expected: e.target.value })} />
              <button onClick={() => setTestCases(old => old.filter((_, i) => i !== index))}>删除</button>
            </div>
          ))}
        </div>
        <button onClick={() => setTestCases(old => [...old, { name: `样例 ${old.length + 1}`, input: '', expected: '' }])}>添加样例</button>

        {evaluation && (
          <div className="evaluation-box">
            <strong>通过率 {(evaluation.pass_rate * 100).toFixed(0)}%</strong>
            <span>{evaluation.passed}/{evaluation.total} 通过</span>
            {evaluation.results.map(item => (
              <p key={item.name} className={item.ok ? 'pass' : 'fail'}>{item.ok ? '通过' : '失败'} - {item.name} - {item.elapsed_ms}ms</p>
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
          <h2>安全与发布检查</h2>
          <p>草稿可以运行，但发布前需要通过权限、网络、secrets 和入口文件检查。</p>
        </div>
        <label className="toggle-line">
          <input type="checkbox" checked={manifest.permissions.network} onChange={e => updatePermission('network', e.target.checked)} />
          需要访问外网
        </label>
        <label>
          文件系统权限
          <select value={manifest.permissions.filesystem} onChange={e => updatePermission('filesystem', e.target.value)}>
            <option value="sandbox">仅沙箱目录</option>
            <option value="readonly">只读工作区</option>
          </select>
        </label>
        <label className="full-field">
          Secrets 声明
          <textarea value={stringifyList(manifest.permissions.secrets)} onChange={e => updatePermission('secrets', parseList(e.target.value))} placeholder="OPENAI_API_KEY&#10;FEISHU_APP_SECRET" />
        </label>
        <div className="check-list">
          {(validation?.errors || []).map(item => <p className="fail" key={item}>阻断：{item}</p>)}
          {(validation?.warnings || []).map(item => <p className="warn" key={item}>提醒：{item}</p>)}
          {validation?.ok && <p className="pass">Manifest 校验通过，可以继续评测。</p>}
        </div>
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
    <section className="webide-page atlas">
      <header className="webide-header">
        <div>
          <span className="eyebrow">Agent App Builder</span>
          <h1>应用开发 Web IDE</h1>
          <p>面向 Atlas 智能体应用的代码化开发、沙箱预览与发布前检查。</p>
        </div>
        <div className="actions">
          <button onClick={initDraft} disabled={loading}>{loading ? '加载中' : '新建草稿'}</button>
          <button onClick={saveManifest} disabled={saving}>{saving ? '保存中' : '保存配置'}</button>
          <button className="primary" onClick={runPreview} disabled={running}>{running ? '运行中' : '运行预览'}</button>
        </div>
      </header>

      <div className="status-bar">
        <div className={validation?.ok ? 'status-pill online' : 'status-pill'}>{statusText(validation, serverMode)}</div>
        <span>draft_id: {draftId || '未连接'}</span>
        <span>entry: {manifest.entry}</span>
        <span>model: {manifest.model}</span>
      </div>

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
              <h3>运行预览</h3>
              <span>沙箱日志与错误定位</span>
            </div>
            <button onClick={runPreview} disabled={running}>{running ? '运行中' : 'Run'}</button>
          </div>
          <label>
            输入
            <textarea className="run-input" value={inputText} onChange={e => setInputText(e.target.value)} />
          </label>
          <pre>{logs}</pre>
        </aside>
      </div>
    </section>
  )
}

