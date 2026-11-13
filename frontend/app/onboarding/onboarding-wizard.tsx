"use client";

import { useEffect, useRef, useState } from "react";

import {
  ApiRequestError,
  completeOnboarding,
  getOnboardingStatus,
  type OnboardingCredentialWrite,
  type OnboardingStatus,
} from "../api";
import { storedKeyPlaceholder } from "../credential-hint";
import { maskSecret } from "../setup/setup-wizard-state";
import {
  onboardingCredentialPayload,
  ONBOARDING_REVIEW_STEP,
  ONBOARDING_STEPS,
  type OnboardingDraft,
  validateOnboardingStep,
} from "./onboarding-state";

const EMPTY_PERSONAL_CREDENTIAL: OnboardingCredentialWrite = {
  provider: "",
  api_key: "",
  api_base: null,
  model: null,
};

function onboardingError(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return "暂时无法连接个人向导服务，请稍后重试。";
  if (error.status === 401) return "登录会话已失效，请重新登录。";
  if (error.status === 403) return "当前账号不能访问这份个人向导。";
  if (error.status === 422) return "个人设置未通过校验，请检查填写内容后重试。";
  if (error.status === 503) return "个人设置服务或受保护凭据存储暂时不可用，请稍后重试。";
  return "个人向导未能安全保存，请稍后重试。";
}

function storedCredentialSummary(status: OnboardingStatus): string | null {
  const credential = status.default_credential;
  if (credential === null) return null;
  const details = [credential.provider, credential.model, storedKeyPlaceholder(credential)]
    .filter((value): value is string => typeof value === "string" && value.length > 0);
  return details.join(" · ");
}

