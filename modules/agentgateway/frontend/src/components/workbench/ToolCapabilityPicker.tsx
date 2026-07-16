"use client";
import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import type { CapabilityItem } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Search } from "lucide-react";

interface NodeConfig {
  [key: string]: string | number | boolean | undefined;
}

interface Props {
  agentId?: number;
  config: NodeConfig;
  onChange: (config: NodeConfig) => void;
  onSave: () => void;
}

export default function ToolCapabilityPicker({ config, onChange, onSave }: Props) {
  const [tools, setTools] = useState<CapabilityItem[]>([]);
  const [searchQuery, setSearchQuery] = useState("");

  const selectedTool = (config.tool_name as string) || "";

  useEffect(() => {
    api.listCapabilities("tool").then(setTools).catch(() => {});
  }, []);

  const handleSearch = useCallback(() => {
    if (!searchQuery.trim()) {
      api.listCapabilities("tool").then(setTools).catch(() => {});
    } else {
      api.searchCapabilities(searchQuery).then((items) => {
        setTools(items.filter((i) => i.type === "tool"));
      }).catch(() => {});
    }
  }, [searchQuery]);

  const handleSelect = (item: CapabilityItem) => {
    try {
      const toolConfig = JSON.parse(item.config);
      onChange({
        ...config,
        tool_name: item.name,
        tool_params: JSON.stringify(toolConfig.parameters || {}),
      });
    } catch {
      onChange({ ...config, tool_name: item.name, tool_params: "{}" });
    }
  };

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        <Label className="text-xs text-muted-foreground">选择工具</Label>

        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-2 top-2 w-3.5 h-3.5 text-muted-foreground" />
            <Input
              placeholder="搜索工具..."
              className="pl-8 h-8 text-sm"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            />
          </div>
          <Button variant="outline" size="sm" className="h-8" onClick={handleSearch}>搜索</Button>
        </div>

        <ScrollArea className="max-h-[400px]">
          <div className="space-y-2">
            {tools.map((item) => (
              <Card
                key={item.id}
                className={`cursor-pointer transition-colors ${
                  selectedTool === item.name ? "border-primary bg-primary/5" : "hover:border-primary/50"
                }`}
                onClick={() => handleSelect(item)}
              >
                <CardContent className="p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{item.name}</span>
                    {selectedTool === item.name && (
                      <Badge className="text-xs">已选</Badge>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">{item.description}</p>
                </CardContent>
              </Card>
            ))}
            {tools.length === 0 && (
              <p className="text-xs text-muted-foreground text-center py-6">暂无可用工具</p>
            )}
          </div>
        </ScrollArea>

        {selectedTool && (
          <div className="p-3 border border-border rounded-lg bg-muted/30">
            <p className="text-xs text-muted-foreground mb-1">当前选择</p>
            <p className="text-sm font-medium">{selectedTool}</p>
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
