import { useState } from 'react'

type FileMap = Record<string, string>

export default function WebIDE({ notice }: { notice: (text: string) => void }) {
  const [appName, setAppName] = useState('demo-agent-app')
  const [selectedFile, setSelectedFile] = useState('main.py')
  const [logs, setLogs] = useState('等待运行...')
  const [files, setFiles] = useState<FileMap>({
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
  })

  const currentContent = files[selectedFile] || ''

  function updateFile(value: string) {
    setFiles(old => ({
      ...old,
      [selectedFile]: value
    }))
  }

  function saveDraft() {
    console.log({
      appName,
      files
    })

    notice('草稿已保存（Mock）')
  }

  function runPreview() {
    setLogs(`> 构建应用：${appName}
> 检查 manifest.json
> 入口文件：main.py
> 沙箱启动中...
> 执行 main.py
> Hello Atlas: test input
> 运行完成`)
    notice('运行预览完成（Mock）')
  }

  return (
    <section className="webide-page">
      <div className="webide-title">
        <div>
          <span className="section-code">APP DEVELOPMENT / WEB IDE</span>
          <h1>Web IDE</h1>
          <p>编辑 Agent App 文件、manifest 配置，并在沙箱中运行预览。</p>
        </div>

        <div className="webide-actions">
          <button type="button" onClick={saveDraft}>
            保存草稿
          </button>
          <button type="button" className="solid" onClick={runPreview}>
            运行预览
          </button>
        </div>
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

          {Object.keys(files).map(name => (
            <button
              key={name}
              type="button"
              className={selectedFile === name ? 'active' : ''}
              onClick={() => setSelectedFile(name)}
            >
              <span>{name.endsWith('.json') ? '▣' : '◇'}</span>
              {name}
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
            onChange={event => updateFile(event.target.value)}
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
