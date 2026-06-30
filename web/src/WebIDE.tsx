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
  "runtime": "python",
  "permissions": {
    "network": false,
    "secrets": []
  }
}
`
}

const fallbackFileTree: AppFile[] = [
  {
    path: 'main.py',
    name: 'main.py',
    type: 'file'
  },
  {
    path: 'manifest.json',
    name: 'manifest.json',
    type: 'file'
  }
]

export default function WebIDE({ notice }: { notice: (text: string) => void }) {
  const [appName, setAppName] = useState('demo-agent-app')
  const [draftId, setDraftId] = useState<string>('')
  const [serverMode, setServerMode] = useState(false)

  const [fileTree, setFileTree] = useState<AppFile[]>(fallbackFileTree)
  const [files, setFiles] = useState<FileMap>(fallbackFiles)
  const [selectedFile, setSelectedFile] = useState('main.py')
  const [currentContent, setCurrentContent] = useState(fallbackFiles['main.py'])
  const [logs, setLogs] = useState('等待运行...')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)

  useEffect(() => {
    initDraft()
  }, [])

  async function initDraft() {
    setLoading(true)

    try {
      const draft = await createAppDraft(appName)
      setDraftId(draft.id)
      setAppName(draft.name || appName)

      const tree = await listDraftFiles(draft.id)
      if (tree.length > 0) {
        setFileTree(tree)
        const firstFile = tree.find(item => item.type === 'file') || tree[0]
        setSelectedFile(firstFile.path)
        await openFile(draft.id, firstFile.path)
      }

      setServerMode(true)
      notice('已连接服务器草稿')
    } catch (error) {
      setServerMode(false)
      setFileTree(fallbackFileTree)
      setFiles(fallbackFiles)
      setSelectedFile('main.py')
      setCurrentContent(fallbackFiles['main.py'])
      notice('服务器 Web IDE 接口暂未接入，当前使用 Mock 文件')
    } finally {
      setLoading(false)
    }
  }

  async function openFile(targetDraftId: string, path: string) {
    setSelectedFile(path)

    if (!targetDraftId) {
      setCurrentContent(files[path] || '')
      return
    }

    try {
      const result = await getDraftFile(targetDraftId, path)
      setCurrentContent(result.content)
      setFiles(old => ({
        ...old,
        [path]: result.content
      }))
    } catch (error) {
      setCurrentContent(files[path] || fallbackFiles[path] || '')
      notice('读取服务器文件失败，使用本地缓存内容')
    }
  }

  function updateCurrentFile(value: string) {
    setCurrentContent(value)
    setFiles(old => ({
      ...old,
      [selectedFile]: value
    }))
  }

  async function saveDraft() {
    setSaving(true)

    try {
      if (serverMode && draftId) {
        await saveDraftFile(draftId, selectedFile, currentContent)
        notice(`已保存到服务器：${selectedFile}`)
      } else {
        console.log({
          appName,
          selectedFile,
          content: currentContent,
          files
        })
        notice('草稿已保存（Mock）')
      }
    } catch (error) {
      notice('保存到服务器失败，已保留在前端缓存')
    } finally {
      setSaving(false)
    }
  }

  async function runPreview() {
    setRunning(true)

    try {
      if (serverMode && draftId) {
        const result = await runDraftApp(draftId)
        setLogs(result.logs || '运行完成，但没有返回日志')
        notice(result.ok ? '运行预览完成' : '运行失败，请查看日志')
      } else {
        const mockLogs = `> 构建应用：${appName}
> 当前模式：Mock
> 检查 manifest.json
> 入口文件：main.py
> 沙箱启动中...
> 执行 main.py
> Hello Atlas: test input
> 运行完成`

        setLogs(mockLogs)
        notice('运行预览完成（Mock）')
      }
    } catch (error) {
      setLogs(`> 运行失败
> ${error instanceof Error ? error.message : '未知错误'}
> 当前后端 run 接口可能尚未实现`)
      notice('运行预览失败')
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="webide-page">
      <div className="webide-title">
        <div>
          <span className="section-code">APP DEVELOPMENT / WEB IDE</span>
          <h1>Web IDE</h1>
          <p>
            编辑服务器端 Agent App 文件、manifest 配置，并在沙箱中运行预览。
          </p>
        </div>

        <div className="webide-actions">
          <button type="button" onClick={initDraft} disabled={loading}>
            {loading ? '加载中...' : '重新加载'}
          </button>

          <button type="button" onClick={saveDraft} disabled={saving}>
            {saving ? '保存中...' : '保存草稿'}
          </button>

          <button
            type="button"
            className="solid"
            onClick={runPreview}
            disabled={running}
          >
            {running ? '运行中...' : '运行预览'}
          </button>
        </div>
      </div>

      <div className="webide-status">
        <span className={serverMode ? 'online' : 'offline'}>
          {serverMode ? '● Server Draft Mode' : '○ Mock Mode'}
        </span>

        <small>
          {draftId
            ? `draft_id: ${draftId}`
            : '服务器接口未连接时，将使用前端 Mock 文件'}
        </small>
      </div>

      <div className="webide-name-row">
        <label>
          应用名称
          <input
            value={appName}
            onChange={event => setAppName(event.target.value)}
          />
        </label>
      </div>

      <div className="webide-layout">
        <aside className="webide-files">
          <h3>Files</h3>

          {fileTree.map(file => (
            <button
              key={file.path}
              type="button"
              className={selectedFile === file.path ? 'active' : ''}
              onClick={() => openFile(draftId, file.path)}
            >
              <span>{file.name.endsWith('.json') ? '▣' : '◇'}</span>
              {file.name}
            </button>
          ))}
        </aside>

        <main className="webide-editor">
          <div className="editor-head">
            <strong>{selectedFile}</strong>
            <small>
              {selectedFile === 'manifest.json' ? 'Manifest 配置' : '代码文件'}
            </small>
          </div>

          <textarea
            value={currentContent}
            onChange={event => updateCurrentFile(event.target.value)}
            spellCheck={false}
          />
        </main>

        <aside className="webide-preview">
          <h3>Run Logs</h3>
          <pre>{logs}</pre>
        </aside>
      </div>
    </section>
  )
}
