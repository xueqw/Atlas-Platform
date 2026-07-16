"use client";
import { useRef, useState } from "react";
import { X, FileText, AlertTriangle, Plus, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { getApiBase } from "@/lib/runtime-env";
import type { AttachmentMeta } from "@/lib/types";

const MAX_FILES = 4;
const MAX_BYTES = 5 * 1024 * 1024; // 5 MiB (mirrors backend D7)
const ACCEPT = "image/png,image/jpeg,image/webp,image/gif,text/plain,text/markdown,.md,.txt";

function isAllowed(file: File): boolean {
  if (file.type.startsWith("image/")) {
    return ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(file.type);
  }
  if (file.type === "text/plain" || file.type === "text/markdown") return true;
  return /\.(md|txt)$/i.test(file.name);
}

// Thumbnail strip for already-uploaded attachments. Lives at the top-left of the
// composer; the "+" trigger ({@link AttachmentButton}) owns the upload flow.
export function AttachmentPreviews({
  attachments,
  onChange,
  imageSupported,
}: {
  attachments: AttachmentMeta[];
  onChange: (next: AttachmentMeta[]) => void;
  imageSupported: boolean;
}) {
  if (attachments.length === 0) return null;
  function removeAt(idx: number) {
    onChange(attachments.filter((_, i) => i !== idx));
  }
  return (
    <div className="flex flex-wrap gap-2">
      {attachments.map((att, i) => (
        <div key={att.path} className="relative group">
          {att.kind === "image" ? (
            <div className="relative w-14 h-14 rounded-lg overflow-hidden border border-border">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={`${getApiBase()}${att.preview_url}`} alt={att.name} className="w-full h-full object-cover" />
              {!imageSupported && (
                <div className="absolute inset-0 bg-black/55 flex items-center justify-center text-[9px] text-white text-center px-1">
                  <span className="flex flex-col items-center gap-0.5">
                    <AlertTriangle className="w-3 h-3" /> 已忽略
                  </span>
                </div>
              )}
            </div>
          ) : (
            <div className="w-14 h-14 rounded-lg border border-border bg-muted/40 flex flex-col items-center justify-center gap-1 px-1">
              <FileText className="w-4 h-4 text-muted-foreground" />
              <span className="text-[9px] text-muted-foreground truncate w-full text-center">{att.name}</span>
            </div>
          )}
          <button
            onClick={() => removeAt(i)}
            className="absolute -top-1.5 -right-1.5 bg-background border border-border rounded-full p-0.5 opacity-0 group-hover:opacity-100 transition-opacity hover:text-destructive"
            title="移除附件"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      ))}
    </div>
  );
}

// Icon-only "+" upload trigger. Owns the hidden file input, validation, and the
// upload-to-backend round trip; reports results upward via onChange.
export function AttachmentButton({
  conversationId,
  attachments,
  onChange,
  disabled,
}: {
  conversationId: string | null;
  attachments: AttachmentMeta[];
  onChange: (next: AttachmentMeta[]) => void;
  disabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);

  async function addFiles(files: FileList | File[]) {
    if (!conversationId) {
      toast.error("会话尚未就绪，请稍候再上传");
      return;
    }
    const list = Array.from(files);
    if (attachments.length + list.length > MAX_FILES) {
      toast.error(`单次最多附带 ${MAX_FILES} 个附件`);
      return;
    }
    setUploading(true);
    const accepted: AttachmentMeta[] = [];
    for (const file of list) {
      if (!isAllowed(file)) {
        toast.error(`不支持的文件类型：${file.name}`);
        continue;
      }
      if (file.size > MAX_BYTES) {
        toast.error(`${file.name} 超过 5 MiB，请压缩后再传`);
        continue;
      }
      try {
        const meta = await api.uploadPlannerAttachment(conversationId, file);
        accepted.push(meta);
      } catch (e) {
        toast.error((e as Error)?.message || `上传失败：${file.name}`);
      }
    }
    setUploading(false);
    if (accepted.length) onChange([...attachments, ...accepted]);
  }

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        multiple
        className="hidden"
        onChange={(e) => { if (e.target.files) void addFiles(e.target.files); e.target.value = ""; }}
      />
      <button
        type="button"
        disabled={disabled || uploading || attachments.length >= MAX_FILES}
        onClick={() => inputRef.current?.click()}
        className={cn(
          "inline-flex items-center justify-center w-8 h-8 rounded-lg text-muted-foreground",
          "hover:text-foreground hover:bg-muted/60 transition-colors",
          "disabled:opacity-50 disabled:cursor-not-allowed"
        )}
        title="添加附件（图片 / .md / .txt，最多 4 个 / 5 MiB）"
      >
        {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
      </button>
    </>
  );
}

// Backward-compatible bundle: previews on top, "+" trigger below. New layouts
// should compose AttachmentPreviews / AttachmentButton directly.
export default function AttachmentComposer(props: {
  conversationId: string | null;
  attachments: AttachmentMeta[];
  onChange: (next: AttachmentMeta[]) => void;
  imageSupported: boolean;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-2">
      <AttachmentPreviews attachments={props.attachments} onChange={props.onChange} imageSupported={props.imageSupported} />
      <AttachmentButton
        conversationId={props.conversationId}
        attachments={props.attachments}
        onChange={props.onChange}
        disabled={props.disabled}
      />
    </div>
  );
}
