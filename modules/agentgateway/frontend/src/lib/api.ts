import type { Agent, PromptTemplate, ModelRegistryItem, Conversation, Message, PipelineStatus, KBDocument, RetrieveResult, CapabilityItem, PromptVersion, OptimizeResult, ToolConfig, AttachmentMeta, PlannerSkillItem, EvaluationSuiteItem, EvaluationCaseItem, EvaluationRunItem, EvaluationRunDetail, EvaluationCompareResult, ReleaseGateResult } from "./types";
import { getApiBase } from "./runtime-env";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${getApiBase()}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail?.errors || body.detail || res.statusText);
  }
  return res.json();
}

export const api = {
  // Agents
  listAgents: () => request<Agent[]>("/api/agents"),
  getAgent: (id: number) => request<Agent>(`/api/agents/${id}`),
  createAgent: (name: string, description?: string) =>
    request<Agent>("/api/agents", {
      method: "POST",
      body: JSON.stringify({ name, description: description || "" }),
    }),
  updateAgent: (id: number, data: Partial<Agent> & {
    prompt_config?: Agent["prompt_config"];
    model?: Agent["model"];
  }) =>
    request<Agent>(`/api/agents/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  deleteAgent: (id: number) =>
    request<{ message: string }>(`/api/agents/${id}`, { method: "DELETE" }),
  publishAgent: (id: number) =>
    request<{ success: boolean; message: string }>(`/api/agents/${id}/publish`, { method: "POST" }),
  getReleaseGate: (id: number) =>
    request<ReleaseGateResult>(`/api/agents/${id}/release-gate`),

  // Prompt Templates
  listTemplates: () => request<PromptTemplate[]>("/api/prompt-templates"),
  createTemplate: (name: string, category: string, content: string) =>
    request<PromptTemplate>("/api/prompt-templates", {
      method: "POST",
      body: JSON.stringify({ name, category, content }),
    }),

  // Models
  listModels: () => request<ModelRegistryItem[]>("/api/models"),

  // Conversations
  listConversations: () => request<Conversation[]>("/api/conversations"),
  getMessages: (conversationId: number) =>
    request<Message[]>(`/api/conversations/${conversationId}/messages`),
  deleteConversation: (id: number) =>
    request<{ message: string }>(`/api/conversations/${id}`, { method: "DELETE" }),

  // Pipeline
  getPipelineStatus: (agentId: number) =>
    request<PipelineStatus>(`/api/agents/${agentId}/pipeline-status`),

  // Capabilities
  listCapabilities: (type?: string) =>
    request<CapabilityItem[]>(`/api/capabilities${type ? `?type=${type}` : ""}`),
  searchCapabilities: (q: string) =>
    request<CapabilityItem[]>(`/api/capabilities/search?q=${encodeURIComponent(q)}`),
  createCapability: (data: { type: string; name: string; description?: string; tags?: string; config?: string }) =>
    request<CapabilityItem>("/api/capabilities", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateCapability: (id: number, data: Record<string, string>) =>
    request<CapabilityItem>(`/api/capabilities/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  deleteCapability: (id: number) =>
    request<{ ok: boolean }>(`/api/capabilities/${id}`, { method: "DELETE" }),
  syncPlatformCapabilities: (data: { skills: Array<Record<string, unknown>>; connectors: Array<Record<string, unknown>> }) =>
    request<{ ok: boolean; skills: number; connectors: number }>("/api/capabilities/platform-sync", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // Skill bundle upload
  uploadSkillBundle: async (
    file: File
  ): Promise<{ name: string; description: string; version: number; file_count: number; source: string }> => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${getApiBase()}/api/capabilities/skills/upload`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const msg = body.detail?.message || body.detail?.error || body.detail || res.statusText;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return res.json();
  },

  // Builder
  startBuilder: () =>
    request<{ conversation_id: string; greeting: string }>("/api/builder/start", { method: "POST" }),
  applyBuilderRecommendation: (recommendation: Record<string, unknown>, agentName: string) =>
    request<Agent>("/api/builder/apply", {
      method: "POST",
      body: JSON.stringify({ recommendation, agent_name: agentName }),
    }),

  // Prompt Versions
  listPromptVersions: (agentId: number) =>
    request<PromptVersion[]>(`/api/agents/${agentId}/prompt-versions`),
  getPromptVersion: (agentId: number, versionId: number) =>
    request<PromptVersion>(`/api/agents/${agentId}/prompt-versions/${versionId}`),
  deletePromptVersion: (agentId: number, versionId: number) =>
    request<{ ok: boolean }>(`/api/agents/${agentId}/prompt-versions/${versionId}`, { method: "DELETE" }),

  // Prompt Optimizer
  optimizePrompt: (agentId: number, currentPrompt: Record<string, unknown>, recentMessages: Array<{ role: string; content: string }>) =>
    request<OptimizeResult>(`/api/agents/${agentId}/optimize-prompt`, {
      method: "POST",
      body: JSON.stringify({ current_prompt: currentPrompt, recent_messages: recentMessages }),
    }),

  // Tools
  listTools: (agentId: number) =>
    request<ToolConfig[]>(`/api/agents/${agentId}/tools`),
  createTool: (agentId: number, data: { name: string; description?: string; parameters?: string; mock_endpoint?: string }) =>
    request<ToolConfig>(`/api/agents/${agentId}/tools`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateTool: (agentId: number, toolId: number, data: Record<string, string>) =>
    request<ToolConfig>(`/api/agents/${agentId}/tools/${toolId}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  deleteTool: (agentId: number, toolId: number) =>
    request<{ ok: boolean }>(`/api/agents/${agentId}/tools/${toolId}`, { method: "DELETE" }),

  // Knowledge Base
  uploadDocuments: (agentId: number, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return request<{ document_count: number; chunk_count: number; status: string }>(
      `/api/agents/${agentId}/knowledge-base/documents`,
      { method: "POST", body: fd, headers: {} }
    );
  },
  listKBDocuments: (agentId: number) =>
    request<KBDocument[]>(`/api/agents/${agentId}/knowledge-base/documents`),
  deleteKBDocument: (agentId: number, filename: string) =>
    request<{ deleted: string; chunks_removed: number }>(
      `/api/agents/${agentId}/knowledge-base/documents/${encodeURIComponent(filename)}`,
      { method: "DELETE" }
    ),
  retrieveChunks: (agentId: number, query: string) =>
    request<RetrieveResult>(`/api/agents/${agentId}/knowledge-base/retrieve`, {
      method: "POST",
      body: JSON.stringify({ query }),
    }),

  // Node Types
  getNodeTypes: () =>
    request<Array<{
      node_type: string; display_name: string; category: string;
      config_schema: string; input_keys: string; output_keys: string; enabled: boolean;
    }>>("/api/node-types"),

  // DAG Graph
  getDAGGraph: (agentId: number) =>
    request<{ id: number; agent_id: number; graph_json: string; state_schema: string; version: number; created_at: string; updated_at: string }>(
      `/api/agents/${agentId}/dag-graph`
    ),
  saveDAGGraph: (agentId: number, graphJson: string, stateSchema?: string) =>
    request<{ id: number; agent_id: number; graph_json: string; state_schema: string; version: number }>(
      `/api/agents/${agentId}/dag-graph`,
      { method: "POST", body: JSON.stringify({ graph_json: graphJson, state_schema: stateSchema || "{}" }) }
    ),
  validateDAG: (agentId: number, graphJson: string) =>
    request<{ valid: boolean; errors: Array<{ node_id: string; node_type: string; rule_type: string; severity: string; message: string }>; warnings: Array<{ node_id: string; node_type: string; rule_type: string; severity: string; message: string }> }>(
      `/api/agents/${agentId}/dag-graph/validate`,
      { method: "POST", body: JSON.stringify({ graph_json: graphJson }) }
    ),
  listDAGVersions: (agentId: number) =>
    request<number[]>(`/api/agents/${agentId}/dag-graph/versions/list`),

  // DAG Templates
  listDAGTemplates: () =>
    request<Array<{ id: number; name: string; description: string; category: string; graph_json: string; icon: string; sort_order: number }>>(
      "/api/dag-templates"
    ),
  applyDAGTemplate: (templateId: number) =>
    request<{ agent_id: number; name: string; template_name: string }>(
      `/api/dag-templates/${templateId}/apply`,
      { method: "POST" }
    ),

  // Evaluation
  evaluateAgent: (agentId: number, testCases: Array<{
    name: string; input: string; expected_keywords?: string[];
    expected_sentiment?: string; expected_schema?: Record<string, unknown>; judge_prompt?: string;
  }>) =>
    request<{ passed: boolean; scores: string; results: string }>(
      `/api/agents/${agentId}/evaluate`,
      { method: "POST", body: JSON.stringify({ test_cases: testCases }) }
    ),
  listEvaluations: (agentId: number) =>
    request<Array<{ id: number; agent_id: number; dag_version: number; test_cases: string; scores: string; passed: boolean; created_at: string }>>(
      `/api/agents/${agentId}/evaluations`
    ),

  // Monitoring
  getMonitoring: (agentId: number) =>
    request<{
      agent_id: number; request_count_24h: number; request_count_7d: number; request_count_30d: number;
      p50_latency_ms: number; p95_latency_ms: number; token_consumption: number; error_rate: number; status: string;
      langfuse_enabled: boolean; langfuse_auth_ok: boolean; langfuse_base_url: string;
    }>(`/api/agents/${agentId}/monitoring`),
  getMonitoringTraces: (agentId: number) =>
    request<{ agent_id: number; traces: Array<{ trace_id: string; trace_url: string; duration_ms: number; status: string; node_count: number; created_at: string }> }>(
      `/api/agents/${agentId}/monitoring/traces`
    ),

  // Test Cases
  listTestCases: (agentId: number) =>
    request<Array<{ id: number; agent_id: number; name: string; input_message: string; expected_keywords: string; expected_sentiment: string | null; expected_schema: string; judge_prompt: string; sort_order: number; created_at: string }>>(
      `/api/agents/${agentId}/test-cases`
    ),
  createTestCase: (agentId: number, data: { name: string; input_message?: string; expected_keywords?: string; expected_sentiment?: string | null; expected_schema?: string; judge_prompt?: string }) =>
    request<{ id: number }>(`/api/agents/${agentId}/test-cases`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateTestCase: (agentId: number, tcId: number, data: Record<string, string | null>) =>
    request<{ id: number }>(`/api/agents/${agentId}/test-cases/${tcId}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  deleteTestCase: (agentId: number, tcId: number) =>
    request<{ ok: boolean }>(`/api/agents/${agentId}/test-cases/${tcId}`, { method: "DELETE" }),

  // Evaluation Suites / Cases (scoped to a suite)
  listSuites: (agentId: number) =>
    request<EvaluationSuiteItem[]>(`/api/agents/${agentId}/suites`),
  createSuite: (agentId: number, data: {
    name: string; description?: string;
    suite_type?: string; default_dimensions_json?: string; pass_threshold?: number;
  }) =>
    request<EvaluationSuiteItem>(`/api/agents/${agentId}/suites`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteSuite: (agentId: number, suiteId: number) =>
    request<{ ok: boolean }>(`/api/agents/${agentId}/suites/${suiteId}`, { method: "DELETE" }),
  listSuiteCases: (agentId: number, suiteId: number) =>
    request<EvaluationCaseItem[]>(`/api/agents/${agentId}/test-cases?suite_id=${suiteId}`),
  createSuiteCase: (agentId: number, data: {
    name: string; input_message?: string; expected_keywords?: string;
    expected_schema?: string; judge_prompt?: string; suite_id: number; is_key?: boolean; notes?: string;
  }) =>
    request<EvaluationCaseItem>(`/api/agents/${agentId}/test-cases`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // Evaluation Runs / Comparison
  startEvaluationRun: (agentId: number, suiteId: number) =>
    request<EvaluationRunItem>(`/api/agents/${agentId}/suites/${suiteId}/runs`, { method: "POST" }),
  listEvaluationRuns: (agentId: number, suiteId?: number) =>
    request<EvaluationRunItem[]>(
      `/api/agents/${agentId}/runs${suiteId != null ? `?suite_id=${suiteId}` : ""}`
    ),
  getEvaluationRun: (agentId: number, runId: number) =>
    request<EvaluationRunDetail>(`/api/agents/${agentId}/runs/${runId}`),
  compareEvaluationRuns: (agentId: number, baselineId: number, candidateId: number) =>
    request<EvaluationCompareResult>(
      `/api/agents/${agentId}/runs/compare?baseline_id=${baselineId}&candidate_id=${candidateId}`
    ),

  // Planner snapshot — returns null on 404 (no proposal recorded for this agent).
  getPlannerSnapshot: async (agentId: number): Promise<{
    requirement_summary: string;
    task_classification: string;
    confirmed_constraints: string[];
    rationale: string;
    user_feedback_summary: string[];
    proposal_version: number;
    created_at: string;
  } | null> => {
    const res = await fetch(`${getApiBase()}/api/planner/proposal/${agentId}`);
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(res.statusText);
    return res.json();
  },

  // Planner sessions
  listPlannerSessions: () =>
    request<Array<{
      conversation_id: string;
      session_title: string;
      stage: string;
      linked_agent_id: number | null;
      last_updated_at: string;
      created_at: string;
    }>>("/api/planner/sessions"),
  getPlannerSession: (conversationId: string) =>
    request<{
      conversation_id: string;
      session_title: string;
      stage: string;
      user_request: string;
      memory: {
        requirement_summary: string;
        confirmed_constraints: string[];
        task_classification: string;
        latest_proposal_summary: string;
        user_feedback: string[];
      };
      messages: Array<{ role: string; content: string }>;
      file_artifacts: Array<{ path: string; kind: string; updated_at: string }>;
      linked_agent_id: number | null;
      mode: string;
      replan_context: string;
      last_updated_at: string;
      created_at: string;
    }>(`/api/planner/sessions/${encodeURIComponent(conversationId)}`),
  createSessionFromAgent: (agentId: number) =>
    request<{ conversation_id: string; session_title: string; mode: string }>(
      `/api/planner/sessions/from-agent/${agentId}`,
      { method: "POST" }
    ),
  applyReplan: (data: {
    agent_id: number;
    proposal: Record<string, unknown>;
    memory?: Record<string, unknown>;
    conversation_id?: string | null;
    landing: "save_new_version" | "override_draft" | "partial";
    selected_node_ids?: string[];
  }) =>
    request<{ id: number; name: string; version: number; landing: string }>(
      "/api/planner/apply-replan",
      { method: "POST", body: JSON.stringify(data) }
    ),
  deletePlannerSession: (conversationId: string) =>
    request<{ ok: boolean; removed: string }>(
      `/api/planner/sessions/${encodeURIComponent(conversationId)}`,
      { method: "DELETE" }
    ),
  uploadPlannerAttachment: (conversationId: string, file: File): Promise<AttachmentMeta> => {
    const fd = new FormData();
    fd.append("file", file);
    return request<AttachmentMeta>(
      `/api/planner/sessions/${encodeURIComponent(conversationId)}/attachments`,
      { method: "POST", body: fd, headers: {} }
    );
  },
  listPlannerSkills: (conversationId?: string) =>
    request<PlannerSkillItem[]>(
      `/api/planner/skills${conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ""}`
    ),

  // ── Plan-first builder loop (T7): proposals / draft agents / run events ──
  // Read-only proposal (T5). Returns { proposal: null } when none exists.
  getProposalByConversation: (conversationId: string) =>
    request<Record<string, unknown>>(
      `/api/planner/proposals/${encodeURIComponent(conversationId)}`
    ),
  getProposalById: (proposalId: number) =>
    request<Record<string, unknown>>(`/api/planner/proposals/by-id/${proposalId}`),
  // Advance a proposal's lifecycle (T5): confirm derives a draft agent.
  transitionProposal: (proposalId: number, action: "confirm" | "reject") =>
    request<Record<string, unknown>>(
      `/api/planner/proposals/by-id/${proposalId}/transition`,
      { method: "POST", body: JSON.stringify({ action }) }
    ),
  getDraftAgent: (draftAgentId: number) =>
    request<Record<string, unknown>>(`/api/planner/draft-agents/${draftAgentId}`),
  // Compile + (optional) dry-run a draft agent (T6). Never creates an Agent.
  compileDraftAgent: (draftAgentId: number, dryRun: boolean = true) =>
    request<{
      success: boolean;
      errors: string[];
      warnings: string[];
      graph_json?: string | null;
      dryrun?: { ran: boolean; ok: boolean; message: string };
    }>(`/api/planner/draft-agents/${draftAgentId}/compile`,
      { method: "POST", body: JSON.stringify({ dry_run: dryRun }) }),
  // Read-only run-event timeline for a planner run (T8).
  getRunEvents: (runId: string) =>
    request<{ run_id: string; events: Array<Record<string, unknown>> }>(
      `/api/planner/runs/${encodeURIComponent(runId)}/events`
    ),
};
