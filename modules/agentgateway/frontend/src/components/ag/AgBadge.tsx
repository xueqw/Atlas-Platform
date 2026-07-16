"use client";
// AgBadge — status badge that consumes statusPresentation as the single source
// of truth for label/color/icon. Pass a `status`; optionally override the label
// or hide the icon. Falls back to a neutral idle presentation for unknown values.
import * as React from "react";
import { cn } from "@/lib/utils";
import { statusPresentation } from "@/lib/status-presentation";

export interface AgBadgeProps extends React.ComponentProps<"span"> {
  status: string;
  // Override the default Chinese label from the presentation map.
  label?: string;
  showIcon?: boolean;
}

export function AgBadge({ status, label, showIcon = true, className, ...props }: AgBadgeProps) {
  const p = statusPresentation(status);
  const Icon = p.Icon;
  return (
    <span
      data-slot="ag-badge"
      data-status={status}
      className={cn(
        "inline-flex h-5 w-fit shrink-0 items-center gap-1 rounded-full border border-line-soft bg-surface px-2 py-0.5 text-xs font-medium",
        p.tokenClass,
        className,
      )}
      {...props}
    >
      {showIcon && <Icon className={cn("size-3", p.live && "animate-spin")} />}
      {label ?? p.label}
    </span>
  );
}
