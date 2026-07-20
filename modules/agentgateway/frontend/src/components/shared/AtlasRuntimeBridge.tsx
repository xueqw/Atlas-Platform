"use client";

import { useEffect } from "react";

export const BRIDGE_SCHEMA = "atlas.application-runtime-bridge.v1";
export const EVENT_SCHEMA = "atlas.runtime-event.v1";
export const COMMAND_EVENT = "atlas:runtime-command";
export const RESPONSE_EVENT = "atlas:runtime-response";
const COMMAND_TYPES = new Set(["runtime.start", "runtime.resume", "runtime.cancel", "runtime.subscribe"]);

type BridgeCommand = {
  schema_version: typeof BRIDGE_SCHEMA;
  type: "runtime.start" | "runtime.resume" | "runtime.cancel" | "runtime.subscribe";
  request_id: string;
  payload: Record<string, unknown>;
};

export type BridgeResponse = {
  schema_version?: string;
  type?: string;
  request_id?: string;
  payload?: Record<string, unknown>;
};

type RuntimeCommandEvent = CustomEvent<Omit<BridgeCommand, "schema_version">>;

function allowedHostOrigins(): Set<string> {
  const configured = process.env.NEXT_PUBLIC_ATLAS_HOST_ORIGIN || "http://localhost:5173";
  return new Set(configured.split(",").map((item) => item.trim()).filter(Boolean));
}

export function validAtlasRuntimeResponse(value: unknown): value is BridgeResponse {
  if (!value || typeof value !== "object") return false;
  const response = value as BridgeResponse;
  if (response.schema_version !== BRIDGE_SCHEMA || typeof response.request_id !== "string") return false;
  if (!["runtime.ready", "runtime.ack", "runtime.event", "runtime.error"].includes(response.type || "")) return false;
  if (!response.payload || typeof response.payload !== "object" || Array.isArray(response.payload)) return false;
  if (response.type === "runtime.event") {
    const event = response.payload.event as Record<string, unknown> | undefined;
    return event?.schema_version === EVENT_SCHEMA && Number.isInteger(event.sequence);
  }
  return true;
}

function parentOrigin(origins: Set<string>): string | null {
  try {
    const referrerOrigin = document.referrer ? new URL(document.referrer).origin : "";
    if (origins.has(referrerOrigin)) return referrerOrigin;
  } catch {
    // Invalid referrers cannot widen the configured allow-list.
  }
  return origins.values().next().value || null;
}

export function dispatchAtlasRuntimeCommand(
  type: BridgeCommand["type"],
  payload: Record<string, unknown>,
  requestId = crypto.randomUUID(),
): string {
  window.dispatchEvent(new CustomEvent(COMMAND_EVENT, { detail: { type, payload, request_id: requestId } }));
  return requestId;
}

export function AtlasRuntimeBridge() {
  useEffect(() => {
    const origins = allowedHostOrigins();
    const targetOrigin = parentOrigin(origins);
    if (!targetOrigin || window.parent === window) return;

    const send = (command: Omit<BridgeCommand, "schema_version">) => {
      if (!COMMAND_TYPES.has(command.type) || !command.request_id || !command.payload) return;
      window.parent.postMessage({ schema_version: BRIDGE_SCHEMA, ...command }, targetOrigin);
    };
    const receive = (event: MessageEvent<unknown>) => {
      if (event.source !== window.parent || !origins.has(event.origin) || !validAtlasRuntimeResponse(event.data)) return;
      window.dispatchEvent(new CustomEvent(RESPONSE_EVENT, { detail: event.data }));
    };
    const forward = (event: Event) => send((event as RuntimeCommandEvent).detail);

    window.addEventListener("message", receive);
    window.addEventListener(COMMAND_EVENT, forward);
    send({
      type: "runtime.subscribe",
      request_id: crypto.randomUUID(),
      payload: { handshake: true, event_schema: EVENT_SCHEMA },
    });
    return () => {
      window.removeEventListener("message", receive);
      window.removeEventListener(COMMAND_EVENT, forward);
    };
  }, []);
  return null;
}
