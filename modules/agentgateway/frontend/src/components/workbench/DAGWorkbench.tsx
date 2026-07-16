"use client";
import { useState, useCallback, useEffect, useRef } from "react";
import { toast } from "sonner";
import DAGCanvas from "./DAGCanvas";
import NodePalette from "./NodePalette";
import NodePropertyPanel from "./NodePropertyPanel";
import NodeDebugDialog from "./NodeDebugDialog";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { Edge } from "@xyflow/react";

export interface DAGNodeData {
  id: string;
  type: string;
  position: { x: number; y: number };
  config: Record<string, string | number | boolean | undefined>;
}

interface NodeTypeInfo {
  node_type: string;
  display_name: string;
  category: string;
  config_schema: string;
  input_keys: string;
  output_keys: string;
}

interface Props {
  agentId: number;
  initialGraphJson?: string;
}

let nodeCounter = 0;

type DebugMode = "single" | "upstream";

export default function DAGWorkbench({ agentId, initialGraphJson }: Props) {
  const [graphJson, setGraphJson] = useState<string>(initialGraphJson || "{}");

  // Sync with prop when it changes (async load from parent)
  useEffect(() => {
    if (initialGraphJson && initialGraphJson !== "{}") {
      setGraphJson(initialGraphJson);
    }
  }, [initialGraphJson]);
  const [nodes, setNodes] = useState<DAGNodeData[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [nodeTypes, setNodeTypes] = useState<NodeTypeInfo[]>([]);
  const [selectedNode, setSelectedNode] = useState<{
    id: string; type: string; displayName: string; config: Record<string, string | number | boolean | undefined>;
  } | null>(null);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Version history
  const [versions, setVersions] = useState<number[]>([]);
  const [currentVersion, setCurrentVersion] = useState<number>(1);
  const [versionSelectorOpen, setVersionSelectorOpen] = useState(false);

  // Debug state
  const [debugNode, setDebugNode] = useState<{
    id: string; type: string; displayName: string;
    inputKeys: string[]; config: Record<string, string | number | boolean | undefined>;
  } | null>(null);

  // Breakpoints
  const [breakpoints, setBreakpoints] = useState<Set<string>>(new Set());
  const [executingNodes, setExecutingNodes] = useState<Set<string>>(new Set());
  const [completedNodes, setCompletedNodes] = useState<Set<string>>(new Set());
  const [errorNodes, setErrorNodes] = useState<Set<string>>(new Set());
  const [nodeTimings, setNodeTimings] = useState<Record<string, number>>({});

  // Right-click context menu
  const [contextMenu, setContextMenu] = useState<{ nodeId: string; x: number; y: number } | null>(null);

  // Load node types and versions
  useEffect(() => {
    api.getNodeTypes().then(setNodeTypes).catch(() => {});
    api.listDAGVersions(agentId).then((v) => { setVersions(v); if (v.length > 0) setCurrentVersion(v[0]); }).catch(() => {});
  }, [agentId]);

  // Parse initial graph JSON
  useEffect(() => {
    try {
      const graph = JSON.parse(graphJson);
      const ns: DAGNodeData[] = (graph.nodes || []).map((n: any) => ({
        id: n.id,
        type: n.type,
        position: n.position || { x: 0, y: 0 },
        config: n.config || {},
      }));
      const es: Edge[] = (graph.edges || []).map((e: any) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle: e.sourceHandle || null,
        targetHandle: e.targetHandle || null,
        type: "smoothstep",
        animated: true,
      }));
      setNodes(ns);
      setEdges(es);
      nodeCounter = ns.length;
    } catch {
      setNodes([]);
      setEdges([]);
    }
  }, [graphJson]);

  // Auto-save with 2s debounce
  const autoSave = useCallback((ns: DAGNodeData[], es: Edge[]) => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      const json = JSON.stringify({
        nodes: ns.map((n) => ({ id: n.id, type: n.type, position: n.position, config: n.config })),
        edges: es.map((e) => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle || null, targetHandle: e.targetHandle || null })),
      });
      setGraphJson(json);
      try {
        const result = await api.saveDAGGraph(agentId, json);
        setCurrentVersion(result.version);
        api.listDAGVersions(agentId).then(setVersions).catch(() => {});
      } catch {
        // Silently fail on auto-save
      }
    }, 2000);
  }, [agentId]);

  // Load specific version
  const handleLoadVersion = async (version: string | null) => {
    if (!version) return;
    try {
      // For now, reload the page with the version param
      toast.info(`切换到版本 ${version}`);
    } catch {
      toast.error("加载版本失败");
    }
  };

  const handleDropNewNode = useCallback((nodeType: string, position: { x: number; y: number }) => {
    const nt = nodeTypes.find((n) => n.node_type === nodeType);
    if (!nt) return;

    nodeCounter++;
    const id = `${nodeType}${nodeCounter}`;

    let config: Record<string, string | number | boolean> = {};
    try {
      const schema = JSON.parse(nt.config_schema);
      if (schema.properties) {
        for (const [key, prop] of Object.entries(schema.properties)) {
          const p = prop as any;
          if (p.default !== undefined) config[key] = p.default;
          else if (p.type === "string") config[key] = "";
          else if (p.type === "number" || p.type === "integer") config[key] = 0;
          else if (p.type === "boolean") config[key] = false;
        }
      }
    } catch {}

    if (nodeType === "agent") {
      // AI Agent: auto-create P and M nodes above it
      const agentNode: DAGNodeData = {
        id, type: "agent", position, config: {
          role_name: "AI助手", system_prompt: "你是一个有帮助的AI助手。", output_format: "markdown",
          model_name: "qwen3.6-27b", provider: "glm", temperature: 0.7, max_tokens: 4096, streaming: true,
        }
      };
      nodeCounter++;
      const pId = `p${nodeCounter}`;
      const pNode: DAGNodeData = {
        id: pId, type: "p", position: { x: position.x - 50, y: position.y - 160 }, config: {
          role_name: "AI助手", role_description: "", system_prompt: "你是一个有帮助的AI助手。", output_format: "markdown",
        }
      };
      nodeCounter++;
      const mId = `m${nodeCounter}`;
      const mNode: DAGNodeData = {
        id: mId, type: "m", position: { x: position.x + 150, y: position.y - 160 }, config: {
          model_name: "qwen3.6-27b", provider: "glm", temperature: 0.7, max_tokens: 4096, streaming: true,
        }
      };
      const newNodes = [...nodes, agentNode, pNode, mNode];
      const newEdges: Edge[] = [
        ...edges,
        { id: `e_${pId}_${id}`, source: pId, target: id, sourceHandle: null, targetHandle: "prompt", type: "smoothstep", animated: true },
        { id: `e_${mId}_${id}`, source: mId, target: id, sourceHandle: null, targetHandle: "model", type: "smoothstep", animated: true },
      ];
      setNodes(newNodes);
      setEdges(newEdges);
      autoSave(newNodes, newEdges);
    } else {
      const newNode: DAGNodeData = { id, type: nodeType, position, config };
      const newNodes = [...nodes, newNode];
      setNodes(newNodes);
      autoSave(newNodes, edges);
    }
  }, [nodes, edges, nodeTypes, autoSave]);

  const handleNodeDoubleClick = useCallback((nodeId: string) => {
    const nodeData = nodes.find((n) => n.id === nodeId);
    if (!nodeData) return;
    const nt = nodeTypes.find((n) => n.node_type === nodeData.type);
    setSelectedNode({
      id: nodeId,
      type: nodeData.type,
      displayName: nt?.display_name || nodeData.type,
      config: { ...nodeData.config },
    });
  }, [nodes, nodeTypes]);

  const handleNodeContextMenu = useCallback((nodeId: string, event: React.MouseEvent) => {
    event.preventDefault();
    setContextMenu({ nodeId, x: event.clientX, y: event.clientY });
  }, []);

  const handleDebugNode = (mode: DebugMode) => {
    if (!contextMenu) return;
    const nodeData = nodes.find((n) => n.id === contextMenu.nodeId);
    if (!nodeData) return;
    const nt = nodeTypes.find((n) => n.node_type === nodeData.type);
    let inputKeys: string[] = [];
    try { inputKeys = JSON.parse(nt?.input_keys || "[]"); } catch {}
    setDebugNode({
      id: nodeData.id,
      type: nodeData.type,
      displayName: nt?.display_name || nodeData.type,
      inputKeys,
      config: { ...nodeData.config },
    });
    setContextMenu(null);
  };

  const handleToggleBreakpoint = () => {
    if (!contextMenu) return;
    setBreakpoints((prev) => {
      const next = new Set(prev);
      if (next.has(contextMenu.nodeId)) next.delete(contextMenu.nodeId);
      else next.add(contextMenu.nodeId);
      return next;
    });
    setContextMenu(null);
  };

  const handlePropertySave = useCallback((nodeId: string, config: Record<string, string | number | boolean | undefined>) => {
    const newNodes = nodes.map((n) =>
      n.id === nodeId ? { ...n, config } : n
    );
    setNodes(newNodes);
    autoSave(newNodes, edges);
    setSelectedNode(null);
  }, [nodes, edges, autoSave]);

  const handleNodesDelete = useCallback((deletedNodeIds: string[]) => {
    const idSet = new Set(deletedNodeIds);
    const newNodes = nodes.filter((n) => !idSet.has(n.id));
    const newEdges = edges.filter((e) => !idSet.has(e.source) && !idSet.has(e.target));
    setNodes(newNodes);
    setEdges(newEdges);
    autoSave(newNodes, newEdges);
  }, [nodes, edges, autoSave]);

  // Close context menu on any click
  useEffect(() => {
    const handler = () => setContextMenu(null);
    window.addEventListener("click", handler);
    return () => window.removeEventListener("click", handler);
  }, []);

  return (
    <div className="flex absolute inset-0">
      {/* Left: Node Palette */}
      <div className="w-48 border-r border-border bg-card overflow-y-auto shrink-0">
        <NodePalette />
      </div>

      {/* Center: Canvas with toolbar */}
      <div className="flex-1 flex flex-col h-full">
        {/* Toolbar */}
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border bg-card shrink-0">
          <span className="text-xs text-muted-foreground">版本:</span>
          <span className="text-xs font-mono font-medium">{currentVersion}</span>
          {versions.length > 1 && (
            <Select onValueChange={handleLoadVersion}>
              <SelectTrigger className="w-32 h-7 text-xs">
                <SelectValue placeholder="切换版本" />
              </SelectTrigger>
              <SelectContent>
                {versions.map((v) => (
                  <SelectItem key={v} value={String(v)}>
                    v{v} {v === currentVersion ? "(当前)" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <div className="flex-1" />
          {breakpoints.size > 0 && (
            <span className="text-xs text-amber-600">
              {breakpoints.size} 个断点
            </span>
          )}
        </div>

        {/* Canvas */}
        <div className="flex-1 min-h-0 relative">
          <DAGCanvas
            key={nodes.length > 0 ? "loaded" : "empty"}
            initialNodes={nodes}
            initialEdges={edges}
            agentId={agentId}
            onNodeDoubleClick={handleNodeDoubleClick}
            onNodeContextMenu={handleNodeContextMenu}
            onDropNewNode={handleDropNewNode}
            onNodesDelete={handleNodesDelete}
          />
        </div>

        {/* Timing overlay at bottom */}
        {Object.keys(nodeTimings).length > 0 && (
          <div className="flex items-center gap-2 px-3 py-1 border-t border-border bg-card text-xs text-muted-foreground shrink-0">
            <span>耗时:</span>
            {Object.entries(nodeTimings).map(([id, ms]) => (
              <span key={id} className="font-mono">{id}: {ms}ms</span>
            ))}
          </div>
        )}
      </div>

      {/* Right: Property Panel */}
      {selectedNode && (
        <NodePropertyPanel
          nodeId={selectedNode.id}
          nodeType={selectedNode.type}
          displayName={selectedNode.displayName}
          config={selectedNode.config}
          agentId={agentId}
          onSave={handlePropertySave}
          onClose={() => setSelectedNode(null)}
          validationErrors={validationErrors.length > 0 ? validationErrors : undefined}
        />
      )}

      {/* Node Debug Dialog */}
      {debugNode && (
        <NodeDebugDialog
          nodeId={debugNode.id}
          nodeType={debugNode.type}
          displayName={debugNode.displayName}
          config={debugNode.config}
          inputKeys={debugNode.inputKeys}
          agentId={agentId}
          onClose={() => setDebugNode(null)}
        />
      )}

      {/* Right-click Context Menu */}
      {contextMenu && (
        <div
          className="fixed z-50 bg-card border border-border rounded-md shadow-lg py-1 min-w-[160px]"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            className="w-full text-left px-3 py-1.5 text-sm hover:bg-accent"
            onClick={() => handleDebugNode("single")}
          >
            调试此节点
          </button>
          <button
            className="w-full text-left px-3 py-1.5 text-sm hover:bg-accent"
            onClick={() => handleDebugNode("upstream")}
          >
            使用上游数据调试
          </button>
          <div className="border-t border-border my-1" />
          <button
            className="w-full text-left px-3 py-1.5 text-sm hover:bg-accent"
            onClick={handleToggleBreakpoint}
          >
            {contextMenu && breakpoints.has(contextMenu.nodeId) ? "✓ " : ""}切换断点
          </button>
        </div>
      )}
    </div>
  );
}
