const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = Number(process.env.PORT || 3000);
const ROOT = __dirname;
const PUBLIC = path.join(ROOT, 'public');
const DATA = path.join(ROOT, 'data');
const DB_FILE = path.join(DATA, 'db.json');

fs.mkdirSync(DATA, { recursive: true });
if (!fs.existsSync(DB_FILE)) {
  fs.writeFileSync(DB_FILE, JSON.stringify({
    agents: [{
      id: 'agent_customer', name: '客户服务助手', description: '基于产品资料回答客户问题，并给出引用来源。',
      model: 'gpt-4.1-mini', status: 'published', updatedAt: new Date().toISOString(),
      prompt: '你是一名严谨、友好的企业客服。优先依据知识库回答；如果资料中没有答案，请明确说明。',
      tools: ['订单查询 API']
    }],
    documents: [{ id: 'doc_demo', name: '产品服务说明.txt', size: 3280, status: 'ready', chunks: 12, createdAt: new Date().toISOString(), content: '企业版支持私有知识库、API 工具调用、权限管理和运行审计。标准服务时间为工作日 9:00-18:00。' }],
    tools: [{ id: 'tool_order', name: '订单查询 API', method: 'GET', url: 'https://api.example.com/orders/{id}', status: 'connected' }],
    conversations: []
  }, null, 2));
}

const readDb = () => JSON.parse(fs.readFileSync(DB_FILE, 'utf8'));
const writeDb = (db) => fs.writeFileSync(DB_FILE, JSON.stringify(db, null, 2));
const json = (res, status, body) => { res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(body)); };
const body = (req) => new Promise((resolve, reject) => {
  let raw = ''; req.on('data', c => { raw += c; if (raw.length > 4e6) req.destroy(); });
  req.on('end', () => { try { resolve(raw ? JSON.parse(raw) : {}); } catch (e) { reject(e); } }); req.on('error', reject);
});

async function chat(input, db) {
  const agent = db.agents.find(a => a.id === input.agentId) || db.agents[0];
  const related = db.documents.filter(d => d.status === 'ready').slice(0, 2);
  if (process.env.OPENAI_API_KEY) {
    const base = (process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/$/, '');
    const context = related.map(d => `[${d.name}] ${d.content || ''}`).join('\n');
    const response = await fetch(`${base}/chat/completions`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${process.env.OPENAI_API_KEY}` }, body: JSON.stringify({ model: process.env.OPENAI_MODEL || agent.model, messages: [{ role: 'system', content: `${agent.prompt}\n\n知识库：\n${context}` }, ...(input.history || []).slice(-8), { role: 'user', content: input.message }] }) });
    if (!response.ok) throw new Error(`模型服务返回 ${response.status}`);
    const result = await response.json();
    return { answer: result.choices?.[0]?.message?.content || '模型没有返回内容。', sources: related.map(d => d.name), mode: 'model' };
  }
  const text = input.message.toLowerCase();
  let answer = '这是本地演示模式。我已经完成知识检索，并生成了一条模拟回复。配置 OPENAI_API_KEY 后，这里会返回真实模型结果。';
  if (/企业|权限|知识库|功能/.test(text)) answer = '根据《产品服务说明》，企业版支持私有知识库、API 工具调用、权限管理和运行审计。';
  if (/时间|服务|客服/.test(text)) answer = '资料显示，标准服务时间为工作日 9:00–18:00。';
  return { answer, sources: related.map(d => d.name), mode: 'demo' };
}

async function api(req, res, url) {
  const db = readDb();
  if (req.method === 'GET' && url.pathname === '/api/overview') return json(res, 200, { agents: db.agents, documents: db.documents, tools: db.tools, stats: { calls: 1284, tokens: 386200, successRate: 99.2, avgLatency: 1.8 } });
  if (req.method === 'GET' && url.pathname === '/api/agents') return json(res, 200, db.agents);
  if (req.method === 'POST' && url.pathname === '/api/agents') {
    const data = await body(req); const item = { id: crypto.randomUUID(), name: data.name || '未命名智能体', description: data.description || '', model: data.model || 'gpt-4.1-mini', prompt: data.prompt || '你是一名可靠的企业助手。', tools: [], status: 'draft', updatedAt: new Date().toISOString() };
    db.agents.unshift(item); writeDb(db); return json(res, 201, item);
  }
  if (req.method === 'PUT' && url.pathname.startsWith('/api/agents/')) {
    const id = url.pathname.split('/').pop(); const data = await body(req); const i = db.agents.findIndex(a => a.id === id);
    if (i < 0) return json(res, 404, { error: '智能体不存在' }); db.agents[i] = { ...db.agents[i], ...data, id, updatedAt: new Date().toISOString() }; writeDb(db); return json(res, 200, db.agents[i]);
  }
  if (req.method === 'POST' && url.pathname === '/api/documents') {
    const data = await body(req); const item = { id: crypto.randomUUID(), name: data.name || '未命名文档', size: data.size || 0, status: 'ready', chunks: Math.max(1, Math.ceil((data.content || '').length / 500)), content: (data.content || '').slice(0, 100000), createdAt: new Date().toISOString() };
    db.documents.unshift(item); writeDb(db); return json(res, 201, item);
  }
  if (req.method === 'POST' && url.pathname === '/api/chat') {
    const data = await body(req); try { const result = await chat(data, db); db.conversations.push({ id: crypto.randomUUID(), agentId: data.agentId, question: data.message, ...result, createdAt: new Date().toISOString() }); writeDb(db); return json(res, 200, result); } catch (e) { return json(res, 502, { error: e.message }); }
  }
  return json(res, 404, { error: '接口不存在' });
}

const mime = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png' };
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname.startsWith('/api/')) return await api(req, res, url);
    let file = path.join(PUBLIC, url.pathname === '/' ? 'index.html' : url.pathname);
    if (!file.startsWith(PUBLIC)) return json(res, 403, { error: '禁止访问' });
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(PUBLIC, 'index.html');
    res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' }); fs.createReadStream(file).pipe(res);
  } catch (e) { json(res, 500, { error: e.message }); }
});
server.listen(PORT, '0.0.0.0', () => console.log(`Atlas Agent Console: http://localhost:${PORT}`));
