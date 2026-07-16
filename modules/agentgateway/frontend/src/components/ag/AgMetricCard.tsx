"use client";
// AgMetricCard — a single metric tile: label, big value (+ optional unit),
// optional status badge, optional trend hint. Used by Monitoring / Release Gate
// to render metrics with consistent semantic-token styling.
import * as React from "react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import { cn } from "@/lib/utils";
import { AgCard } from "./AgPanel";
import { AgBadge } from "./AgBadge";

export interface AgMetricCardProps extends Omit<React.ComponentProps<"div">, "title"> {
  label: string;
  value: React.ReactNode;
  unit?: string;
  status?: string;
  // Direction of change relative to a baseline (purely presentational).
  trend?: "up" | "down" | "flat";
  hint?: string;
}

const TREND_ICON = { up: TrendingUp, down: TrendingDown, flat: Minus } as const;

export function AgMetricCard({
  label, value, unit, status, trend, hint, className, ...props
}: AgMetricCardProps) {
  const TrendIcon = trend ? TREND_ICON[trend] : null;
  return (
    <AgCard className={cn("gap-2", className)} {...props}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-ink-soft">{label}</span>
        {status && <AgBadge status={status} />}
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-semibold text-ink tabular-nums">{value}</span>
        {unit && <span className="text-xs text-ink-faint">{unit}</span>}
      </div>
      {(TrendIcon || hint) && (
        <div className="flex items-center gap-1 text-xs text-ink-faint">
          {TrendIcon && <TrendIcon className="size-3" />}
          {hint}
        </div>
      )}
    </AgCard>
  );
}
