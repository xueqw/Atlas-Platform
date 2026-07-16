"use client";
import { useEffect, useRef, useState } from "react";
import { Sparkles, Check, Loader2, Plus, Lock } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import type { PlannerSkillItem } from "@/lib/types";

interface Props {
  conversationId: string | null;
  // Latest selected_skills from the backend ack, lets the parent drive state.
  selectedSkills: string[];
  onToggle: (skillName: string, attach: boolean) => void;
  // Bumping this refetches the list (e.g. after an ack).
  refreshKey?: number;
}

export default function PlannerSelfSkills({ conversationId, selectedSkills, onToggle, refreshKey }: Props) {
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState<PlannerSkillItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    api.listPlannerSkills(conversationId || undefined)
      .then((rows) => { if (!cancelled) setSkills(rows); })
      .catch((e) => { if (!cancelled) setError(e?.message || "加载技能失败"); });
    return () => { cancelled = true; };
  }, [open, conversationId, refreshKey]);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const selected = new Set(selectedSkills);

  // Group skills by tier: defaults (system/workspace) are always-on and shown
  // separately from user-attachable skills. Count reflects user_selected only.
  const defaultSkills = (skills || []).filter(
    (s) => (s.default_source && s.default_source !== "none") || s.is_default,
  );
  const attachableSkills = (skills || []).filter(
    (s) => !((s.default_source && s.default_source !== "none") || s.is_default),
  );
  const attachedCount = attachableSkills.filter(
    (s) => selected.has(s.name) || !!s.attached_to_current,
  ).length;

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-md hover:bg-muted/60 text-muted-foreground hover:text-foreground transition-colors"
        title="规划师能力"
      >
        <Sparkles className="w-3.5 h-3.5" />
        规划师能力
        {attachedCount > 0 && (
          <span className="px-1 rounded bg-primary/15 text-primary text-[10px]">{attachedCount}</span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 bottom-full mb-1 w-80 max-h-96 overflow-y-auto rounded-lg border border-border bg-popover shadow-lg z-50 p-2">
          <div className="px-2 py-1.5 text-[11px] text-muted-foreground border-b border-border/50 mb-1">
            挂载技能后，规划师会在下一轮对话中运用其方法论
          </div>
          {skills === null && !error && (
            <div className="flex items-center justify-center py-6 text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          )}
          {error && <div className="px-2 py-3 text-[11px] text-destructive">{error}</div>}
          {skills && skills.length === 0 && (
            <div className="px-2 py-6 text-[11px] text-muted-foreground text-center">
              暂无可用技能（运行 seed_planner_skills 后出现）
            </div>
          )}

          {/* Default (always-on) skills — backend-authoritative, not detachable. */}
          {defaultSkills.length > 0 && (
            <div className="mb-1">
              <div className="px-2 pt-1 pb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground/70">
                内建能力
              </div>
              {defaultSkills.map((s) => {
                const isWorkspace = s.default_source === "workspace";
                return (
                  <div
                    key={s.name}
                    className="flex items-start gap-2 px-2 py-2 rounded-md opacity-70"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs font-medium truncate">{s.name}</span>
                        <span className="text-[9px] px-1 rounded bg-muted text-muted-foreground">{s.source}</span>
                        <span className="text-[9px] px-1 rounded bg-primary/15 text-primary">
                          {isWorkspace ? "工作区默认" : "系统默认"}
                        </span>
                      </div>
                      <p className="text-[11px] text-muted-foreground line-clamp-2 mt-0.5">{s.description}</p>
                    </div>
                    <span
                      className="shrink-0 inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-md bg-muted text-muted-foreground cursor-default"
                      title={s.lock_reason || "该能力默认启用，无法卸载"}
                    >
                      <Lock className="w-3 h-3" /> 始终启用
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          {/* User-attachable skills. */}
          {attachableSkills.length > 0 && (
            <div>
              {defaultSkills.length > 0 && (
                <div className="px-2 pt-1 pb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground/70">
                  可挂载技能
                </div>
              )}
              {attachableSkills.map((s) => {
                const attached = selected.has(s.name) || !!s.attached_to_current;
                return (
                  <div
                    key={s.name}
                    className="flex items-start gap-2 px-2 py-2 rounded-md hover:bg-muted/50"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs font-medium truncate">{s.name}</span>
                        <span className="text-[9px] px-1 rounded bg-muted text-muted-foreground">{s.source}</span>
                      </div>
                      <p className="text-[11px] text-muted-foreground line-clamp-2 mt-0.5">{s.description}</p>
                    </div>
                    <button
                      onClick={() => onToggle(s.name, !attached)}
                      disabled={!conversationId}
                      className={cn(
                        "shrink-0 inline-flex items-center gap-1 text-[10px] px-2 py-1 rounded-md transition-colors disabled:opacity-50",
                        attached
                          ? "bg-primary/15 text-primary hover:bg-primary/25"
                          : "bg-muted hover:bg-muted/70 text-foreground"
                      )}
                    >
                      {attached ? <><Check className="w-3 h-3" /> 已挂载</> : <><Plus className="w-3 h-3" /> 挂载</>}
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
