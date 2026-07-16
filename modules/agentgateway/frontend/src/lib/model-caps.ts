// Frontend mirror of backend app/core/model_caps.py — keep the two in sync.
// Used to decide whether image attachments will be honored by the selected model.
const MULTIMODAL_MODELS = [
  "gpt-4o",
  "gpt-4o-mini",
  "gpt-4-vision-preview",
  "qwen-vl-plus",
  "qwen-vl-max",
  "glm-4v",
  "glm-4v-plus",
];

export function isMultimodalModel(modelId: string): boolean {
  if (!modelId) return false;
  const mid = modelId.trim().toLowerCase();
  return MULTIMODAL_MODELS.some((m) => mid === m || mid.startsWith(m));
}
