export const OPENAI_REASONING_EFFORTS: Record<string, readonly string[]> = {
  "gpt-5.5": ["none", "low", "medium", "high", "xhigh"],
  "gpt-5.6-luna": ["none", "low", "medium", "high", "xhigh", "max"],
  "gpt-5.6-terra": ["none", "low", "medium", "high", "xhigh", "max"],
  "gpt-5.6-sol": ["none", "low", "medium", "high", "xhigh", "max"],
  "gpt-6-astra": ["low", "medium", "high", "xhigh", "max"],
};

export const OPENAI_VISION_MODELS = Object.freeze(Object.keys(OPENAI_REASONING_EFFORTS));

export function validReasoningEffort(model: string, effort: unknown): effort is string {
  return typeof effort === "string" && OPENAI_REASONING_EFFORTS[model]?.includes(effort);
}
