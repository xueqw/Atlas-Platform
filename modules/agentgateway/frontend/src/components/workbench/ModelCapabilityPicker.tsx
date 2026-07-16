"use client";
import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import type { ModelRegistryItem } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ChevronDown, ChevronUp } from "lucide-react";

interface NodeConfig {
  [key: string]: string | number | boolean | undefined;
}

interface Props {
  config: NodeConfig;
  onChange: (config: NodeConfig) => void;
  onSave: () => void;
}

export default function ModelCapabilityPicker({ config, onChange, onSave }: Props) {
  const [models, setModels] = useState<ModelRegistryItem[]>([]);
  const [paramsOpen, setParamsOpen] = useState(false);

  const selectedModel = (config.model_name as string) || "";
  const temperature = (config.temperature as number) ?? 0.7;
  const maxTokens = (config.max_tokens as number) ?? 4096;
  const streaming = (config.streaming as boolean) ?? true;

  useEffect(() => {
    api.listModels().then(setModels).catch(() => {});
  }, []);

  const handleSelect = (model: ModelRegistryItem) => {
    onChange({
      ...config,
      model_name: model.model_id,
      provider: model.provider,
      max_tokens: Math.min(maxTokens, model.max_output_tokens || 4096),
    });
  };

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        <Label className="text-xs text-muted-foreground">选择模型</Label>

        <ScrollArea className="max-h-[360px]">
          <div className="grid gap-2">
            {models.map((m) => (
              <Card
                key={m.id}
                className={`cursor-pointer transition-colors ${
                  selectedModel === m.model_id ? "border-primary bg-primary/5" : "hover:border-primary/50"
                }`}
                onClick={() => handleSelect(m)}
              >
                <CardContent className="p-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="text-xs">{m.provider}</Badge>
                      <span className="text-sm font-medium">{m.display_name}</span>
                    </div>
                    {selectedModel === m.model_id && (
                      <Badge className="text-xs">已选</Badge>
                    )}
                  </div>
                  <div className="flex items-center gap-3 mt-1.5 text-xs text-muted-foreground">
                    <span>上下文: {(m.context_window / 1000).toFixed(0)}K</span>
                    <span>输入: ${m.input_price_per_1k}/1K</span>
                    <span>输出: ${m.output_price_per_1k}/1K</span>
                  </div>
                </CardContent>
              </Card>
            ))}
            {models.length === 0 && (
              <p className="text-xs text-muted-foreground text-center py-6">加载中...</p>
            )}
          </div>
        </ScrollArea>

        {/* Parameter overrides */}
        <button
          onClick={() => setParamsOpen(!paramsOpen)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          {paramsOpen ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          参数微调
        </button>

        {paramsOpen && (
          <div className="grid grid-cols-2 gap-3 p-3 border border-border rounded-lg">
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
            <div className="space-y-1 col-span-2">
              <Label className="text-xs">流式输出</Label>
              <Select value={streaming ? "true" : "false"} onValueChange={(v) => onChange({ ...config, streaming: v === "true" })}>
                <SelectTrigger className="h-8 text-sm"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="true">开启</SelectItem>
                  <SelectItem value="false">关闭</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        )}
      </div>

      {/* Save */}
      <div className="p-4 border-t border-border shrink-0">
        <Button className="w-full" onClick={onSave}>保存</Button>
      </div>
    </div>
  );
}
