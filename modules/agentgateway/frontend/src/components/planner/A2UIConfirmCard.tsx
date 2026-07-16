"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { CheckCircle2, MessageSquare } from "lucide-react";

export interface A2UIRequest {
  id: string;
  prompt: string;
  options: Array<{ id: string; label: string; description?: string }>;
  allow_free_text?: boolean;
}

interface Props {
  request: A2UIRequest;
  onSubmit: (choiceId: string, freeText?: string) => void;
  disabled?: boolean;
}

export default function A2UIConfirmCard({ request, onSubmit, disabled }: Props) {
  const [picked, setPicked] = useState<string | null>(null);
  const [freeText, setFreeText] = useState("");
  const [submitted, setSubmitted] = useState(false);

  function submit(choiceId: string) {
    if (submitted || disabled) return;
    setPicked(choiceId);
    setSubmitted(true);
    onSubmit(choiceId, request.allow_free_text ? freeText.trim() : undefined);
  }

  return (
    <div className="rounded-2xl border-2 border-primary/30 bg-primary/5 p-4 space-y-3">
      <div className="flex items-center gap-2 text-xs text-primary font-medium">
        <CheckCircle2 className="w-3.5 h-3.5" />
        需要你确认
      </div>
      <div className="text-sm leading-relaxed">{request.prompt}</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {request.options.map((opt) => {
          const isPicked = picked === opt.id;
          return (
            <button
              key={opt.id}
              onClick={() => submit(opt.id)}
              disabled={submitted || disabled}
              className={`text-left rounded-lg border p-2.5 transition-colors ${
                isPicked
                  ? "border-primary bg-primary/15"
                  : submitted
                  ? "border-border/40 opacity-50 cursor-not-allowed"
                  : "border-border/60 hover:border-primary/60 hover:bg-primary/5"
              }`}
            >
              <div className="text-xs font-medium">{opt.label}</div>
              {opt.description && (
                <div className="text-[10px] text-muted-foreground mt-0.5">{opt.description}</div>
              )}
            </button>
          );
        })}
      </div>
      {request.allow_free_text && !submitted && (
        <div className="flex items-start gap-2">
          <MessageSquare className="w-3.5 h-3.5 text-muted-foreground mt-1 shrink-0" />
          <textarea
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
            placeholder="（可选）补充说明"
            rows={2}
            className="flex-1 text-xs resize-none rounded-md border border-border/60 bg-background px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      )}
      {submitted && (
        <div className="text-[11px] text-muted-foreground italic">
          已记录你的选择，规划师正在基于此继续。
        </div>
      )}
    </div>
  );
}
