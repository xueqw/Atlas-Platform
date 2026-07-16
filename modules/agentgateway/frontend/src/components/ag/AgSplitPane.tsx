"use client";
// AgSplitPane — a two-pane split (horizontal or vertical) with a configurable
// ratio and a semantic line divider. Ratio is the fraction (0–1) given to the
// first pane. Layout only; not a draggable resizer (that can come later).
import * as React from "react";
import { cn } from "@/lib/utils";

export interface AgSplitPaneProps extends Omit<React.ComponentProps<"div">, "children"> {
  first: React.ReactNode;
  second: React.ReactNode;
  // Orientation of the split. "horizontal" = side by side, "vertical" = stacked.
  orientation?: "horizontal" | "vertical";
  // Fraction of space for the first pane (0–1). Defaults to an even split.
  ratio?: number;
}

export function AgSplitPane({
  first, second, orientation = "horizontal", ratio = 0.5, className, ...props
}: AgSplitPaneProps) {
  const r = Math.min(0.9, Math.max(0.1, ratio));
  const isH = orientation === "horizontal";
  return (
    <div
      data-slot="ag-split-pane"
      data-orientation={orientation}
      className={cn("flex min-h-0 min-w-0", isH ? "flex-row" : "flex-col", className)}
      {...props}
    >
      <div className="min-h-0 min-w-0 overflow-auto" style={{ flexBasis: `${r * 100}%`, flexGrow: 0, flexShrink: 1 }}>
        {first}
      </div>
      <div className={cn("shrink-0 bg-line", isH ? "w-px" : "h-px")} aria-hidden />
      <div className="min-h-0 min-w-0 flex-1 overflow-auto">{second}</div>
    </div>
  );
}
