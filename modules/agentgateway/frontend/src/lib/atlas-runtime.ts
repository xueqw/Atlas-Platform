"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  dispatchAtlasRuntimeCommand,
  RESPONSE_EVENT,
  validAtlasRuntimeResponse,
  type BridgeResponse,
} from "@/components/shared/AtlasRuntimeBridge";
import { atlasRuntimeBindingFromUrl, type AtlasRuntimeBinding } from "@/lib/atlas-runtime-contract";
export { atlasRuntimeBindingFromUrl, type AtlasRuntimeBinding } from "@/lib/atlas-runtime-contract";

export type AtlasRuntimeEvent = Readonly<{
  schema_version: "atlas.runtime-event.v1";
  event_id: string;
  run_id: string;
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
}>;

export type PendingRuntimeInterrupt = Readonly<{
  interrupt_id: string;
  nonce: string;
  parameter_digest: string;
  resource_version: string;
  scope: string[];
}>;

export type AtlasRuntimeCallbacks = Readonly<{
  onEvent?: (event: AtlasRuntimeEvent) => void;
  onError?: (message: string) => void;
}>;

function readInterrupt(event: AtlasRuntimeEvent): PendingRuntimeInterrupt | null {
  if (event.type !== "interrupt.requested") return null;
  const { interrupt_id, nonce, parameter_digest, resource_version, scope } = event.payload;
  if (![interrupt_id, nonce, parameter_digest, resource_version].every((value) => typeof value === "string" && value.length > 0)) return null;
  return {
    interrupt_id: String(interrupt_id),
    nonce: String(nonce),
    parameter_digest: String(parameter_digest),
    resource_version: String(resource_version),
    scope: Array.isArray(scope) ? scope.filter((item): item is string => typeof item === "string") : [],
  };
}

export function useAtlasRuntimeExecution(gatewayAgentId: number, callbacks: AtlasRuntimeCallbacks = {}) {
  const callbacksRef = useRef(callbacks);
  callbacksRef.current = callbacks;
  const [bindingState, setBindingState] = useState<{ binding: AtlasRuntimeBinding | null; error: string | null }>({ binding: null, error: null });
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [pendingInterrupt, setPendingInterrupt] = useState<PendingRuntimeInterrupt | null>(null);
  const requestKindsRef = useRef(new Map<string, "start" | "subscribe" | "resume" | "cancel">());
  const runIdRef = useRef<string | null>(null);

  useEffect(() => {
    setBindingState(atlasRuntimeBindingFromUrl(gatewayAgentId, window.location.href, window.parent !== window));
  }, [gatewayAgentId]);

  useEffect(() => {
    const receive = (raw: Event) => {
      const response = (raw as CustomEvent<unknown>).detail;
      if (!validAtlasRuntimeResponse(response)) return;
      const message = response as BridgeResponse;
      const kind = requestKindsRef.current.get(message.request_id || "");
      const payload = message.payload || {};
      if (message.type === "runtime.error") {
        setRunning(false);
        callbacksRef.current.onError?.(String(payload.message || "Atlas Runtime 请求失败"));
        return;
      }
      if (message.type === "runtime.ack" && kind === "start") {
        const nextRunId = String(payload.run_id || "");
        if (!nextRunId) return;
        runIdRef.current = nextRunId;
        setRunId(nextRunId);
        const subscribeId = dispatchAtlasRuntimeCommand("runtime.subscribe", { run_id: nextRunId, after_sequence: 0 });
        requestKindsRef.current.set(subscribeId, "subscribe");
        return;
      }
      if (message.type === "runtime.ack" && (kind === "cancel" || kind === "resume")) {
        if (kind === "cancel") setRunning(false);
        if (kind === "resume") {
          setPendingInterrupt(null);
          setRunning(true);
        }
        return;
      }
      if (message.type !== "runtime.event") return;
      const event = payload.event as AtlasRuntimeEvent;
      if (!event || event.run_id !== runIdRef.current) return;
      const interrupt = readInterrupt(event);
      if (interrupt) {
        setPendingInterrupt(interrupt);
        setRunning(false);
      }
      if (["run.completed", "run.failed", "run.cancelled"].includes(event.type)) setRunning(false);
      callbacksRef.current.onEvent?.(event);
    };
    window.addEventListener(RESPONSE_EVENT, receive);
    return () => window.removeEventListener(RESPONSE_EVENT, receive);
  }, []);

  const start = useCallback((input: string, requestedResources: string[] = []) => {
    const binding = bindingState.binding;
    if (!binding) return false;
    setPendingInterrupt(null);
    setRunning(true);
    const requestId = dispatchAtlasRuntimeCommand("runtime.start", {
      agent_id: binding.agentId,
      input,
      idempotency_key: `agentgateway:${gatewayAgentId}:${crypto.randomUUID()}`,
      source: "builder",
      ...(binding.versionId ? { version_id: binding.versionId } : {}),
      ...(requestedResources.length ? { requested_resources: requestedResources } : {}),
    });
    requestKindsRef.current.set(requestId, "start");
    return true;
  }, [bindingState.binding, gatewayAgentId]);

  const cancel = useCallback(() => {
    if (!runIdRef.current) return;
    const requestId = dispatchAtlasRuntimeCommand("runtime.cancel", { run_id: runIdRef.current });
    requestKindsRef.current.set(requestId, "cancel");
  }, []);

  const resolveInterrupt = useCallback((decision: "approve" | "deny") => {
    if (!runIdRef.current || !pendingInterrupt) return;
    const requestId = dispatchAtlasRuntimeCommand("runtime.resume", {
      run_id: runIdRef.current,
      interrupt_id: pendingInterrupt.interrupt_id,
      nonce: pendingInterrupt.nonce,
      decision,
      parameter_digest: pendingInterrupt.parameter_digest,
      resource_version: pendingInterrupt.resource_version,
    });
    requestKindsRef.current.set(requestId, "resume");
  }, [pendingInterrupt]);

  return {
    embedded: bindingState.error !== null || bindingState.binding !== null,
    binding: bindingState.binding,
    mappingError: bindingState.error,
    runId,
    running,
    pendingInterrupt,
    start,
    cancel,
    resolveInterrupt,
  };
}
