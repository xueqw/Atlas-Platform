import { getWsBase } from "./runtime-env";

type MessageHandler = (data: WsMessage) => void;
type StatusHandler = (status: "connected" | "disconnected" | "reconnecting") => void;

interface WsMessage {
  type: "token" | "done" | "error" | "pong" | "thinking" | "thinking_content" | "node_start" | "node_complete" | "node_error" | "node_status";
  content?: string;
  message?: string;
  [key: string]: unknown;
}

export function createChatSocket(agentId: number) {
  let ws: WebSocket | null = null;
  let reconnectAttempts = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let handlers: MessageHandler[] = [];
  let statusHandlers: StatusHandler[] = [];
  let disposed = false;

  function connect() {
    if (disposed) return;
    const url = `${getWsBase()}/api/agent/${agentId}`;
    ws = new WebSocket(url);

    ws.onopen = () => {
      reconnectAttempts = 0;
      notifyStatus("connected");
    };

    ws.onmessage = (event) => {
      const data: WsMessage = JSON.parse(event.data);
      handlers.forEach((h) => h(data));
    };

    ws.onclose = () => {
      if (disposed) return;
      notifyStatus("disconnected");
      scheduleReconnect();
    };

    ws.onerror = () => {
      ws?.close();
    };
  }

  function scheduleReconnect() {
    if (disposed) return;
    notifyStatus("reconnecting");
    const delay = Math.min(1000 * 2 ** reconnectAttempts, 30000);
    reconnectAttempts++;
    reconnectTimer = setTimeout(connect, delay);
  }

  function notifyStatus(status: "connected" | "disconnected" | "reconnecting") {
    statusHandlers.forEach((h) => h(status));
  }

  return {
    connect,
    send(content: string, extra?: Record<string, unknown>) {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "message", content, ...extra }));
      }
    },
    onMessage(handler: MessageHandler) {
      handlers.push(handler);
      return () => { handlers = handlers.filter((h) => h !== handler); };
    },
    onStatus(handler: StatusHandler) {
      statusHandlers.push(handler);
      return () => { statusHandlers = statusHandlers.filter((h) => h !== handler); };
    },
    disconnect() {
      disposed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
    },
  };
}