export function OnboardingWizard() {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const submitInFlight = useRef(false);
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [step, setStep] = useState(0);
  const [displayName, setDisplayName] = useState("");
  const [configureLlm, setConfigureLlm] = useState(false);
  const [credential, setCredential] = useState<OnboardingCredentialWrite>({
    ...EMPTY_PERSONAL_CREDENTIAL,
  });
  const [automaticTranslation, setAutomaticTranslation] = useState(false);
  const [automaticAiRepair, setAutomaticAiRepair] = useState(false);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const draft: OnboardingDraft = { displayName, configureLlm, credential };

  useEffect(() => {
    let cancelled = false;
    void getOnboardingStatus()
      .then((nextStatus) => {
        if (cancelled) return;
        if (!nextStatus.onboarding_required || nextStatus.status === "COMPLETED") {
          window.location.replace("/library");
          return;
        }
        setStatus(nextStatus);
        setDisplayName(nextStatus.display_name);
        setAutomaticTranslation(nextStatus.automatic_translation_enabled);
        setAutomaticAiRepair(nextStatus.automatic_ai_repair_enabled);
      })
      .catch((caught) => {
        if (!cancelled) setError(onboardingError(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  function updateCredential(field: keyof OnboardingCredentialWrite, value: string) {
    setCredential((current) => ({ ...current, [field]: value }));
  }

  function advance() {
    const validationError = validateOnboardingStep(step, draft);
    if (validationError !== null) {
      setError(validationError);
      return;
    }
    setError(null);
    setStep((current) => Math.min(current + 1, ONBOARDING_REVIEW_STEP));
  }

  function back() {
    if (submitting) return;
    setError(null);
    setStep((current) => Math.max(0, current - 1));
  }

  async function complete() {
    if (submitInFlight.current) return;
    const profileError = validateOnboardingStep(1, draft);
    const credentialError = validateOnboardingStep(2, draft);
    if (profileError !== null || credentialError !== null) {
      setError(profileError ?? credentialError);
      return;
    }

    submitInFlight.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const completed = await completeOnboarding({
        display_name: displayName.trim(),
        automatic_translation_enabled: automaticTranslation,
        automatic_ai_repair_enabled: automaticAiRepair,
        credential: onboardingCredentialPayload(draft),
      });
      setCredential({ ...EMPTY_PERSONAL_CREDENTIAL });
      if (completed.onboarding_required || completed.status !== "COMPLETED") {
        throw new Error("The onboarding completion response did not close onboarding.");
      }
      window.location.replace("/library");
    } catch (caught) {
      setError(onboardingError(caught));
      submitInFlight.current = false;
      setSubmitting(false);
    }
  }

  if (loading) {
    return <p className="setup-message" role="status">正在读取你的个人向导进度…</p>;
  }
  if (status === null) {
    return (
      <section className="onboarding-unavailable" role="alert">
        <h2>暂时无法打开个人向导</h2>
        <p>{error ?? "请稍后刷新页面重试。"}</p>
      </section>
    );
  }

  const storedCredential = storedCredentialSummary(status);

  return (
    <form className="setup-form onboarding-form" onSubmit={(event) => event.preventDefault()}>
      <div className="setup-stepper onboarding-stepper" aria-label="个人向导进度">
        <p>第 {step + 1} 步，共 {ONBOARDING_STEPS.length} 步</p>
        <ol>
          {ONBOARDING_STEPS.map((label, index) => (
            <li
              aria-current={index === step ? "step" : undefined}
              data-state={index < step ? "complete" : index === step ? "current" : "upcoming"}
              key={label}
            >
              <span>{index < step ? "✓" : index + 1}</span>
              <small>{label}</small>
            </li>
          ))}
        </ol>
      </div>

      <div className="setup-wizard-panel onboarding-wizard-panel">
        {step === 0 ? (
          <section aria-labelledby="onboarding-welcome-heading">
            <p className="eyebrow">欢迎加入 AcadeMate Paper</p>
            <h2 id="onboarding-welcome-heading" ref={headingRef} tabIndex={-1}>先完成你的个人设置</h2>
            <p>这个向导只配置你的资料、个人 LLM 凭据和阅读偏好。完成后即可进入文库。</p>
            <div className="setup-info-box">
              <strong>LLM 凭据可以跳过</strong>
              <p>不配置凭据也可以导入和阅读论文；需要翻译或辅助修复时再到个人设置中添加。</p>
            </div>
          </section>
        ) : null}

        {step === 1 ? (
          <section aria-labelledby="onboarding-profile-heading">
            <p className="eyebrow">个人资料</p>
            <h2 id="onboarding-profile-heading" ref={headingRef} tabIndex={-1}>确认你的显示信息</h2>
            <div className="setup-form-grid">
              <label>
                <span>用户名</span>
                <input aria-readonly="true" readOnly value={status.username} />
              </label>
              <label>
                <span>显示名称</span>
                <input
                  autoComplete="name"
                  maxLength={128}
                  onChange={(event) => setDisplayName(event.target.value)}
                  required
                  value={displayName}
                />
              </label>
            </div>
            <p className="setup-field-help">用户名由注册结果确定；显示名称可以在此修改。</p>
          </section>
        ) : null}

        {step === 2 ? (
          <section aria-labelledby="onboarding-credential-heading">
            <p className="eyebrow">个人 LLM</p>
            <h2 id="onboarding-credential-heading" ref={headingRef} tabIndex={-1}>配置个人 LLM 凭据（可选）</h2>
            {storedCredential !== null ? (
              <div className="setup-info-box">
                <strong>已有受保护凭据</strong>
                <p>{storedCredential}。不勾选替换时会保留现有凭据。</p>
              </div>
            ) : null}
            <label className="setup-optional-toggle">
              <input
                checked={configureLlm}
                onChange={(event) => setConfigureLlm(event.target.checked)}
                type="checkbox"
              />
              <span>
                <strong>{storedCredential === null ? "现在添加个人凭据" : "现在替换个人凭据"}</strong>
                <small>关闭此选项即可跳过；密钥不会在完成后返回到浏览器。</small>
              </span>
            </label>
            {configureLlm ? (
              <fieldset className="setup-credential-fieldset">
                <legend>新的个人凭据</legend>
                <div className="credential-fields">
                  <div className="setup-form-grid">
                    <label>
                      <span>服务商标识</span>
                      <input
                        autoComplete="off"
                        maxLength={64}
                        onChange={(event) => updateCredential("provider", event.target.value)}
                        required
                        value={credential.provider}
                      />
                    </label>
                    <label>
                      <span>模型（可选）</span>
                      <input
                        autoComplete="off"
                        maxLength={128}
                        onChange={(event) => updateCredential("model", event.target.value)}
                        value={credential.model ?? ""}
                      />
                    </label>
                  </div>
                  <label>
                    <span>API 基础地址（可选）</span>
                    <input
                      autoComplete="off"
                      inputMode="url"
                      maxLength={2048}
                      onChange={(event) => updateCredential("api_base", event.target.value)}
                      placeholder="留空使用服务商默认地址"
                      type="url"
                      value={credential.api_base ?? ""}
                    />
                  </label>
                  <label>
                    <span>API 凭据</span>
                    <input
                      autoComplete="new-password"
                      maxLength={8192}
                      minLength={8}
                      onChange={(event) => updateCredential("api_key", event.target.value)}
                      placeholder={storedKeyPlaceholder(status.default_credential)}
                      required
                      type="password"
                      value={credential.api_key}
                    />
                  </label>
                </div>
              </fieldset>
            ) : null}
          </section>
        ) : null}

        {step === 3 ? (
          <section aria-labelledby="onboarding-preferences-heading">
            <p className="eyebrow">使用偏好</p>
            <h2 id="onboarding-preferences-heading" ref={headingRef} tabIndex={-1}>选择个人自动化偏好</h2>
            <label className="automatic-toggle-setting onboarding-toggle">
              <span>
                <strong>自动翻译</strong>
                <small>开启后，新导入论文在解析完成时可以自动提交翻译任务。</small>
              </span>
              <input
                aria-label="新论文解析后自动翻译"
                checked={automaticTranslation}
                onChange={(event) => setAutomaticTranslation(event.target.checked)}
                type="checkbox"
              />
            </label>
            <label className="automatic-toggle-setting onboarding-toggle">
              <span>
                <strong>自动 AI 辅助修复</strong>
                <small>开启后，符合条件的渲染失败可以自动提交辅助修复任务。</small>
              </span>
              <input
                aria-label="自动提交符合条件的 AI 辅助修复"
                checked={automaticAiRepair}
                onChange={(event) => setAutomaticAiRepair(event.target.checked)}
                type="checkbox"
              />
            </label>
          </section>
        ) : null}

        {step === ONBOARDING_REVIEW_STEP ? (
          <section aria-labelledby="onboarding-review-heading">
            <p className="eyebrow">确认</p>
            <h2 id="onboarding-review-heading" ref={headingRef} tabIndex={-1}>检查你的个人设置</h2>
            <p>确认后会一次保存并完成个人向导。</p>
            <dl className="setup-review-list">
              <div><dt>用户名</dt><dd>{status.username}</dd></div>
              <div><dt>显示名称</dt><dd>{displayName.trim()}</dd></div>
              <div>
                <dt>个人 LLM</dt>
                <dd>
                  {configureLlm
                    ? `${credential.provider.trim()} · ${credential.model?.trim() || "默认模型"} · ${credential.api_base?.trim() || "默认地址"} · ${maskSecret(credential.api_key, true)}`
                    : storedCredential === null ? "暂不配置" : `保留现有凭据：${storedCredential}`}
                </dd>
              </div>
              <div><dt>自动翻译</dt><dd>{automaticTranslation ? "开启" : "关闭"}</dd></div>
              <div><dt>自动 AI 辅助修复</dt><dd>{automaticAiRepair ? "开启" : "关闭"}</dd></div>
            </dl>
          </section>
        ) : null}
      </div>

      {error !== null ? <p className="setup-error" role="alert">{error}</p> : null}
      <div className="setup-wizard-actions">
        <button
          className="setup-button-secondary"
          disabled={step === 0 || submitting}
          onClick={back}
          type="button"
        >
          上一步
        </button>
        {step < ONBOARDING_REVIEW_STEP ? (
          <button disabled={submitting} onClick={advance} type="button">下一步</button>
        ) : (
          <button disabled={submitting} onClick={() => void complete()} type="button">
            {submitting ? "正在保存…" : "保存并进入文库"}
          </button>
        )}
      </div>
      <p className="setup-privacy-note">API 凭据只随完成请求提交；页面仅展示脱敏结果。</p>
    </form>
  );
}
