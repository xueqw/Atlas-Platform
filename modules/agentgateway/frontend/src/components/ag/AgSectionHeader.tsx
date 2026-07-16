"use client";
// AgSectionHeader / AgPill — small structural primitives. AgSectionHeader is an
// eyebrow-style block title (optional small-caps eyebrow + title + actions).
// AgPill is a compact tab/filter pill with an active state.
import * as React from "react";
import { cn } from "@/lib/utils";

export interface AgSectionHeaderProps extends Omit<React.ComponentProps<"div">, "title"> {
  eyebrow?: string;
  title: React.ReactNode;
  actions?: React.ReactNode;
}

export function AgSectionHeader({ eyebrow, title, actions, className, ...props }: AgSectionHeaderProps) {
  return (
    <div
      data-slot="ag-section-header"
      className={cn("flex items-end justify-between gap-3 border-b border-line-soft pb-2", className)}
      {...props}
    >
      <div className="flex flex-col gap-0.5">
        {eyebrow && (
          <span className="text-[10px] font-medium uppercase tracking-wider text-ink-faint">
            {eyebrow}
          </span>
        )}
        <span className="text-sm font-semibold text-ink">{title}</span>
      </div>
      {actions && <div className="flex items-center gap-1">{actions}</div>}
    </div>
  );
}

export interface AgPillProps extends React.ComponentProps<"button"> {
  active?: boolean;
}

export function AgPill({ active = false, className, ...props }: AgPillProps) {
  return (
    <button
      type="button"
      data-slot="ag-pill"
      data-active={active}
      className={cn(
        "inline-flex h-7 items-center gap-1 rounded-full border px-3 text-xs font-medium transition-colors",
        active
          ? "border-transparent bg-ink text-paper"
          : "border-line bg-surface text-ink-soft hover:text-ink",
        className,
      )}
      {...props}
    />
  );
}
