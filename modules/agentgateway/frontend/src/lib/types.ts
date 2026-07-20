export type AgentStatus = "draft" | "testing" | "published" | "deprecated";

export interface PromptConfig {
  role_name: string;
  role_description: string;
  output_format: string;
  constraints: string;
  system_prompt: string;
}

export interface ModelConfig {
  provider: string;
  model_name: string;
  temperature: number;
  max_tokens: number;
  top_p: number;
  streaming: boolean;
}

export interface Agent {
  id: number;
  name: string;
  description: string;
  status: AgentStatus;
  version: number;
  prompt_config: PromptConfig | null;
  model: ModelConfig | null;
  created_at: string;
  updated_at: string;
}

export interface PromptTemplate {
  id: number;
  name: string;
  category: string;
  preview: string;
  content: string;
  created_at: string;
}

export interface ModelRegistryItem {
  id: number;
  provider: string;
  model_id: string;
  display_name: string;
  capability_tags: string;
  context_window: number;
  max_output_tokens: number;
  input_price_per_1k: number;
  output_price_per_1k: number;
  supports_streaming: boolean;
  supports_vision: boolean;
  is_available: boolean;
  configured: boolean;
}

export interface Conversation {
  id: number;
  agent_id: number;
  title: string;
  created_at: string;
  updated_at: string;
  last_message_preview: string;
}

export interface Message {
  id: number;
  conversation_id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface PipelineNodeStatus {
  node_name: string;
  status: "pending" | "in_progress" | "complete" | "failed";
  quality_score: number | null;
  started_at: string | null;
  completed_at: string | null;
  trace_url: string | null;
}

export interface PipelineStatus {
  agent_id: number;
  nodes: PipelineNodeStatus[];
}

export interface KBDocument {
  filename: string;
  chunk_count: number;
  status: string;
}

export interface RetrieveChunk {
  chunk_id: string;
  content: string;
  score: number;
  source: string;
  chunk_index: number;
}

export interface RetrieveResult {
  results: RetrieveChunk[];
}

export interface CapabilityItem {
  id: number;
  type: string;
  name: string;
  description: string;
  tags: string;
  config: string;
  created_at: string;
  updated_at: string;
}

export interface PromptVersion {
  id: number;
  agent_id: number;
  version_number: number;
  prompt_config: string;
  created_at: string;
}

export interface OptimizeResult {
  optimized_prompt: {
    role_name: string;
    role_description: string;
    system_prompt: string;
  };
  changes: Array<{
    field: string;
    before: string;
    after: string;
    reason: string;
  }>;
}

export interface ToolConfig {
  id: number;
  agent_id: number;
  name: string;
  description: string;
  parameters: string;
  mock_endpoint: string;
  created_at: string;
  updated_at: string;
}

export interface AttachmentMeta {
  path: string;
  kind: "image" | "text";
  mime: string;
  name: string;
  size: number;
  preview_url: string;
}

export interface PlannerSkillItem {
  name: string;
  description: string;
  tags: string[];
  source: string;
  entrypoint: string;
  attached_to_current?: boolean | null;
  is_default?: boolean;
  // Three-tier contract fields (planner-effective-skills). Derived server-side.
  default_source?: "system" | "workspace" | "none";
  effective_in_current?: boolean | null;
  lock_reason?: string | null;
}

export interface EvaluationSuiteItem {
  id: number;
  agent_id: number;
  name: string;
  description: string;
  created_at: string;
  case_count: number;
  // Dimension-driven evaluation (Phase 1)
  suite_type?: string;
  default_dimensions_json?: string;
  pass_threshold?: number;
}

export interface EvaluationCaseItem {
  id: number;
  agent_id: number;
  suite_id: number | null;
  name: string;
  input_message: string;
  expected_keywords: string;
  expected_sentiment: string | null;
  expected_schema: string;
  judge_prompt: string;
  is_key: boolean;
  notes: string;
  sort_order: number;
  created_at: string;
}

export interface EvaluationRunItem {
  id: number;
  agent_id: number;
  suite_id: number;
  dag_version: number;
  prompt_version: number;
  model: string;
  trace_ids: string;
  summary: string;
  passed: boolean;
  created_at: string;
}

export interface EvaluationDimensionResult {
  dimension: string;
  type: string;
  score: number;
  passed: boolean;
  threshold: number;
  weight: number;
  required: boolean;
  reason: string;
  evidence: Record<string, unknown>;
  skipped: boolean;
}

export interface EvaluationCaseResultItem {
  id: number;
  run_id: number;
  case_id: number;
  case_name: string;
  is_key: boolean;
  output: string;
  scores: string;
  passed: boolean;
  trace_id: string;
  duration_ms: number;
  // Dimension-driven evaluation (Phase 1)
  trace_url?: string;
  dimension_results?: EvaluationDimensionResult[];
  dimension_results_json?: string;
  evidence_json?: string;
  // Langfuse score writeback (Phase 2)
  langfuse_score_ids?: string[];
  langfuse_score_count?: number;
}

export interface EvaluationRunDetail extends EvaluationRunItem {
  case_results: EvaluationCaseResultItem[];
}

export interface EvaluationVersionTag {
  run_id: number;
  dag_version: number;
  prompt_version: number;
  model: string;
  created_at: string;
}

export interface EvaluationCompareResult {
  baseline: EvaluationVersionTag;
  candidate: EvaluationVersionTag;
  deltas: {
    avg_score: number | null;
    pass_rate: number | null;
    key_pass_rate: number | null;
    latency_avg_ms: number | null;
    token_input: number | null;
    token_output: number | null;
  };
  baseline_summary: Record<string, unknown>;
  candidate_summary: Record<string, unknown>;
  regressions: Array<{ case_id: number; case_name: string; is_key: boolean; baseline_passed: boolean; candidate_passed: boolean }>;
  improvements: Array<{ case_id: number; case_name: string; is_key: boolean }>;
}

export interface ReleaseGateFailure {
  code: string;
  label: string;
  dimension?: string;
  actual?: number;
  threshold?: number;
  cases?: Array<{ case_id?: number; case_name?: string; dimension?: string; score?: number; threshold?: number }>;
}

export interface ReleaseGateResult {
  passed: boolean;
  reasons: string[];
  // Structured failing items (Phase 3), parallel to `reasons`.
  failures?: ReleaseGateFailure[];
  summary: {
    thresholds?: Record<string, number>;
    suite_type?: string;
    gate_dimensions?: Array<{ name: string; min_avg?: number; required_no_fail?: boolean }>;
    dimensions_checked?: boolean;
    dimension_averages?: Record<string, number>;
    dimensions_skipped?: string[];
    worst_dimension?: { dimension: string; avg: number };
    evaluation?: {
      run_id: number;
      passed: boolean;
      pass_rate: number | null;
      key_total: number | null;
      key_passed: number | null;
      key_pass_rate: number | null;
      latency_p95_ms: number | null;
      created_at: string;
    };
    observability?: {
      run_count: number;
      error_rate: number;
      latency_p95_ms: number;
      avg_token: number;
    };
  };
}
