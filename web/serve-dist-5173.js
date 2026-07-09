import { createReadStream, existsSync, statSync } from 'node:fs'
import { createServer, request as httpRequest } from 'node:http'
import { dirname, extname, join, normalize } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), 'dist')
const port = 5173
const types = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.ico': 'image/x-icon',
}

function proxyApi(req, res) {
  const upstream = httpRequest(
    {
      hostname: '127.0.0.1',
      port: 8000,
      path: req.url,
      method: req.method,
      headers: req.headers,
    },
    upstreamRes => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers)
      upstreamRes.pipe(res)
    },
  )
  upstream.on('error', () => {
    res.writeHead(502, { 'content-type': 'application/json; charset=utf-8' })
    res.end(JSON.stringify({ detail: 'API server is not running on 127.0.0.1:8000' }))
  })
  req.pipe(upstream)
}

createServer((req, res) => {
  if (req.url?.startsWith('/api/')) return proxyApi(req, res)

  const urlPath = decodeURIComponent((req.url || '/').split('?')[0])
  const safePath = normalize(urlPath).replace(/^(\.\.[/\\])+/, '')
  let filePath = join(root, safePath)
  if (!existsSync(filePath) || statSync(filePath).isDirectory()) filePath = join(root, 'index.html')

  res.writeHead(200, { 'content-type': types[extname(filePath)] || 'application/octet-stream' })
  createReadStream(filePath).pipe(res)
}).listen(port, '0.0.0.0', () => {
  console.log(`Atlas static web: http://localhost:${port}/`)
})
