export type AtlasRuntimeBinding = Readonly<{
  agentId: string;
  versionId?: string;
}>;

export function atlasRuntimeBindingFromUrl(
  gatewayAgentId: number,
  href: string,
  embedded: boolean,
): { binding: AtlasRuntimeBinding | null; error: string | null } {
  if (!embedded) return { binding: null, error: null };
  const url = new URL(href);
  const atlasAgentId = url.searchParams.get("atlas_agent_id")?.trim() || "";
  const mappedGatewayId = url.searchParams.get("atlas_gateway_agent_id")?.trim() || "";
  if (!atlasAgentId) {
    return { binding: null, error: "Atlas Runtime 未绑定智能体，请在宿主工作台选择 Atlas 智能体。" };
  }
  if (mappedGatewayId && mappedGatewayId !== String(gatewayAgentId)) {
    return { binding: null, error: "Atlas Runtime 映射与当前 AgentGateway 智能体不一致。" };
  }
  const versionId = url.searchParams.get("atlas_agent_version_id")?.trim() || undefined;
  return { binding: { agentId: atlasAgentId, ...(versionId ? { versionId } : {}) }, error: null };
}
