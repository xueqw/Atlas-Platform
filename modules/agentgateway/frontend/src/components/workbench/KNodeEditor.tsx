"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { api } from "@/lib/api";
import type { KBDocument, RetrieveChunk } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { toast } from "sonner";
import { Upload, File, Trash2, Search, Loader2 } from "lucide-react";

const ALLOWED_TYPES = [".pdf", ".txt", ".md"];
const MAX_SIZE = 10 * 1024 * 1024;

interface Props {
  agentId: number;
}

export default function KNodeEditor({ agentId }: Props) {
  const [documents, setDocuments] = useState<KBDocument[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<RetrieveChunk[]>([]);
  const [retrieving, setRetrieving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadDocs = useCallback(() => {
    api.listKBDocuments(agentId).then(setDocuments).catch(() => {});
  }, [agentId]);

  useEffect(() => { loadDocs(); }, [loadDocs]);

  const handleFiles = async (files: FileList | File[]) => {
    const arr = Array.from(files);
    const invalid = arr.filter((f) => {
      const ext = "." + f.name.split(".").pop()?.toLowerCase();
      return !ALLOWED_TYPES.includes(ext);
    });
    if (invalid.length > 0) {
      toast.error(`不支持的文件类型: ${invalid.map((f) => f.name).join(", ")}`);
      return;
    }
    const tooBig = arr.filter((f) => f.size > MAX_SIZE);
    if (tooBig.length > 0) {
      toast.error(`文件过大（>10MB）: ${tooBig.map((f) => f.name).join(", ")}`);
      return;
    }
    setUploading(true);
    try {
      const result = await api.uploadDocuments(agentId, arr);
      toast.success(`已索引 ${result.document_count} 个文档，共 ${result.chunk_count} 个块`);
      loadDocs();
    } catch (e: any) {
      toast.error(e.message || "上传失败");
    }
    setUploading(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
  };

  const handleDelete = async (filename: string) => {
    if (!confirm(`确定要删除 "${filename}" 及其所有块吗？`)) return;
    try {
      await api.deleteKBDocument(agentId, filename);
      toast.success("文档已删除");
      loadDocs();
    } catch (e: any) {
      toast.error(e.message || "删除失败");
    }
  };

  const handleRetrieve = async () => {
    if (!query.trim()) return;
    setRetrieving(true);
    try {
      const data = await api.retrieveChunks(agentId, query);
      setResults(data.results);
    } catch (e: any) {
      toast.error(e.message || "检索失败");
    }
    setRetrieving(false);
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Upload zone */}
      <Card>
        <CardHeader className="py-3">
          <CardTitle className="text-sm">上传文档</CardTitle>
        </CardHeader>
        <CardContent>
          <div
            className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors ${
              dragOver ? "border-primary bg-primary/5" : "border-border"
            }`}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
          >
            <Upload className="w-8 h-8 mx-auto mb-2 text-muted-foreground" />
            <p className="text-sm text-muted-foreground mb-2">
              拖拽文件到此处，或点击浏览
            </p>
            <p className="text-xs text-muted-foreground">
              PDF、TXT、MD — 每个最大 10MB
            </p>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept=".pdf,.txt,.md"
              className="hidden"
              onChange={(e) => e.target.files && handleFiles(e.target.files)}
            />
            <Button
              variant="outline"
              size="sm"
              className="mt-3"
              disabled={uploading}
              onClick={() => inputRef.current?.click()}
            >
              {uploading ? (
                <><Loader2 className="w-4 h-4 mr-2 animate-spin" /> 索引中...</>
              ) : (
                "浏览文件"
              )}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Document list */}
      <Card>
        <CardHeader className="py-3">
          <CardTitle className="text-sm">
            文档列表 ({documents.length})
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <ScrollArea className="h-48">
            {documents.length === 0 ? (
              <p className="text-sm text-muted-foreground p-4">暂无上传文档</p>
            ) : (
              <div className="divide-y">
                {documents.map((doc) => (
                  <div key={doc.filename} className="flex items-center gap-3 p-3">
                    <File className="w-4 h-4 text-muted-foreground shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm truncate">{doc.filename}</p>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-xs text-muted-foreground">
                          {doc.chunk_count} 个块
                        </span>
                        <Badge variant="outline" className="text-xs">
                          {doc.status}
                        </Badge>
                      </div>
                    </div>
                    <Button variant="ghost" size="icon" onClick={() => handleDelete(doc.filename)}>
                      <Trash2 className="w-4 h-4 text-muted-foreground" />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </ScrollArea>
        </CardContent>
      </Card>

      {/* Retrieval test */}
      <Card>
        <CardHeader className="py-3">
          <CardTitle className="text-sm">检索测试</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex gap-2 mb-3">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入查询以测试检索..."
              onKeyDown={(e) => e.key === "Enter" && handleRetrieve()}
            />
            <Button size="sm" onClick={handleRetrieve} disabled={retrieving}>
              {retrieving ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Search className="w-4 h-4" />
              )}
            </Button>
          </div>
          {results.length > 0 && (
            <ScrollArea className="h-64">
              <div className="space-y-3">
                {results.map((r, i) => (
                  <div key={r.chunk_id} className="border rounded-lg p-3">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-medium text-muted-foreground">
                        {r.source} (块 {r.chunk_index})
                      </span>
                      <Badge variant="secondary" className="text-xs">
                        相似度: {(r.score * 100).toFixed(1)}%
                      </Badge>
                    </div>
                    <p className="text-sm whitespace-pre-wrap">{r.content}</p>
                  </div>
                ))}
              </div>
            </ScrollArea>
          )}
        </CardContent>
      </Card>
    </div>
  );
}