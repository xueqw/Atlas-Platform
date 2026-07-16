// Status presentation — the single source of truth for how a workbench
// lifecycle state is shown (label, color token, icon, tone). Badges, metric
// cards, and any other status indicator MUST read from here instead of
// hardcoding colors or copy, so the same state always looks the same.
import {
  Circle,
  Loader2,
  Wifi,
  WifiOff,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  FileEdit,
  Rocket,
  type LucideIcon,
} from "lucide-react";

// The lifecycle states the workbench expresses. Kept as a union so call sites
// get autocomplete and the exhaustive map below is type-checked.
export type AgStatus =
  | "idle"
  | "active"
  | "running"
  | "connected"
  | "reconnecting"
  | "stale"
  | "degraded"
  | "passed"
  | "failed"
  | "draft"
  | "published";

// Coarse tone bucket — lets consumers pick a background/border treatment
// without re-deriving it from the specific status.
export type AgTone = "neutral" | "info" | "progress" | "success" | "warning" | "danger";

export interface StatusPresentation {
  label: string;
  // Tailwind text-color utility backed by a --color-status-* token.
  tokenClass: string;
  Icon: LucideIcon;
  tone: AgTone;
  // Whether this state is "live" (icon should spin / pulse).
  live: boolean;
}

const PRESENTATIONS: Record<AgStatus, StatusPresentation> = {
  idle:         { label: "空闲",   tokenClass: "text-status-idle",         Icon: Circle,        tone: "neutral",  live: false },
  active:       { label: "活动",   tokenClass: "text-status-active",       Icon: Circle,        tone: "info",     live: false },
  running:      { label: "运行中", tokenClass: "text-status-running",      Icon: Loader2,       tone: "progress", live: true  },
  connected:    { label: "已连接", tokenClass: "text-status-connected",    Icon: Wifi,          tone: "success",  live: false },
  reconnecting: { label: "重连中", tokenClass: "text-status-reconnecting", Icon: Loader2,       tone: "progress", live: true  },
  stale:        { label: "已过期", tokenClass: "text-status-stale",        Icon: WifiOff,       tone: "neutral",  live: false },
  degraded:     { label: "降级",   tokenClass: "text-status-degraded",     Icon: AlertTriangle, tone: "warning",  live: false },
  passed:       { label: "通过",   tokenClass: "text-status-passed",       Icon: CheckCircle2,  tone: "success",  live: false },
  failed:       { label: "失败",   tokenClass: "text-status-failed",       Icon: XCircle,       tone: "danger",   live: false },
  draft:        { label: "草稿",   tokenClass: "text-status-draft",        Icon: FileEdit,      tone: "neutral",  live: false },
  published:    { label: "已发布", tokenClass: "text-status-published",    Icon: Rocket,        tone: "info",     live: false },
};

// All known statuses (handy for preview grids / validation).
export const AG_STATUSES = Object.keys(PRESENTATIONS) as AgStatus[];

// Map a status to its presentation. Unknown values degrade to the neutral
// "idle" presentation rather than throwing, so a stray backend string can
// never crash a render.
export function statusPresentation(status: string): StatusPresentation {
  return PRESENTATIONS[status as AgStatus] ?? PRESENTATIONS.idle;
}
