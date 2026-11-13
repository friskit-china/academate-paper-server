import type { LlmCredentialWrite } from "../api";
import { validatePassword } from "../account-validation.ts";

export const SETUP_STEPS = [
  "欢迎",
  "登录密码",
  "工作区与模型",
  "确认配置",
  "开始使用",
] as const;

export const REVIEW_STEP = SETUP_STEPS.length - 2;
export const PROGRESS_STEP = SETUP_STEPS.length - 1;

export type SetupWizardDraft = {
  bootstrapToken: string;
  password: string;
  passwordConfirmation: string;
  workspaceName: string;
  configureLlm: boolean;
  llmCredential: LlmCredentialWrite;
};

export function maskSecret(value: string, revealSuffix = false): string {
  if (value.length === 0) return "未提供";
  if (revealSuffix) {
    const characters = Array.from(value);
    return characters.length > 8 ? `********${characters.slice(-4).join("")}` : "********";
  }
  return "••••••••（已提供）";
}

function isValidBootstrapToken(value: string): boolean {
  return value.length >= 32
    && value.length <= 512
    && /^[\x21-\x7e]+$/.test(value);
}

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function validateSetupStep(
  step: number,
  draft: SetupWizardDraft,
): string | null {
  if (step === 0 && !isValidBootstrapToken(draft.bootstrapToken)) {
    return "请输入首次配置链接提供的有效配置凭证。";
  }
  if (step === 1) {
    const passwordError = validatePassword(draft.password);
    if (passwordError !== null) return passwordError;
    if (draft.password !== draft.passwordConfirmation) return "两次输入的密码不一致。";
  }
  if (step === 2) {
    if (draft.workspaceName.trim().length === 0) return "请输入工作区名称。";
    if (!draft.configureLlm) return null;
    if (draft.llmCredential.provider.trim().length === 0) return "请输入 LLM 服务商标识。";
    if (draft.llmCredential.model.trim().length === 0) return "请输入 LLM 模型名称。";
    if (!isHttpUrl(draft.llmCredential.api_base)) return "请输入有效的 HTTP(S) API 地址。";
    if (draft.llmCredential.api_key.length < 8) return "LLM API 凭据至少需要 8 个字符。";
  }
  return null;
}
