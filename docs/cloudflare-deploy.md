# Cloudflare 部署

这个项目建议拆成两层部署：

- 前端：Cloudflare Pages，构建 `web/dist`。
- 后端：继续运行 FastAPI，可以放在一台服务器、容器平台，或通过 Cloudflare Tunnel 暴露。

## 前端 Pages

在 Cloudflare Pages 里选择仓库后配置：

- Root directory: `web`
- Build command: `npm run build`
- Build output directory: `dist`
- Environment variable: `VITE_API_BASE_URL=https://你的后端域名`

如果后端和前端同源反代到 `/api`，`VITE_API_BASE_URL` 可以留空。

## 后端 FastAPI

后端仍按现有方式启动：

```powershell
cd api
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

生产环境建议配置：

```env
CORS_ORIGINS=https://你的-pages.pages.dev,https://你的自定义域名
COOKIE_SECURE=true
COOKIE_SAMESITE=none
```

如果前端和后端在同一个站点域名下，`COOKIE_SAMESITE=lax` 也可以。

## MCP 注意事项

远程 MCP（Context7、Firecrawl、百度地图等）可以跟随后端部署。小红书、Excel、本地网页抓取这类本地 MCP 需要在后端所在机器上额外启动对应 MCP 服务，并确保后端能访问它们的本地端口。
