"use client";
// DEV-ONLY primitives preview. Not linked from any production nav — open
// /dev-primitives directly to visually self-check every ag-* primitive and
// every status semantic in both light and dark. Toggling the button flips a
// local `dark` wrapper so both themes render side by side without changing the
// app. (Folder is not `_primitives` because the App Router treats `_`-prefixed
// folders as private and excludes them from routing entirely.)
import { useState } from "react";
import {
  AgPanel,
  AgCard,
  AgBadge,
  AgMetricCard,
  AgSectionHeader,
  AgPill,
  AgSplitPane,
  AgToolbarButton,
  AgSidebarItem,
  AG_STATUSES,
} from "@/components/ag";
import { Sparkles, Folder } from "lucide-react";

function Showcase() {
  return (
    <div className="flex flex-col gap-6 bg-paper p-6 text-ink">
      {/* Status badges — all 11 semantics */}
      <section className="flex flex-col gap-2">
        <AgSectionHeader eyebrow="status" title="AgBadge · 全部状态语义" />
        <div className="flex flex-wrap gap-2">
          {AG_STATUSES.map((s) => (
            <AgBadge key={s} status={s} />
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <AgBadge status="unknown-fallback" />
          <AgBadge status="passed" showIcon={false} />
          <AgBadge status="running" label="自定义文案" />
        </div>
      </section>

      {/* Metric cards */}
      <section className="flex flex-col gap-2">
        <AgSectionHeader eyebrow="metrics" title="AgMetricCard" />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <AgMetricCard label="请求量 (24h)" value="1,284" status="connected" trend="up" hint="较昨日 +12%" />
          <AgMetricCard label="p95 延迟" value="842" unit="ms" status="degraded" trend="up" hint="高于阈值" />
          <AgMetricCard label="错误率" value="0.4" unit="%" status="passed" trend="down" hint="健康" />
          <AgMetricCard label="发布状态" value="—" status="draft" />
        </div>
      </section>
      {/* CHUNK_MARKER */}

      {/* Pills + section header actions */}
      <section className="flex flex-col gap-2">
        <AgSectionHeader
          eyebrow="nav"
          title="AgPill · AgSectionHeader"
          actions={<AgToolbarButton active><Sparkles className="size-3.5" />动作</AgToolbarButton>}
        />
        <div className="flex flex-wrap gap-2">
          <AgPill active>全部</AgPill>
          <AgPill>运行中</AgPill>
          <AgPill>已完成</AgPill>
          <AgPill>失败</AgPill>
        </div>
      </section>

      {/* Panels + cards */}
      <section className="flex flex-col gap-2">
        <AgSectionHeader eyebrow="surface" title="AgPanel · AgCard" />
        <div className="grid grid-cols-2 gap-3">
          <AgPanel tone="surface" className="p-4 text-sm text-ink-soft">surface 面板</AgPanel>
          <AgPanel tone="paper" className="p-4 text-sm text-ink-soft">paper 面板</AgPanel>
        </div>
        <AgCard>
          <span className="font-medium">AgCard 标题</span>
          <span className="text-ink-soft">正文用 ink-soft，提示用 ink-faint。</span>
          <span className="text-ink-faint">这是一段 faint 提示文字。</span>
        </AgCard>
      </section>

      {/* Toolbar + sidebar chrome */}
      <section className="flex flex-col gap-2">
        <AgSectionHeader eyebrow="chrome" title="AgToolbarButton · AgSidebarItem" />
        <AgPanel className="flex items-center gap-1 p-2">
          <AgToolbarButton active><Sparkles className="size-3.5" />Planner</AgToolbarButton>
          <AgToolbarButton>Workbench</AgToolbarButton>
          <AgToolbarButton>Evaluation</AgToolbarButton>
          <AgToolbarButton disabled>禁用</AgToolbarButton>
        </AgPanel>
        <AgPanel className="max-w-xs p-2">
          <AgSidebarItem active icon={<Folder className="size-3.5" />} trailing={<AgBadge status="running" showIcon={false} />}>
            客服机器人会话
          </AgSidebarItem>
          <AgSidebarItem icon={<Folder className="size-3.5" />}>数据分析流程</AgSidebarItem>
          <AgSidebarItem icon={<Folder className="size-3.5" />} trailing={<AgBadge status="passed" showIcon={false} />}>
            已发布智能体
          </AgSidebarItem>
        </AgPanel>
      </section>

      {/* Split pane */}
      <section className="flex flex-col gap-2">
        <AgSectionHeader eyebrow="layout" title="AgSplitPane (ratio 0.35)" />
        <AgPanel className="h-32 overflow-hidden p-0">
          <AgSplitPane
            className="h-full"
            ratio={0.35}
            first={<div className="p-3 text-xs text-ink-soft">左栏 35%</div>}
            second={<div className="p-3 text-xs text-ink-soft">右栏 65%</div>}
          />
        </AgPanel>
      </section>
    </div>
  );
}

export default function PrimitivesPreviewPage() {
  const [showBoth, setShowBoth] = useState(true);
  return (
    <div className="min-h-screen bg-background p-4">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-lg font-semibold">AgentGateway Primitives 预览</h1>
        <span className="text-xs text-muted-foreground">dev-only · 不在主导航</span>
        <AgPill active={showBoth} onClick={() => setShowBoth((v) => !v)}>
          {showBoth ? "并排 light + dark" : "仅 light"}
        </AgPill>
      </div>
      <div className={showBoth ? "grid grid-cols-1 gap-4 lg:grid-cols-2" : ""}>
        {/* Light */}
        <div className="overflow-hidden rounded-xl border border-line">
          <Showcase />
        </div>
        {/* Dark */}
        {showBoth && (
          <div className="dark overflow-hidden rounded-xl border border-line">
            <Showcase />
          </div>
        )}
      </div>
    </div>
  );
}
