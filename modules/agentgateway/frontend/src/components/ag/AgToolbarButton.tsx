"use client";
// AgToolbarButton / AgSidebarItem — chrome primitives reserved for the future
// workbench shell. AgToolbarButton is a compact top-toolbar action; AgSidebarItem
// is a left-rail object row with an active state and optional trailing slot.
import * as React from "react";
import { cn } from "@/lib/utils";

export interface AgToolbarButtonProps extends React.ComponentProps<"button"> {
  active?: boolean;
}

export function AgToolbarButton({ active = false, className, children, ...props }: AgToolbarButtonProps) {
  return (
    <button
      type="button"
      data-slot="ag-toolbar-button"
      data-active={active}
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium transition-colors",
        active ? "bg-paper text-ink" : "text-ink-soft hover:bg-paper hover:text-ink",
        "disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}

export interface AgSidebarItemProps extends React.ComponentProps<"button"> {
  active?: boolean;
  icon?: React.ReactNode;
  trailing?: React.ReactNode;
}

export function AgSidebarItem({ active = false, icon, trailing, className, children, ...props }: AgSidebarItemProps) {
  return (
    <button
      type="button"
      data-slot="ag-sidebar-item"
      data-active={active}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors",
        active ? "bg-paper font-medium text-ink" : "text-ink-soft hover:bg-paper hover:text-ink",
        className,
      )}
      {...props}
    >
      {icon && <span className="shrink-0 text-ink-faint">{icon}</span>}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {trailing && <span className="shrink-0">{trailing}</span>}
    </button>
  );
}
