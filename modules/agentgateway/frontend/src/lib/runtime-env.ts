// 统一 API / WS base URL 解析。
//
// 设计原则：
// - 优先使用编译期注入的 NEXT_PUBLIC_API_URL / NEXT_PUBLIC_WS_URL（dev 默认从
//   .env.local 读取）。这是所有部署形态都该走的主路径。
// - 未配置 NEXT_PUBLIC_API_URL 时，HTTP API 走【相对路径】（getApiBase() 返回 ""），
//   请求打到当前页面 origin，由 Next dev server 的 rewrites 在服务端代理到后端。
//   这样通过 VS Code / 任意端口转发访问时，浏览器只需要前端那一个端口，无需再
//   单独转发后端 8000——避免 “127.0.0.1:8000 在浏览器侧指向本机而非服务器” 的坑。
// - WS 无法走 Next rewrites，未配置时 fallback 到 window.location.hostname:8000。
const DEV_BACKEND_PORT = "8000";

function envApi(): string | undefined {
  const v = process.env.NEXT_PUBLIC_API_URL;
  return v && v.length > 0 ? v.replace(/\/+$/, "") : undefined;
}

function envWs(): string | undefined {
  const v = process.env.NEXT_PUBLIC_WS_URL;
  return v && v.length > 0 ? v.replace(/\/+$/, "") : undefined;
}

export function getApiBase(): string {
  const fromEnv = envApi();
  if (fromEnv) return fromEnv;
  // 相对路径：fetch(`${""}${"/api/..."}`) → 打到当前 origin，由 Next rewrites 代理。
  return "";
}

export function getWsBase(): string {
  const fromEnv = envWs();
  if (fromEnv) return fromEnv;
  // 未配置：走当前页面 origin（同前端端口），由 Next dev 的 WS rewrite 代理到后端。
  // 这样和 HTTP 一样只依赖前端那一个转发端口。
  if (typeof window !== "undefined" && window.location?.host) {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${window.location.host}`;
  }
  return `ws://127.0.0.1:${DEV_BACKEND_PORT}`;
}
