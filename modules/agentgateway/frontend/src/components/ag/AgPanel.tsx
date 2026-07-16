"use client";
// AgPanel / AgCard — workbench surface primitives built on the semantic token
// layer. AgPanel is a recessed/raised region container; AgCard is a discrete
// content card. Both forward className through cn() so callers can override.
import * as React from "react";
import { cn } from "@/lib/utils";

export interface AgPanelProps extends React.ComponentProps<"div"> {
  // "surface" sits above the page; "paper" is the recessed background.
  tone?: "surface" | "paper";
}

export function AgPanel({ className, tone = "surface", ...props }: AgPanelProps) {
  return (
    <div
      data-slot="ag-panel"
      className={cn(
        "rounded-xl border border-line",
        tone === "surface" ? "bg-surface" : "bg-paper",
        className,
      )}
      {...props}
    />
  );
}

export function AgCard({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="ag-card"
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-line bg-surface p-4 text-sm text-ink",
        className,
      )}
      {...props}
    />
  );
}
