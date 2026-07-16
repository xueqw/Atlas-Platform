"use client";
import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import type { CapabilityItem, PromptVersion, OptimizeResult } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Search, Sparkles, ChevronDown, ChevronUp, History } from "lucide-react";
import { toast } from "sonner";

interface NodeConfig {
  [key: string]: string | number | boolean | undefined;
}

interface Props {
  agentId?: number;
  config: NodeConfig;
  onChange: (config: NodeConfig) => void;
  onSave: () => void;
  highlightKeys?: string[];
}

export default function PromptCapabilityEditor({ agentId, config, onChange, onSave, highlightKeys }: Props) {
  const [capabilities, setCapabilities] = useState<CapabilityItem[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [versions, setVersions] = useState<PromptVersion[]>([]);
  const [optimizing, setOptimizing] = useState(false);

  const systemPrompt = (config.system_prompt as string) || "";
  const roleName = (config.role_name as string) || "";
  const outputFormat = (config.output_format as string) || "markdown";

  useEffect(() => {
    api.listCapabilities("prompt").then(setCapabilities).catch(() => {});
    if (agentId) {
      api.listPromptVersions(agentId).then(setVersions).catch(() => {});
    }
  }, [agentId]);

  const handleSearch = useCallback(() => {
    if (!searchQuery.trim()) {
      api.listCapabilities("prompt").then(setCapabilities).catch(() => {});
    } else {
      api.searchCapabilities(searchQuery).then((items) => {
        setCapabilities(items.filter((i) => i.type === "prompt"));
      }).catch(() => {});
    }
  }, [searchQuery]);

  const handleImport = (item: CapabilityItem) => {
    try {
      const cfg = JSON.parse(item.config);
      onChange({
        ...config,
        system_prompt: cfg.content || cfg.system_prompt || "",
        role_name: cfg.role_name || item.name || roleName,
      });
      setPickerOpen(false);
      toast.success(`已导入模板: ${item.name}`);
    } catch {
      toast.error("导入失败");
    }
  };

  const handleOptimize = async () => {
    if (!agentId) return;
    setOptimizing(true);
    try {
      const result = await api.optimizePrompt(agentId, {
        role_name: roleName,
        system_prompt: systemPrompt,
      }, []);
      if (result.optimized_prompt?.system_prompt) {
        onChange({ ...config, system_prompt: result.optimized_prompt.system_prompt });
        toast.success("优化完成，已更新提示词");
      }
    } catch {
      toast.error("优化失败");
    }
    setOptimizing(false);
  };

  const handleLoadVersion = async (versionId: string | null) => {
    if (!versionId || !agentId) return;
    try {
      const v = await api.getPromptVersion(agentId, Number(versionId));
      const cfg = JSON.parse(v.prompt_config);
      onChange({
        ...config,
        role_name: cfg.role_name || "",
        system_prompt: cfg.system_prompt || "",
        output_format: cfg.output_format || "markdown",
      });
      toast.success(`已加载版本 ${v.version_number}`);
    } catch {
      toast.error("加载版本失败");
    }
  };

  const promptLength = systemPrompt.length;
  const lengthOk = promptLength >= 10;

  const ringFor = (key: string) =>
    highlightKeys?.includes(key) ? "ring-2 ring-primary/40 rounded-md transition-shadow" : "transition-shadow";

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Capability picker toggle */}
        <button
          onClick={() => setPickerOpen(!pickerOpen)}
          className="flex items-center gap-2 text-sm text-primary hover:underline"
        >
          {pickerOpen ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          从能力库导入模板
        </button>

        {/* Capability picker */}
        {pickerOpen && (
          <div className="border border-border rounded-lg p-3 space-y-2 bg-muted/30">
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <Search className="absolute left-2 top-2 w-3.5 h-3.5 text-muted-foreground" />
                <Input
                  placeholder="搜索提示词模板..."
                  className="pl-8 h-8 text-sm"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                />
              </div>
              <Button variant="outline" size="sm" className="h-8" onClick={handleSearch}>搜索</Button>
            </div>
            <ScrollArea className="max-h-40">
              <div className="space-y-1.5">
                {capabilities.map((item) => (
                  <Card
                    key={item.id}
                    className="cursor-pointer hover:border-primary transition-colors"
                    onClick={() => handleImport(item)}
                  >
                    <CardContent className="p-2">
                      <p className="text-sm font-medium truncate">{item.name}</p>
                      <p className="text-xs text-muted-foreground truncate">{item.description}</p>
                    </CardContent>
                  </Card>
                ))}
                {capabilities.length === 0 && (
                  <p className="text-xs text-muted-foreground text-center py-3">暂无模板</p>
                )}
              </div>
            </ScrollArea>
          </div>
        )}

        {/* Meta fields row */}
        <div className="grid grid-cols-2 gap-3">
          <div className={`space-y-1 ${ringFor("role_name")}`}>
            <Label className="text-xs">角色名称</Label>
            <Input
              value={roleName}
              onChange={(e) => onChange({ ...config, role_name: e.target.value })}
              placeholder="例如：翻译助手"
              className="h-8 text-sm"
            />
          </div>
          <div className={`space-y-1 ${ringFor("output_format")}`}>
            <Label className="text-xs">输出格式</Label>
            <Select value={outputFormat} onValueChange={(v) => onChange({ ...config, output_format: v ?? "markdown" })}>
              <SelectTrigger className="h-8 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="markdown">Markdown</SelectItem>
                <SelectItem value="json">JSON</SelectItem>
                <SelectItem value="text">纯文本</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Main prompt editor */}
        <div className={`space-y-1 flex-1 ${ringFor("system_prompt")}`}>
          <Label className="text-xs">系统提示词</Label>
          <Textarea
            value={systemPrompt}
            onChange={(e) => onChange({ ...config, system_prompt: e.target.value })}
            placeholder="编写系统提示词..."
            className="font-mono text-sm min-h-[300px] resize-y"
          />
        </div>

        {/* Toolbar */}
        <div className="flex items-center gap-2 flex-wrap">
          <Badge variant={lengthOk ? "default" : "secondary"} className="text-xs">
            {promptLength} 字符 {lengthOk ? "✓" : "(建议≥10)"}
          </Badge>
          <Button
            variant="outline" size="sm"
            onClick={handleOptimize}
            disabled={optimizing || !systemPrompt.trim()}
          >
            <Sparkles className="w-3 h-3 mr-1" />
            {optimizing ? "优化中..." : "AI 优化"}
          </Button>
          {versions.length > 0 && (
            <Select onValueChange={handleLoadVersion}>
              <SelectTrigger className="w-32 h-7 text-xs">
                <History className="w-3 h-3 mr-1" />
                <SelectValue placeholder="版本历史" />
              </SelectTrigger>
              <SelectContent>
                {versions.map((v) => (
                  <SelectItem key={v.id} value={String(v.id)}>
                    v{v.version_number}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>
      </div>

      {/* Save */}
      <div className="p-4 border-t border-border shrink-0">
        <Button className="w-full" onClick={onSave}>保存</Button>
      </div>
    </div>
  );
}
