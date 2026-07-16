"use client";
import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import type { ModelRegistryItem } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";

interface NodeConfig {
  [key: string]: string | number | boolean | undefined;
}

interface Props {
  agentId?: number;
  config: NodeConfig;
  onChange: (config: NodeConfig) => void;
  onSave: () => void;
}

export default function AgentNodeEditor({ agentId, config, onChange, onSave }: Props) {
  const [models, setModels] = useState<ModelRegistryItem[]>([]);
  const [modelsError, setModelsError] = useState(false);

  const roleName = (config.role_name as string) || "";
  const systemPrompt = (config.system_prompt as string) || "";
  const outputFormat = (config.output_format as string) || "markdown";
  const modelName = (config.model_name as string) || "qwen3.6-27b";
  const provider = (config.provider as string) || "glm";
  const temperature = (config.temperature as number) ?? 0.7;
  const maxTokens = (config.max_tokens as number) ?? 4096;

  useEffect(() => {
    const load = () => api.listModels()
      .then((m) => { setModels(m); setModelsError(false); })
      .catch(() => setModelsError(true));
    load();
    // Refetch on window focus so a capability-library add/delete in another
    // tab/route is reflected, not stuck on the mount-time snapshot. Debounced.
    let last = Date.now();
    const onFocus = () => {
      const now = Date.now();
      if (now - last < 3000) return;
      last = now;
      load();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-y-auto p-4 space-y-5">
        {/* Persona Section */}
        <div>
          <h4 className="text-sm font-semibold mb-3 flex items-center gap-2">
            <span className="w-5 h-5 rounded bg-blue-100 dark:bg-blue-900 flex items-center justify-center text-xs font-bold text-blue-600">P</span>
            Persona 人格
          </h4>
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label className="text-xs">角色名称</Label>
                <Input
                  value={roleName}
                  onChange={(e) => onChange({ ...config, role_name: e.target.value })}
                  placeholder="例如：价盘查询助手"
                  className="h-8 text-sm"
                />
              </div>
              <div className="space-y-1">
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
            <div className="space-y-1">
              <Label className="text-xs">系统提示词</Label>
              <Textarea
                value={systemPrompt}
                onChange={(e) => onChange({ ...config, system_prompt: e.target.value })}
                placeholder="描述这个智能体的角色、能力和约束..."
                className="font-mono text-sm min-h-[200px] resize-y"
              />
              <p className="text-xs text-muted-foreground">{systemPrompt.length} 字符</p>
            </div>
          </div>
        </div>

        <Separator />

        {/* Model Section */}
        <div>
          <h4 className="text-sm font-semibold mb-3 flex items-center gap-2">
            <span className="w-5 h-5 rounded bg-purple-100 dark:bg-purple-900 flex items-center justify-center text-xs font-bold text-purple-600">M</span>
            Model 模型
          </h4>
          <div className="space-y-3">
            {modelsError || models.length === 0 ? (
              <div className="rounded-md border border-border bg-muted/40 p-3 space-y-1.5">
                <div className="flex items-center gap-2">
                  <Badge variant="outline" className="text-xs">{provider}</Badge>
                  <span className="text-sm font-medium">{modelName}</span>
                </div>
                <p className="text-xs text-muted-foreground">
                  {modelsError
                    ? "模型列表加载失败，暂以当前模型只读显示。其它字段仍可编辑，恢复后可重新选择。"
                    : "暂无可用模型，先在模型库/注册表添加模型后再选择。"}
                </p>
              </div>
            ) : (
              <ScrollArea className="max-h-[200px]">
                <div className="grid gap-2">
                  {models.map((m) => (
                    <Card
                      key={m.id}
                      className={`cursor-pointer transition-colors ${
                        modelName === m.model_id ? "border-primary bg-primary/5" : "hover:border-primary/50"
                      }`}
                      onClick={() => onChange({ ...config, model_name: m.model_id, provider: m.provider })}
                    >
                      <CardContent className="p-2.5">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <Badge variant="outline" className="text-xs">{m.provider}</Badge>
                            <span className="text-sm font-medium">{m.display_name}</span>
                          </div>
                          {modelName === m.model_id && <Badge className="text-xs">已选</Badge>}
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </ScrollArea>
            )}
            <p className="text-xs text-muted-foreground">
              提供商（provider）由所选模型自动决定，无需手填。
            </p>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label className="text-xs">温度 ({temperature})</Label>
                <Input
                  type="number" min={0} max={2} step={0.1}
                  value={temperature}
                  onChange={(e) => onChange({ ...config, temperature: parseFloat(e.target.value) || 0.7 })}
                  className="h-8 text-sm"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">最大 Token</Label>
                <Input
                  type="number"
                  value={maxTokens}
                  onChange={(e) => onChange({ ...config, max_tokens: parseInt(e.target.value) || 4096 })}
                  className="h-8 text-sm"
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Save */}
      <div className="p-4 border-t border-border shrink-0">
        <Button className="w-full" onClick={onSave}>保存</Button>
      </div>
    </div>
  );
}
