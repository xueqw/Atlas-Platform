import type { NextConfig } from "next";

// 后端地址（仅服务端使用）。dev 默认本机 8000；可用 BACKEND_ORIGIN 覆盖。
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || "http://127.0.0.1:8100";

const nextConfig: NextConfig = {
  // Next.js 16 默认会拦截非 localhost 的 dev 资源（HMR / __nextjs_font 等）。
  // 本地联调常以 127.0.0.1 或局域网 IP 访问 dev server，加入白名单避免被拦。
  allowedDevOrigins: [
    "127.0.0.1",
    "30.207.101.38",
  ],
  // 把所有 /api/* 请求（含 WebSocket 升级）在服务端代理到后端。前端因此可以走相对
  // 路径，浏览器只跟前端端口通信——通过 VS Code / 端口转发访问时无需再转发 8000。
  // 这里的 127.0.0.1 在 *服务器进程* 内解析，指向后端本身，稳妥可靠。
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_ORIGIN}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
