This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.

## Dev server 慢的诊断与处理

Next dev server（Turbopack）长跑后可能陷入病态：进程持续吃满多核 CPU、内存膨胀到数 GB，即使没有文件改动也不回落。此时**经 dev server 代理（:3000）的 API 会被显著拖慢**——实测后端直连 1~9ms，经代理变成 4~12s。这是 dev server 退化，不是后端或应用代码慢。

### 判定法：直连 vs 代理延迟对比

```bash
# 后端直连（默认 :8000）
curl -s -o /dev/null -w "direct  %{time_total}s\n" http://127.0.0.1:8000/api/models
# 经 Next dev 代理（:3000）
curl -s -o /dev/null -w "proxied %{time_total}s\n" http://127.0.0.1:3000/api/models
```

直连毫秒级、经代理秒级 → 判定为 dev server 病态。可再用 `top`/`ps` 看 `next-server` 进程 CPU/内存佐证（远超编译所需的持续高占用即异常）。

### 处理手段（按成本递增）

1. **重启 dev server**：通常重启后经代理延迟立即回落到与直连同量级。
2. **清 `.next` 缓存后重启**：缓存膨胀时用 `npm run dev:fresh`（等价于 `rm -rf .next && next dev`）。
3. **避免多实例长跑**：同一项目不要同时跑多个 `next dev`，多余实例会叠加占满 CPU。

### 排查清单

- `.next` 缓存体积异常增大（数百 MB~GB 级）→ 清缓存重启。
- `next-server` 进程长期高 CPU/高内存且与编译无关 → 重启。
- 怀疑慢时**先做直连 vs 代理对比**，再决定是否动后端/应用代码，避免误诊。
