"use client";

import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import type { ToolConfig } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { toast } from "sonner";
import { Plus, Pencil, Trash2, Wrench } from "lucide-react";

interface Props {
  agentId: number;
}

export default function TNodeEditor({ agentId }: Props) {
  const [tools, setTools] = useState<ToolConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [editDialog, setEditDialog] = useState(false);
  const [editing, setEditing] = useState<Partial<ToolConfig> | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ToolConfig | null>(null);

  const loadTools = useCallback(() => {
    api.listTools(agentId).then(setTools).finally(() => setLoading(false));
  }, [agentId]);

  useEffect(() => { loadTools(); }, [loadTools]);

  const handleSave = async () => {
    if (!editing || !editing.name?.trim()) return;
    try {
      if (editing.id) {
        await api.updateTool(agentId, editing.id, {
          name: editing.name || "",
          description: editing.description || "",
          parameters: editing.parameters || "{}",
          mock_endpoint: editing.mock_endpoint || "",
        });
        toast.success("工具已更新");
      } else {
        await api.createTool(agentId, {
          name: editing.name,
          description: editing.description || "",
          parameters: editing.parameters || "{}",
          mock_endpoint: editing.mock_endpoint || "",
        });
        toast.success("工具已创建");
      }
      setEditDialog(false);
      setEditing(null);
      loadTools();
    } catch {
      toast.error("操作失败");
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.deleteTool(agentId, deleteTarget.id);
      toast.success("工具已删除");
      setDeleteTarget(null);
      loadTools();
    } catch {
      toast.error("删除失败");
    }
  };

  if (loading) return <p className="text-sm text-muted-foreground">加载中...</p>;

  return (
    <div className="flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">工具配置</h3>
          <p className="text-sm text-muted-foreground">
            定义智能体可调用的工具。工具使用 JSON Schema 描述参数，将被注入到 AgentScope ReActAgent 中。
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => setEditing({ name: "", description: "", parameters: "{}", mock_endpoint: "" })}
        >
          <Plus className="w-4 h-4 mr-1" />添加工具
        </Button>
      </div>

      {tools.length === 0 ? (
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground">
            <Wrench className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">暂无工具配置</p>
            <p className="text-xs mt-1">点击「添加工具」定义智能体的工具能力</p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {tools.map((t) => (
            <Card key={t.id}>
              <CardHeader className="py-3 px-4">
                <div className="flex items-start justify-between">
                  <div className="space-y-1">
                    <CardTitle className="text-sm flex items-center gap-2">
                      <Wrench className="w-4 h-4" />
                      {t.name}
                    </CardTitle>
                    <p className="text-xs text-muted-foreground">{t.description}</p>
                    {t.mock_endpoint && (
                      <Badge variant="outline" className="text-xs">
                        Mock: {t.mock_endpoint}
                      </Badge>
                    )}
                  </div>
                  <div className="flex gap-1">
                    <Button
                      variant="ghost" size="icon" className="w-7 h-7"
                      onClick={() => setEditing(t)}
                    >
                      <Pencil className="w-3.5 h-3.5" />
                    </Button>
                    <Button
                      variant="ghost" size="icon" className="w-7 h-7"
                      onClick={() => setDeleteTarget(t)}
                    >
                      <Trash2 className="w-3.5 h-3.5 text-destructive" />
                    </Button>
                  </div>
                </div>
                <div className="mt-2">
                  <p className="text-xs text-muted-foreground mb-1">参数 Schema:</p>
                  <pre className="text-xs bg-muted p-2 rounded max-h-32 overflow-y-auto">
                    {(() => { try { return JSON.stringify(JSON.parse(t.parameters), null, 2); } catch { return t.parameters; } })()}
                  </pre>
                </div>
              </CardHeader>
            </Card>
          ))}
        </div>
      )}

      {/* Edit dialog */}
      <Dialog open={!!editing} onOpenChange={(open) => { if (!open) setEditing(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing?.id ? "编辑工具" : "添加工具"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={editing?.name || ""}
                onChange={(e) => setEditing((prev) => ({ ...prev, name: e.target.value }))}
                placeholder="例如：天气查询"
              />
            </div>
            <div>
              <Label>描述</Label>
              <Input
                value={editing?.description || ""}
                onChange={(e) => setEditing((prev) => ({ ...prev, description: e.target.value }))}
                placeholder="描述工具的功能"
              />
            </div>
            <div>
              <Label>参数 JSON Schema</Label>
              <Textarea
                className="font-mono text-sm"
                rows={6}
                value={editing?.parameters || "{}"}
                onChange={(e) => setEditing((prev) => ({ ...prev, parameters: e.target.value }))}
                placeholder='{"type": "object", "properties": {...}}'
              />
            </div>
            <div>
              <Label>Mock 端点（可选）</Label>
              <Input
                value={editing?.mock_endpoint || ""}
                onChange={(e) => setEditing((prev) => ({ ...prev, mock_endpoint: e.target.value }))}
                placeholder="例如：https://mock-api.example.com/weather"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
            <Button onClick={handleSave} disabled={!editing?.name?.trim()}>保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={() => setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p>确定要删除工具「{deleteTarget?.name}」吗？</p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>取消</Button>
            <Button variant="destructive" onClick={handleDelete}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}