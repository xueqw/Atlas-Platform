export type Source={document:string;page:number;quote:string;score:number}
export type Message={id:string;role:'user'|'assistant';content:string;sources:string;created_at:string}
export type Conversation={id:string;title:string;created_at:string;updated_at:string;messages?:Message[]}
export type KnowledgeDocument={id:string;knowledge_base_id:string;name:string;content_type:string;size:number;status:string;chunk_count:number;created_at:string}
export type KnowledgeBase={id:string;name:string;description:string;created_at:string;documents:KnowledgeDocument[]}
export type Agent={id:string;name:string;description:string;system_prompt:string;model:string;knowledge_base_id:string|null;status:string;kind:string;version_no:number;published_version_no:number|null;knowledge_base_count:number;skills_count:number;connector_count:number;call_count:number;created_by:string|null;last_eval_ok:boolean|null;has_passed_test:boolean;deploy_config_configured:boolean;workflow_stage:string;health_status:string;success_rate:number|null;has_unpublished_changes:boolean;created_at:string;updated_at:string}
export type AgentTemplate={id:string;name:string;description:string}
export type PublishChecklistItem={key:string;label:string;ok:boolean;level:'blocking'|'warning'}
export type PublishChecklist={items:PublishChecklistItem[];can_publish:boolean}
export type DeployConfig={visibility:'private'|'shared'|'workspace'|'marketplace';shared_user_ids:string[];allowed_knowledge_base_ids:string[];allowed_skill_ids:string[];allowed_connectors:string[];write_confirm:boolean;api_access:boolean;call_log_enabled:boolean;high_risk_approved:boolean}
export type CallLogEntry={id:string;time:string;source:string;status:string;latency_ms:number|null;error:string;request_path:string;status_code:number|null;token_usage:number|null}
export type WorkspaceMember={id:string;name:string;username:string}
export type AgentVersion={id:string;agent_id:string;version_no:number;kind:string;label:string;note:string;created_at:string}
export type AgentVersionDetail=AgentVersion&{snapshot:Record<string,unknown>}
export type VersionFileDiff={path:string;status:'added'|'removed'|'modified';diff:string}
export type VersionFieldDiff={field:string;old:unknown;new:unknown}
export type AgentVersionDiff={from_version:number;to_version:number;kind:string;files:VersionFileDiff[];fields:VersionFieldDiff[]}
export type ModelProvider={id:string;name:string;base_url:string;configured:boolean;models:string[];note:string}
export type ModelCatalog={default:string;providers:ModelProvider[]}
export type ModelTestResult={ok:boolean;latency_ms?:number;message?:string}
export type Connector={provider:string;name:string;description:string;configured:boolean;connected:boolean;account_name:string;actions:string[];config_kind?:string;key_label?:string}
export type User={id:string;username:string;name:string;email:string;avatar_url:string}
export type Workspace={id:string;name:string}
export type Me={user:User;workspace:Workspace;role:string}
export type Account={username:string;name:string}
export type PlanStep={id:string;type:string;title:string;executor:string;connector?:string;risk?:string}
export type Plan={goal:string;requires_knowledge:boolean;requires_tools:boolean;steps:PlanStep[];source:string}
export type Skill={id:string;name:string;description:string;type:string;trigger_phrases:string;content:string;builtin:boolean;status:string;created_at:string;updated_at:string}
export type SelectedSkill={id:string;name:string;source:string}
export type WorkflowStep={id:string;index:number;type:string;title:string;executor:string;skill_id:string|null;status:string;input_json:string;output_json:string;error:string;started_at:string|null;ended_at:string|null}
export type WorkflowRun={id:string;conversation_id:string|null;agent_id:string|null;user_id:string|null;input_text:string;status:string;plan_json:string;output_json:string;error:string;started_at:string|null;ended_at:string|null;created_at:string;steps:WorkflowStep[]}
export type RuntimeRunHandle={run_id:string;thread_id:string;status:'pending'|'running'|'paused'|'succeeded'|'failed'|'cancelled';execution_mode:string;created_at:string}
export type RuntimeRunState={identity:{workspace_id:string;user_id:string;agent_id:string;agent_version_id:string;run_id:string;thread_id:string;source:string;conversation_id:string|null};status:RuntimeRunHandle['status'];node_status:Record<string,string>;retry_counters:Record<string,number>;errors:Array<{category?:string;message?:string;node?:string}>;output:string|null;side_effects_started:boolean}
export type GovernedMemoryFact={fact_id:string;statement:string;evidence:string;confidence:number;sensitivity:string;source_event_id:string;valid_from:string;transaction_from:string}
export type GovernedMemory={scope:{workspace_id:string;user_id:string;agent_id:string};untrusted_context:boolean;retrieved_at:string;facts:GovernedMemoryFact[];exclusions:string[]}
export type SkillDiscoveryItem={id:string;name:string;category_path:string[];summary?:string;use_when?:string[];do_not_use_when?:string[];version?:string;status?:string}
export type SkillDiscovery={tree:{children:Record<string,unknown>};skills:SkillDiscoveryItem[]}
export type ApiKey={exists:boolean;key_prefix:string;status:string;expires_at:string|null;daily_quota:number|null;allowed_origins:string;created_at:string|null;last_used_at:string|null}
export type ApiKeyCreated=ApiKey&{key:string}
export type EvaluationCaseV2={id:string;name:string;input_text:string;expected_text:string;scorers:Record<string,unknown>;is_key:boolean;sort_order:number}
export type EvaluationSuite={id:string;agent_id:string;name:string;description:string;pass_threshold:number;is_release_gate:boolean;created_at:string;cases:EvaluationCaseV2[]}
export type EvaluationScore={dimension:string;score:number;passed:boolean;reason:string}
export type EvaluationRunV2={id:string;ok:boolean;suite_id:string;agent_version_id:string|null;summary:{passed:number;total:number;pass_rate:number;threshold:number;key_cases_passed:boolean};results:Array<{case_id:string;name:string;input:string;output:string;ok:boolean;is_key:boolean;scores:EvaluationScore[];elapsed_ms:number;error:string}>;created_at:string}
export type EvaluationRunHistory = EvaluationRunV2
export type EvaluationRunComparison = {
  baseline: Pick<EvaluationRunHistory, 'id' | 'agent_version_id' | 'ok' | 'summary' | 'created_at'>
  candidate: Pick<EvaluationRunHistory, 'id' | 'agent_version_id' | 'ok' | 'summary' | 'created_at'>
  deltas: { pass_rate: number | null; average_latency_ms: number | null }
  regressions: Array<{ case_id: string; name: string; is_key: boolean }>
  improvements: Array<{ case_id: string; name: string; is_key: boolean }>
}
