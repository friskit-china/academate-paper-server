import type { OnboardingCredentialWrite } from "../api";
import { validateDisplayName } from "../account-validation.ts";

export const ONBOARDING_STEPS = [
  "欢迎",
  "个人资料",
  "个人 LLM",
  "使用偏好",
  "确认",
] as const;

export const ONBOARDING_REVIEW_STEP = 4;

export type OnboardingDraft = Readonly<{
  displayName: string;
  configureLlm: boolean;
  credential: OnboardingCredentialWrite;
}>;

function isOptionalHttpUrl(value: string): boolean {
  const normalized = value.trim();
  if (normalized.length === 0) return true;
  if (normalized.length > 2048) return false;
  try {
    const parsed = new URL(normalized);
    return (parsed.protocol === "http:" || parsed.protocol === "https:")
      && parsed.host.length > 0
      && parsed.username === ""
      && parsed.password === ""
      && parsed.search === ""
      && parsed.hash === "";
  } catch {
    return false;
  }
}

export function validateOnboardingStep(
  step: number,
  draft: OnboardingDraft,
): string | null {
  if (step === 1) {
    return validateDisplayName(draft.displayName);
  }
  if (step === 2 && draft.configureLlm) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(draft.credential.provider.trim())) {
      return "服务商标识需要是 1 至 64 个字母、数字、点、下划线或连字符。";
    }
    if (draft.credential.api_key.length < 8 || draft.credential.api_key.length > 8192) {
      return "API 凭据需要 8 至 8192 个字符。";
    }
    if ((draft.credential.model?.trim().length ?? 0) > 128) {
      return "模型名称不能超过 128 个字符。";
    }
    if (!isOptionalHttpUrl(draft.credential.api_base ?? "")) {
      return "API 基础地址需要是无账号、查询或 fragment 的 HTTP(S) 地址，或留空使用默认值。";
    }
  }
  return null;
}

export function onboardingCredentialPayload(
  draft: OnboardingDraft,
): OnboardingCredentialWrite | null {
  if (!draft.configureLlm) return null;
  const apiBase = draft.credential.api_base?.trim() ?? "";
  const model = draft.credential.model?.trim() ?? "";
  return {
    provider: draft.credential.provider.trim(),
    api_key: draft.credential.api_key,
    api_base: apiBase.length === 0 ? null : apiBase,
    model: model.length === 0 ? null : model,
  };
}
