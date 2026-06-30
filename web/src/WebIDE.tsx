import { useEffect, useState } from 'react'
import {
  createAppDraft,
  getDraftFile,
  listDraftFiles,
  runDraftApp,
  saveDraftFile,
  type AppFile
} from './api'

type FileMap = Record<string, string>

const fallbackFiles: FileMap = {
  'main.py': `def main(input_text: str):
    return "Hello Atlas: " + input_text
`,
  'manifest.json': `{
  "name": "demo-agent-app",
  "description": "A demo Agent App",
  "entry": "main.py",
  "runtime": "python"
}
`
}

const fallbackFileTree: AppFile[] = [
  { path: 'main.py', name: 'main.py', type: 'file' },
  { path: 'manifest.json', name: 'manifest.json', type: 'file' }
]

export default function WebIDE({ notice }: { notice: (text: string) => void }) {
  const [appName, setAppName] = useState('demo-agent-app')
  const [draftId, setDraftId] = useState('')
  const [serverMode, setServerMode] = useState(false)

  const [fileTree, setFileTree] = useState<AppFile[]>(fallbackFileTree)
  const [files, setFiles] = useState<FileMap>(fallbackFiles)
  const [selectedFile, setSelectedFile] = useState('main.py')
  const [currentContent, setCurrentContent] = useState(fallbackFiles['main.py'])

  const [logs, setLogs] = useState('等待运行...')
  const [running, setRunning] = useState(false)

  const [panel, setPanel] = useState<
    'overview' | 'workflow' | 'code' | 'prompt' | 'knowledge' | 'tools' | 'tests'
  >('code')

  useEffect(() => {
    initDraft()
  }, [])

  async function initDraft() {
    try {
      const draft = await createAppDraft(appName)
      setDraftId(draft.id)

      const tree = await listDraftFiles(draft.id)
      if (tree?.length) {
        setFileTree(tree)
        const first = tree[0]
        setSelectedFile(first.path)
        await openFile(draft.id, first.path)
      }

      setServerMode(true)
      notice('Server Draft Mode 已连接')
    } catch (e) {
      setServerMode(false)
      setFileTree(fallbackFileTree)
      setFiles(fallbackFiles)
      setCurrentContent(fallbackFiles['main.py'])
      notice('Mock Mode')
    }
  }

  async function openFile(id: string, path: string) {
    setSelectedFile(path)

    if (!id) {
      setCurrentContent(files[path])
      return
    }

    try {
      const res = await getDraftFile(id, path)
      setCurrentContent(res.content)
    } catch {
      setCurrentContent(files[path] || '')
    }
  }

  function updateFile(v: string) {
    setCurrentContent(v)
    setFiles(old => ({ ...old, [selectedFile]: v }))
  }

  async function save() {
    if (serverMode && draftId) {
      await saveDraftFile(draftId, selectedFile, currentContent)
      notice('已保存到服务器')
    } else {
      notice('Mock 保存')
    }
  }

  async function run() {
    setRunning(true)
    try {
      if (serverMode && draftId) {
        const r = await runDraftApp(draftId)
        setLogs(r.logs)
      } else {
        setLogs(`Mock run:
Hello Atlas: test`)
      }
    } catch (e) {
      setLogs('运行失败')
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="webide-page coze">

      {/* HEADER */}
      <div className="webide-header">
        <div>
          <h1>Web IDE</h1>
          <p>Coze-style Agent App Builder</p>
        </div>

        <div className="actions">
          <button onClick={initDraft}>Reload</button>
          <button onClick={save}>Save</button>
          <button className="primary" onClick={run}>
            {running ? 'Running...' : 'Run'}
          </button>
        </div>
      </div>

      {/* STATUS */}
      <div className="status">
        {serverMode ? '● Server Draft Mode' : '○ Mock Mode'}
        <span>{draftId}</span>
      </div>

      {/* BODY */}
      <div className="layout">

        {/* LEFT NAV */}
        <aside className="nav">
          {['overview','workflow','code','prompt','knowledge','tools','tests'].map(k=>(
            <button
              key={k}
              className={panel===k?'active':''}
              onClick={()=>setPanel(k as any)}
            >
              {k}
            </button>
          ))}
        </aside>

        {/* CENTER */}
        <main className="center">

          {panel === 'code' && (
            <div className="code">

              <div className="files">
                {fileTree.map(f=>(
                  <div
                    key={f.path}
                    onClick={()=>openFile(draftId,f.path)}
                    className={selectedFile===f.path?'active':''}
                  >
                    {f.name}
                  </div>
                ))}
              </div>

              <div className="editor">
                <textarea
                  value={currentContent}
                  onChange={e=>updateFile(e.target.value)}
                />
              </div>

            </div>
          )}

          {panel === 'overview' && (
            <div>
              <h2>Overview</h2>
              <input value={appName} onChange={e=>setAppName(e.target.value)} />
            </div>
          )}

          {panel === 'workflow' && <h2>Workflow (todo)</h2>}
          {panel === 'prompt' && <h2>Prompt (todo)</h2>}
          {panel === 'knowledge' && <h2>Knowledge (todo)</h2>}
          {panel === 'tools' && <h2>Tools (todo)</h2>}
          {panel === 'tests' && <h2>Tests (todo)</h2>}

        </main>

        {/* RIGHT DEBUG */}
        <aside className="debug">
          <h3>Run Logs</h3>
          <pre>{logs}</pre>
        </aside>

      </div>

    </section>
  )
}
