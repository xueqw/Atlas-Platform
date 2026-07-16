"use client";
import { useEffect, useState, useCallback, useRef } from "react";
import { api } from "@/lib/api";
import type { CapabilityItem } from "@/lib/types";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Plus, Trash2, Pencil, Search, Upload, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { getApiBase } from "@/lib/runtime-env";

const TYPE_OPTIONS = [
  { value: "prompt", label: "提示词" },
  { value: "model", label: "模型" },
  { value: "tool", label: "工具" },
  { value: "skill", label: "技能" },
  { value: "expert_template", label: "专家模板" },
];

export default function CapabilitiesPage() {
  const [items, setItems] = useState<CapabilityItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [editDialog, setEditDialog] = useState(false);
  const [editItem, setEditItem] = useState<Partial<CapabilityItem> | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CapabilityItem | null>(null);
  const [modelTestPassed, setModelTestPassed] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    setLoading(true);
    api.listCapabilities(typeFilter !== "all" ? typeFilter : undefined)
      .then(setItems)
      .finally(() => setLoading(false));
  }, [typeFilter]);

  useEffect(() => { load(); }, [load]);

  const syncPlatformCapabilities = useCallback(async (quiet = false) => {
    setSyncing(true);
    try {
      const [skillsResponse, connectorsResponse] = await Promise.all([
        fetch("/platform-api/skills", { credentials: "include" }),
        fetch("/platform-api/connectors", { credentials: "include" }),
      ]);
      if (!skillsResponse.ok || !connectorsResponse.ok) {
        throw new Error("无法读取主平台能力，请先确认平台登录状态");
      }
      const skills = await skillsResponse.json();
      const connectorPayload = await connectorsResponse.json();
      const result = await api.syncPlatformCapabilities({
        skills: Array.isArray(skills) ? skills : [],
        connectors: connectorPayload.connectors || [],
      });
      if (!quiet) toast.success(`已同步 ${result.skills} 个 Skills 和 ${result.connectors} 个连接器`);
      load();
    } catch (error) {
      if (!quiet) toast.error(error instanceof Error ? error.message : "同步主平台能力失败");
    } finally {
      setSyncing(false);
    }
  }, [load]);

  useEffect(() => { void syncPlatformCapabilities(true); }, [syncPlatformCapabilities]);

  const handleSave = async () => {
    if (!editItem) return;
    try {
      if (editItem.id) {
        await api.updateCapability(editItem.id, {
          type: editItem.type || "prompt",
          name: editItem.name || "",
          description: editItem.description || "",
          tags: editItem.tags || "[]",
          config: editItem.config || "{}",
        });
        toast.success("已更新");
      } else {
        await api.createCapability({
          type: editItem.type || "prompt",
          name: editItem.name || "",
          description: editItem.description || "",
          tags: editItem.tags || "[]",
          config: editItem.config || "{}",
        });
        toast.success("已创建");
      }
      setEditDialog(false);
      setEditItem(null);
      load();
    } catch {
      toast.error("操作失败");
    }
  };

  const handleModelSave = async () => {
    if (!editItem) return;
    if (editItem.type === "model") {
      try {
        const cfg = JSON.parse(editItem.config || "{}");
        const desc = `${cfg.provider || "custom"}/${cfg.model_id || "unknown"} — Base: ${cfg.base_url || "默认"}`;
        const item = { ...editItem, description: desc, tags: JSON.stringify([cfg.provider || "custom", "model"]) };
        if (item.id) {
          await api.updateCapability(item.id, {
            type: "model", name: item.name || "", description: desc,
            tags: item.tags || "[]", config: item.config || "{}",
          });
          toast.success("已更新");
        } else {
          await api.createCapability({
            type: "model", name: item.name || "", description: desc,
            tags: item.tags || "[]", config: item.config || "{}",
          });
          toast.success("已创建");
        }
        setEditDialog(false);
        setEditItem(null);
        setModelTestPassed(false);
        load();
      } catch {
        toast.error("保存失败");
      }
    } else {
      await handleSave();
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.deleteCapability(deleteTarget.id);
      setDeleteTarget(null);
      load();
      toast.success("已删除");
    } catch {
      toast.error("删除失败");
    }
  };

  const handleSearch = async () => {
    if (!search.trim()) {
      load();
      return;
    }
    setLoading(true);
    api.searchCapabilities(search).then(setItems).finally(() => setLoading(false));
  };

  const handleUploadBundle = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (e.target) e.target.value = ""; // allow re-selecting the same file
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".zip")) {
      toast.error("请选择 .zip 技能包");
      return;
    }
    setUploading(true);
    try {
      const res = await api.uploadSkillBundle(file);
      toast.success(`已上传技能「${res.name}」v${res.version}：${res.description || "（无简介）"}`);
      if (typeFilter !== "all" && typeFilter !== "skill") setTypeFilter("skill");
      else load();
    } catch (err) {
      toast.error(`上传失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setUploading(false);
    }
  };

  const filtered = items;

  return (
    <div className="flex-1 max-w-5xl mx-auto w-full p-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">能力库</h1>
        <div className="flex items-center gap-2">
          <input
            ref={fileInputRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={handleUploadBundle}
          />
          <Button
            variant="outline"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload className="w-4 h-4 mr-1" />
            {uploading ? "上传中..." : "上传技能包(.zip)"}
          </Button>
          <Button variant="outline" disabled={syncing} onClick={() => void syncPlatformCapabilities()}>
            <RefreshCw className={`w-4 h-4 mr-1 ${syncing ? "animate-spin" : ""}`} />
            {syncing ? "同步中..." : "同步主平台能力"}
          </Button>
          <Button onClick={() => { setEditItem({ type: "prompt" }); setEditDialog(true); }}>
            <Plus className="w-4 h-4 mr-1" />
            添加能力
          </Button>
        </div>
      </div>

      <div className="flex items-center gap-3 mb-4">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-2.5 top-2.5 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="搜索能力..."
            className="pl-9"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          />
        </div>
        <Button variant="outline" size="sm" onClick={handleSearch}>搜索</Button>
        <Tabs value={typeFilter} onValueChange={setTypeFilter}>
          <TabsList>
            <TabsTrigger value="all">全部</TabsTrigger>
            <TabsTrigger value="prompt">提示词</TabsTrigger>
            <TabsTrigger value="model">模型</TabsTrigger>
            <TabsTrigger value="tool">工具</TabsTrigger>
            <TabsTrigger value="skill">技能</TabsTrigger>
            <TabsTrigger value="expert_template">专家模板</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {loading ? (
        <p className="text-muted-foreground">加载中...</p>
      ) : filtered.length === 0 ? (
        <p className="text-muted-foreground">暂无能力项</p>
      ) : (
        <div className="grid gap-3">
          {filtered.map((item) => {
            const typeLabel = TYPE_OPTIONS.find((t) => t.value === item.type)?.label || item.type;
            let tags: string[] = [];
            try { tags = JSON.parse(item.tags); } catch { /* ignore */ }
            let uploadedVersion: number | null = null;
            if (item.type === "skill") {
              try {
                const cfg = JSON.parse(item.config || "{}");
                if (cfg?.source === "uploaded") uploadedVersion = Number(cfg.version) || 1;
              } catch { /* ignore */ }
            }
            return (
              <Card key={item.id}>
                <CardHeader className="py-3 px-4">
                  <div className="flex items-start justify-between">
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <Badge variant="outline">{typeLabel}</Badge>
                        {uploadedVersion !== null && (
                          <Badge variant="secondary" className="text-xs">已上传 v{uploadedVersion}</Badge>
                        )}
                        <CardTitle className="text-base">{item.name}</CardTitle>
                      </div>
                      <CardDescription>{item.description}</CardDescription>
                      {tags.length > 0 && (
                        <div className="flex gap-1 flex-wrap">
                          {tags.map((t, i) => (
                            <Badge key={i} variant="secondary" className="text-xs">{t}</Badge>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="flex gap-1">
                      <Button
                        variant="ghost" size="icon" className="w-8 h-8"
                        onClick={() => { setEditItem(item); setEditDialog(true); }}
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </Button>
                      <Button
                        variant="ghost" size="icon" className="w-8 h-8 text-destructive"
                        onClick={() => setDeleteTarget(item)}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </Button>
                    </div>
                  </div>
                </CardHeader>
              </Card>
            );
          })}
        </div>
      )}

      {/* Edit Dialog */}
      <Dialog open={editDialog} onOpenChange={setEditDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editItem?.id ? "编辑能力" : "添加能力"}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label>类型</Label>
              <Select
                value={editItem?.type || "prompt"}
                onValueChange={(v) => setEditItem((prev) => ({ ...prev, type: v || "prompt" }))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TYPE_OPTIONS.map((t) => (
                    <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>名称</Label>
              <Input
                value={editItem?.name || ""}
                onChange={(e) => setEditItem((prev) => ({ ...prev, name: e.target.value }))}
              />
            </div>

            {/* Model type: specialized form */}
            {editItem?.type === "model" ? (
              <ModelConfigForm
                config={editItem?.config || "{}"}
                onChange={(cfg) => setEditItem((prev) => ({ ...prev, config: cfg }))}
                onTestResult={setModelTestPassed}
              />
            ) : editItem?.type === "skill" ? (
              <SkillConfigForm
                name={editItem?.name || ""}
                description={editItem?.description || ""}
                config={editItem?.config || "{}"}
                tags={editItem?.tags || "[]"}
                onChangeDescription={(v) => setEditItem((prev) => ({ ...prev, description: v }))}
                onChangeConfig={(cfg) => setEditItem((prev) => ({ ...prev, config: cfg }))}
                onChangeTags={(v) => setEditItem((prev) => ({ ...prev, tags: v }))}
              />
            ) : (
              <>
                <div>
                  <Label>描述</Label>
                  <Textarea
                    value={editItem?.description || ""}
                    onChange={(e) => setEditItem((prev) => ({ ...prev, description: e.target.value }))}
                  />
                </div>
                <div>
                  <Label>标签 (JSON 数组)</Label>
                  <Input
                    value={editItem?.tags || "[]"}
                    onChange={(e) => setEditItem((prev) => ({ ...prev, tags: e.target.value }))}
                    placeholder='["tag1","tag2"]'
                  />
                </div>
                <div>
                  <Label>配置 (JSON)</Label>
                  <Textarea
                    className="font-mono text-sm"
                    rows={6}
                    value={editItem?.config || "{}"}
                    onChange={(e) => setEditItem((prev) => ({ ...prev, config: e.target.value }))}
                  />
                </div>
              </>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialog(false)}>取消</Button>
            <Button onClick={handleModelSave} disabled={editItem?.type === "model" && !modelTestPassed}>
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation */}
      <Dialog open={!!deleteTarget} onOpenChange={() => setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p>确定要删除「{deleteTarget?.name}」吗？此操作不可撤销。</p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>取消</Button>
            <Button variant="destructive" onClick={handleDelete}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/* ── Model Config Form ── */
function ModelConfigForm({ config, onChange, onTestResult }: {
  config: string;
  onChange: (cfg: string) => void;
  onTestResult: (passed: boolean) => void;
}) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  let parsed: Record<string, string> = {};
  try { parsed = JSON.parse(config); } catch { /* ignore */ }

  const update = (key: string, value: string) => {
    const next = { ...parsed, [key]: value };
    onChange(JSON.stringify(next));
    onTestResult(false);
    setTestResult(null);
  };

  const handleTest = async () => {
    const baseUrl = parsed.base_url || "";
    const apiKey = parsed.api_key || "";
    const modelId = parsed.model_id || "";
    if (!baseUrl || !modelId) {
      setTestResult({ success: false, message: "请填写 Base URL 和 Model ID" });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const res = await fetch(`${getApiBase()}/api/capabilities/test-model`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model_id: modelId }),
      });
      const data = await res.json();
      setTestResult(data);
      onTestResult(data.success);
    } catch {
      setTestResult({ success: false, message: "请求失败，请确认后端服务已启动" });
      onTestResult(false);
    }
    setTesting(false);
  };

  return (
    <div className="space-y-3 border border-border rounded-lg p-3">
      <p className="text-xs text-muted-foreground font-medium">模型配置</p>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label className="text-xs">Provider</Label>
          <Select value={parsed.provider || "glm"} onValueChange={(v) => update("provider", v ?? "glm")}>
            <SelectTrigger className="h-8 text-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="openai">OpenAI</SelectItem>
              <SelectItem value="anthropic">Anthropic</SelectItem>
              <SelectItem value="deepseek">DeepSeek</SelectItem>
              <SelectItem value="glm">GLM / 自定义</SelectItem>
              <SelectItem value="custom">Custom</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs">Model ID</Label>
          <Input className="h-8 text-sm" value={parsed.model_id || ""} onChange={(e) => update("model_id", e.target.value)} placeholder="qwen3.6-27b" />
        </div>
      </div>
      <div>
        <Label className="text-xs">Base URL</Label>
        <Input className="h-8 text-sm" value={parsed.base_url || ""} onChange={(e) => update("base_url", e.target.value)} placeholder="http://your-server:port/v1" />
      </div>
      <div>
        <Label className="text-xs">API Key</Label>
        <Input className="h-8 text-sm" value={parsed.api_key || ""} onChange={(e) => update("api_key", e.target.value)} placeholder="sk-..." />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label className="text-xs">上下文窗口</Label>
          <Input className="h-8 text-sm" type="number" value={parsed.context_window || "32000"} onChange={(e) => update("context_window", e.target.value)} />
        </div>
        <div>
          <Label className="text-xs">最大输出 Token</Label>
          <Input className="h-8 text-sm" type="number" value={parsed.max_output_tokens || "8192"} onChange={(e) => update("max_output_tokens", e.target.value)} />
        </div>
      </div>

      {/* Test button */}
      <div className="flex items-center gap-2 pt-1">
        <Button variant="outline" size="sm" onClick={handleTest} disabled={testing}>
          {testing ? "测试中..." : "测试连接"}
        </Button>
        {testResult && (
          <span className={`text-xs ${testResult.success ? "text-green-600" : "text-destructive"}`}>
            {testResult.message}
          </span>
        )}
      </div>
    </div>
  );
}

/* ── Skill Config Form ── */
function SkillConfigForm({
  name, description, config, tags,
  onChangeDescription, onChangeConfig, onChangeTags,
}: {
  name: string;
  description: string;
  config: string;
  tags: string;
  onChangeDescription: (v: string) => void;
  onChangeConfig: (cfg: string) => void;
  onChangeTags: (v: string) => void;
}) {
  let parsed: { entrypoint?: string; source?: string; related_skills?: string[] } = {};
  try { parsed = JSON.parse(config || "{}"); } catch { /* ignore */ }

  const update = (key: string, value: string) => {
    const next = { ...parsed, [key]: value };
    onChangeConfig(JSON.stringify(next));
  };

  return (
    <>
      <div>
        <Label>描述</Label>
        <Textarea
          value={description}
          onChange={(e) => onChangeDescription(e.target.value)}
          placeholder="一句话说明这个技能的用途"
        />
      </div>
      <div>
        <Label>标签 (JSON 数组)</Label>
        <Input
          value={tags}
          onChange={(e) => onChangeTags(e.target.value)}
          placeholder='["agentgateway","planner"]'
        />
      </div>
      <div className="space-y-3 border border-border rounded-lg p-3">
        <p className="text-xs text-muted-foreground font-medium">技能元数据</p>
        <div>
          <Label className="text-xs">Entrypoint（SKILL.md 相对路径）</Label>
          <Input
            className="h-8 text-sm"
            value={parsed.entrypoint || (name ? `skills/${name}/SKILL.md` : "")}
            onChange={(e) => update("entrypoint", e.target.value)}
            placeholder="skills/<name>/SKILL.md"
          />
        </div>
        <div>
          <Label className="text-xs">来源</Label>
          <Select value={parsed.source || "project"} onValueChange={(v) => update("source", v ?? "project")}>
            <SelectTrigger className="h-8 text-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="project">project（项目内）</SelectItem>
              <SelectItem value="user">user（用户自定义）</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
    </>
  );
}
