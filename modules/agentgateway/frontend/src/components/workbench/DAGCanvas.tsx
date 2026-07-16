"use client";
import { useCallback, useRef, useEffect } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  type Connection,
  type Edge,
  type Node,
  BackgroundVariant,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { nodeTypes, buildDAGNode } from "./CustomNodes";

export interface DAGNodeData {
  id: string;
  type: string;
  position: { x: number; y: number };
  config: Record<string, string | number | boolean | undefined>;
}

export type { Node, Edge };

interface Props {
  initialNodes: DAGNodeData[];
  initialEdges: Edge[];
  agentId?: number;
  onNodeDoubleClick?: (nodeId: string) => void;
  onNodeContextMenu?: (nodeId: string, event: React.MouseEvent) => void;
  onDropNewNode?: (nodeType: string, position: { x: number; y: number }) => void;
  onNodesDelete?: (deletedNodeIds: string[]) => void;
  readOnly?: boolean;
}

function DAGCanvasInner({
  initialNodes,
  initialEdges,
  onNodeDoubleClick,
  onNodeContextMenu,
  onDropNewNode,
  onNodesDelete,
  readOnly = false,
}: Props) {
  const reactFlowWrapper = useRef<HTMLDivElement>(null);

  const [nodes, setNodes, onNodesChangeRF] = useNodesState(
    initialNodes.map((n) => buildDAGNode(n.id, n.type, n.position, n.config))
  );
  const [edges, setEdges, onEdgesChangeRF] = useEdgesState(initialEdges);

  const handleNodesDeleteRF = useCallback(
    (deleted: Node[]) => {
      onNodesDelete?.(deleted.map((n) => n.id));
    },
    [onNodesDelete]
  );

  // Sync when initialNodes/initialEdges change from parent. Key includes
  // identity-bearing config fields so editor saves (e.g. role_name change)
  // re-trigger the resync and refresh on-canvas labels.
  const nodesKey = initialNodes
    .map((n) => {
      const c = n.config || {};
      const identity =
        c.role_name ?? c.model_name ?? c.knowledge_name ?? c.tool_name ?? "";
      return `${n.id}:${n.type}:${identity}`;
    })
    .join("|");
  const edgesKey = initialEdges.map((e) => e.id).join(",");

  useEffect(() => {
    setNodes(initialNodes.map((n) => buildDAGNode(n.id, n.type, n.position, n.config)));
  }, [nodesKey, setNodes]);

  useEffect(() => {
    setEdges(initialEdges);
  }, [edgesKey, setEdges]);

  const onConnect = useCallback(
    (connection: Connection) => {
      if (readOnly) return;
      setEdges((eds) =>
        addEdge(
          {
            ...connection,
            type: "smoothstep",
            animated: true,
            markerEnd: { type: MarkerType.ArrowClosed },
          },
          eds
        )
      );
    },
    [readOnly, setEdges]
  );

  const onNodeDoubleClickHandler = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      onNodeDoubleClick?.(node.id);
    },
    [onNodeDoubleClick]
  );

  const onNodeContextMenuHandler = useCallback(
    (event: React.MouseEvent, node: Node) => {
      onNodeContextMenu?.(node.id, event);
    },
    [onNodeContextMenu]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      if (readOnly) return;

      const nodeType = event.dataTransfer.getData("application/node-type");
      if (!nodeType || !reactFlowWrapper.current) return;

      const bounds = reactFlowWrapper.current.getBoundingClientRect();
      const position = {
        x: event.clientX - bounds.left - 60,
        y: event.clientY - bounds.top - 20,
      };

      onDropNewNode?.(nodeType, position);
    },
    [readOnly, onDropNewNode]
  );

  return (
    <div ref={reactFlowWrapper} className="absolute inset-0" onDragOver={onDragOver} onDrop={onDrop}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={readOnly ? undefined : onNodesChangeRF}
        onEdgesChange={readOnly ? undefined : onEdgesChangeRF}
        onConnect={onConnect}
        onNodesDelete={readOnly ? undefined : handleNodesDeleteRF}
        onNodeDoubleClick={onNodeDoubleClickHandler}
        onNodeContextMenu={onNodeContextMenuHandler}
        nodeTypes={nodeTypes}
        fitView
        deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
        multiSelectionKeyCode="Shift"
        snapToGrid
        snapGrid={[20, 20]}
        className="bg-background"
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
        <Controls />
        <MiniMap
          nodeColor={(node) => {
            const type = (node.data?.type as string) || "?";
            const colors: Record<string, string> = {
              i: "#10b981", p: "#3b82f6", m: "#a855f7", o: "#f43f5e",
              k: "#f59e0b", t: "#06b6d4", c: "#f97316", x: "#22c55e",
              h: "#6366f1", a: "#ef4444",
            };
            return colors[type] || "#9ca3af";
          }}
        />
      </ReactFlow>
    </div>
  );
}

export default function DAGCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <DAGCanvasInner {...props} />
    </ReactFlowProvider>
  );
}
