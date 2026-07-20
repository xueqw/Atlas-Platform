"use client";
import { useRef, useState } from "react";
import { Play, Clock, Square, ShieldCheck, ShieldX } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { getApiBase } from "@/lib/runtime-env";
import { useAtlasRuntimeExecution } from "@/lib/atlas-runtime";

interface Props {
  nodeId: string;
  nodeType: string;
  displayName: string;
  config: Record<string, string | number | boolean | undefined>;
  inputKeys: string[];
  agentId: number;
  onClose: () => void;
}

export default function NodeDebugDialog({
  nodeId, nodeType, displayName, config, inputKeys, agentId, onClose,
}: Props) {
  const [inputs, setInputs] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const k of inputKeys) init[k] = "";
    return init;
  });
  const [output, setOutput] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);
  const [tokens, setTokens] = useState<number>(0);
  const startedAtRef = useRef(0);
  const streamedOutputRef = useRef("");
  const runtime = useAtlasRuntimeExecution(agentId, {
    onEvent: (event) => {
      if ((event.type === "token.delta" || event.type === "response.token") && typeof event.payload.token === "string") {
        streamedOutputRef.current += event.payload.token;
        setOutput(streamedOutputRef.current);
      }
      if (event.type === "interrupt.requested") setRunning(false);
      if (event.type === "run.completed") {
        const finalOutput = event.payload.output;
        if (typeof finalOutput === "string" && finalOutput) setOutput(finalOutput);
        setDuration(Math.round(performance.now() - startedAtRef.current));
        setRunning(false);
      } else if (event.type === "run.failed") {
        setError(String(event.payload.message || event.payload.error || "Atlas Runtime 执行失败"));
        setDuration(Math.round(performance.now() - startedAtRef.current));
        setRunning(false);
      } else if (event.type === "run.cancelled") {
        setError("运行已取消");
        setDuration(Math.round(performance.now() - startedAtRef.current));
        setRunning(false);
      }
    },
    onError: (message) => {
      setError(message);
      setRunning(false);
    },
  });

  const handleRun = async () => {
    setRunning(true);
    setOutput("");
    setError("");
    setDuration(null);
    const t0 = performance.now();
    startedAtRef.current = t0;
    streamedOutputRef.current = "";

    if (runtime.embedded) {
      if (runtime.mappingError) {
        setError(runtime.mappingError);
        setRunning(false);
        return;
      }
      const started = runtime.start(JSON.stringify({ node_id: nodeId, node_type: nodeType, config, inputs }), [
        `agentgateway:agent:${agentId}`,
        `agentgateway:dag-node:${nodeId}`,
      ]);
      if (!started) {
        setError("Atlas Runtime Bridge 尚未就绪");
        setRunning(false);
      }
      return;
    }

    try {
      // Send debug request via WebSocket or REST
      const res = await fetch(`${getApiBase()}/api/agents/${agentId}/debug-node`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          node_id: nodeId,
          node_type: nodeType,
          config,
          inputs,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Debug failed" }));
        setError(err.detail || "Debug failed");
      } else {
        const data = await res.json();
        setOutput(JSON.stringify(data.output, null, 2));
        setTokens(data.tokens || 0);
      }
    } catch (e: any) {
      setError(e.message || "Network error");
    }

    setDuration(Math.round(performance.now() - t0));
    setRunning(false);
  };

  return (
    <Dialog open onOpenChange={() => onClose()}>
      <DialogContent className="max-w-lg max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            调试节点: {displayName} <span className="text-xs text-muted-foreground">({nodeId})</span>
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4">
          {/* Input fields */}
          {inputKeys.length > 0 && (
            <div className="space-y-3">
              <Label className="text-sm font-semibold">输入参数</Label>
              {inputKeys.map((key) => (
                <div key={key} className="space-y-1">
                  <Label className="text-xs">{key}</Label>
                  <Input
                    value={inputs[key] || ""}
                    onChange={(e) => setInputs((p) => ({ ...p, [key]: e.target.value }))}
                    placeholder={`输入 ${key}`}
                  />
                </div>
              ))}
            </div>
          )}

          {/* Run button */}
          <Button onClick={handleRun} disabled={running || Boolean(runtime.pendingInterrupt)} className="w-full">
            <Play className="w-4 h-4 mr-2" />
            {running ? "执行中..." : "运行"}
          </Button>

          {runtime.embedded && runtime.running && (
            <Button type="button" variant="outline" onClick={runtime.cancel} className="w-full">
              <Square className="w-4 h-4 mr-2" />取消运行
            </Button>
          )}

          {runtime.pendingInterrupt && (
            <div className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
              <p className="text-sm font-medium">工具写操作等待用户授权</p>
              <p className="text-xs text-muted-foreground">
                授权范围：{runtime.pendingInterrupt.scope.join("、") || "未声明"}
              </p>
              <div className="flex gap-2">
                <Button type="button" size="sm" onClick={() => runtime.resolveInterrupt("approve")}>
                  <ShieldCheck className="w-4 h-4 mr-1" />授权并继续
                </Button>
                <Button type="button" size="sm" variant="destructive" onClick={() => runtime.resolveInterrupt("deny")}>
                  <ShieldX className="w-4 h-4 mr-1" />拒绝
                </Button>
              </div>
            </div>
          )}

          {/* Duration & tokens */}
          {duration !== null && (
            <div className="flex items-center gap-4 text-xs text-muted-foreground">
              <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> {duration}ms</span>
              <span>Tokens: {tokens}</span>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="p-3 rounded-md bg-destructive/10 border border-destructive/20">
              <p className="text-sm text-destructive whitespace-pre-wrap">{error}</p>
            </div>
          )}

          {/* Output */}
          {output && (
            <div className="space-y-1">
              <Label className="text-sm font-semibold">输出</Label>
              <pre className="text-xs bg-muted p-3 rounded-md overflow-x-auto whitespace-pre-wrap">{output}</pre>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
