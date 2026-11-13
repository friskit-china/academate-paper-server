"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  ApiRequestError,
  getSetupStatus,
  setupLocalOwner,
  type SetupState,
} from "../api";
import {
  EMPTY_LLM_CREDENTIAL,
  LlmCredentialFields,
} from "../llm-credential-form";
import { consumeSetupTokenFragment } from "./setup-token";
import type { SetupStatus } from "./setup-status";
import {
  maskSecret,
  PROGRESS_STEP,
  REVIEW_STEP,
  SETUP_STEPS,
  type SetupWizardDraft,
  validateSetupStep,
} from "./setup-wizard-state";

const SETUP_STATE_LABELS: Record<SetupState, string> = {
  NEW: "正在初始化服务",
  MIGRATING: "正在升级数据",
  AWAITING_OWNER: "等待提交配置",
  CONFIGURING: "正在保存配置",
  VERIFYING_RUNTIME: "正在验证运行环境",
  READY: "配置完成",
  RECOVERABLE_FAILURE: "配置检查需要重试",
  BLOCKED: "配置已暂停以保护数据",
};

function setupSubmitError(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return "暂时无法保存配置，请稍后重试。";
  if (error.status === 401) return "配置凭证无效，请按安装指南重新获取首次配置链接。";
  if (error.status === 410) return "配置凭证已过期，请按安装指南重新获取首次配置链接。";
  if (error.status === 409) {
    return "配置状态已经变化，请刷新页面确认后再试。";
  }
  if (error.status === 423) return "服务已关闭首次配置写入，请刷新页面查看当前状态。";
  if (error.status === 400 || error.status === 422) {
    return "配置内容未通过服务端校验，请检查填写内容后重试。";
  }
  if (error.status === 503) return "服务仍在准备，或凭据存储暂不可用，请稍后重试。";
  return "服务器未能保存配置，请稍后重试。";
}

function initialWizardStep(status: SetupStatus): number {
  return status.setup_state === "CONFIGURING" || status.setup_state === "VERIFYING_RUNTIME"
    ? PROGRESS_STEP
    : 0;
}

export function SetupOwnerForm({ initialStatus }: { initialStatus: SetupStatus }) {
  const proofConsumed = useRef(false);
  const submitInFlight = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [step, setStep] = useState(() => initialWizardStep(initialStatus));
  const [bootstrapToken, setBootstrapToken] = useState("");
  const [proofFromFragment, setProofFromFragment] = useState(false);
  const [password, setPassword] = useState("");
  const [passwordConfirmation, setPasswordConfirmation] = useState("");
  const [workspaceName, setWorkspaceName] = useState("我的文库");
  const [configureLlm, setConfigureLlm] = useState(false);
  const [llmCredential, setLlmCredential] = useState({ ...EMPTY_LLM_CREDENTIAL });
  const [submitting, setSubmitting] = useState(false);
  const [configurationSubmitted, setConfigurationSubmitted] = useState(
    step === PROGRESS_STEP,
  );
  const [progressState, setProgressState] = useState<SetupState>(initialStatus.setup_state);
  const [error, setError] = useState<string | null>(null);
  const [pollingMessage, setPollingMessage] = useState<string | null>(null);

  const draft: SetupWizardDraft = {
    bootstrapToken,
    password,
    passwordConfirmation,
    workspaceName,
    configureLlm,
    llmCredential,
  };

  useEffect(() => {
    if (proofConsumed.current) return;
    proofConsumed.current = true;
    const token = consumeSetupTokenFragment(window.location, window.history);
    if (token !== null) {
      setBootstrapToken(token);
      setProofFromFragment(true);
    }
  }, []);

  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  useEffect(() => {
    if (!configurationSubmitted || step !== PROGRESS_STEP) return;
    if (["READY", "RECOVERABLE_FAILURE", "BLOCKED"].includes(progressState)) return;
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    async function refreshStatus() {
      try {
        const status = await getSetupStatus();
        if (cancelled) return;
        setPollingMessage(null);
        setProgressState(status.setup_state);
        if (status.setup_state === "READY") {
          window.location.assign("/login?setup=complete");
          return;
        }
        if (status.setup_state === "RECOVERABLE_FAILURE" || status.setup_state === "BLOCKED") {
          return;
        }
      } catch {
        if (cancelled) return;
        setPollingMessage("暂时无法读取最新状态，向导会自动重试。请不要重复提交配置。");
      }
      retryTimer = setTimeout(() => void refreshStatus(), 1500);
    }

    retryTimer = setTimeout(() => void refreshStatus(), 700);
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) clearTimeout(retryTimer);
    };
  }, [configurationSubmitted, progressState, step]);

  function advance() {
    const validationError = validateSetupStep(step, draft);
    if (validationError !== null) {
      setError(validationError);
      return;
    }
    setError(null);
    setStep((current) => Math.min(current + 1, REVIEW_STEP));
  }

  function back() {
    setError(null);
    setStep((current) => Math.max(0, current - 1));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (step !== REVIEW_STEP) {
      advance();
      return;
    }
    for (let candidate = 0; candidate < REVIEW_STEP; candidate += 1) {
      const validationError = validateSetupStep(candidate, draft);
      if (validationError !== null) {
        setStep(candidate);
        setError(validationError);
        return;
      }
    }
    if (submitInFlight.current) return;

    submitInFlight.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const response = await setupLocalOwner({
        bootstrap_token: bootstrapToken,
        installation_mode: "SINGLE_USER",
        workspace_name: workspaceName.trim(),
        expected_state_version: initialStatus.state_version,
        password,
        ...(configureLlm ? { llm_credential: llmCredential } : {}),
      });
      setConfigurationSubmitted(true);
      setProgressState(response.setup_state);
      setStep(PROGRESS_STEP);
      setBootstrapToken("");
      setPassword("");
      setPasswordConfirmation("");
      setLlmCredential((current) => ({ ...current, api_key: "" }));
      if (response.setup_state === "READY") {
        window.location.assign("/login?setup=complete");
      }
    } catch (caught) {
      setError(setupSubmitError(caught));
      submitInFlight.current = false;
    } finally {
      setSubmitting(false);
    }
  }

  const currentStepName = SETUP_STEPS[step];

  return (
    <form className="setup-form setup-wizard" onSubmit={submit}>
      <nav className="setup-stepper" aria-label="配置步骤">
        <p aria-live="polite">步骤 {step + 1} / {SETUP_STEPS.length}：{currentStepName}</p>
        <ol>
          {SETUP_STEPS.map((label, index) => (
            <li
              aria-current={index === step ? "step" : undefined}
              data-state={index < step ? "complete" : index === step ? "current" : "upcoming"}
              key={label}
            >
              <span aria-hidden="true">{index + 1}</span>
              <small>{label}</small>
            </li>
          ))}
        </ol>
      </nav>

      <div className="setup-wizard-panel">
        {step === 0 ? (
          <section aria-labelledby="setup-welcome-heading">
            <p className="eyebrow">首次启动配置</p>
            <h2 id="setup-welcome-heading" ref={headingRef} tabIndex={-1}>欢迎使用 AcadeMate Paper</h2>
            <p>设置登录密码、文库和可选的模型凭据后，即可在多个设备上登录同一文库。</p>
            <label>
              <span>首次配置凭证</span>
              <input
                aria-describedby="setup-token-help"
                autoComplete="off"
                maxLength={512}
                minLength={32}
                name="bootstrap-token"
                onChange={(event) => {
                  setBootstrapToken(event.target.value);
                  setProofFromFragment(false);
                }}
                required
                type="password"
                value={bootstrapToken}
              />
            </label>
            <p className="setup-field-help" id="setup-token-help">
              {proofFromFragment
                ? "已从首次配置链接读取凭证。"
                : "请从安装程序提供的首次配置链接取得一次性凭证。"}
            </p>
          </section>
        ) : null}

        {step === 1 ? (
          <section aria-labelledby="setup-admin-heading">
            <p className="eyebrow">登录密码</p>
            <h2 id="setup-admin-heading" ref={headingRef} tabIndex={-1}>设置登录密码</h2>
            <p>在其他设备上连接这台 Server 时，使用同一个密码登录。</p>
            <div className="setup-form-grid">
              <label>
                <span>密码</span>
                <input
                  autoComplete="new-password"
                  maxLength={1024}
                  minLength={12}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                  type="password"
                  value={password}
                />
              </label>
              <label>
                <span>再次输入密码</span>
                <input
                  autoComplete="new-password"
                  maxLength={1024}
                  minLength={12}
                  onChange={(event) => setPasswordConfirmation(event.target.value)}
                  required
                  type="password"
                  value={passwordConfirmation}
                />
              </label>
            </div>
            <p className="setup-field-help">密码至少需要 12 个字符。服务只保存安全摘要。</p>
          </section>
        ) : null}

        {step === 2 ? (
          <section aria-labelledby="setup-workspace-heading">
            <p className="eyebrow">工作区与模型</p>
            <h2 id="setup-workspace-heading" ref={headingRef} tabIndex={-1}>设置个人工作区</h2>
            <label>
              <span>工作区名称</span>
              <input
                autoComplete="off"
                maxLength={120}
                onChange={(event) => setWorkspaceName(event.target.value)}
                required
                value={workspaceName}
              />
            </label>
            <label className="setup-optional-toggle">
              <input
                checked={configureLlm}
                onChange={(event) => setConfigureLlm(event.target.checked)}
                type="checkbox"
              />
              <span>
                <strong>现在配置 LLM 凭据（可选）</strong>
                <small>跳过后仍可导入和阅读论文，也可以稍后在设置中添加。</small>
              </span>
            </label>
            {configureLlm ? (
              <fieldset className="setup-credential-fieldset">
                <legend>个人模型凭据</legend>
                <p>凭据会随最终配置一次提交；浏览器之后只会收到脱敏元数据。</p>
                <LlmCredentialFields
                  disabled={submitting}
                  locale="zh-CN"
                  onChange={setLlmCredential}
                  value={llmCredential}
                />
              </fieldset>
            ) : null}
          </section>
        ) : null}

        {step === REVIEW_STEP ? (
          <section aria-labelledby="setup-review-heading">
            <p className="eyebrow">确认配置</p>
            <h2 id="setup-review-heading" ref={headingRef} tabIndex={-1}>请检查后再保存</h2>
            <p>点击“保存配置并开始使用”前，服务端不会收到下面的设置。</p>
            <dl className="setup-review-list">
              <div><dt>密码</dt><dd>{maskSecret(password)}</dd></div>
              <div><dt>工作区</dt><dd>{workspaceName.trim()}</dd></div>
              <div><dt>首次配置凭证</dt><dd>{maskSecret(bootstrapToken)}</dd></div>
              <div>
                <dt>LLM 凭据</dt>
                <dd>
                  {configureLlm
                    ? `${llmCredential.provider} / ${llmCredential.model} / ${llmCredential.api_base} / ${maskSecret(llmCredential.api_key, true)}`
                    : "暂不配置"}
                </dd>
              </div>
            </dl>
          </section>
        ) : null}

        {step === PROGRESS_STEP ? (
          <section aria-labelledby="setup-progress-heading" aria-live="polite">
            <p className="eyebrow">正在应用配置</p>
            <h2 id="setup-progress-heading" ref={headingRef} tabIndex={-1}>
              {SETUP_STATE_LABELS[progressState]}
            </h2>
            <div className="setup-progress-indicator" data-state={progressState} aria-hidden="true">
              <span />
            </div>
            {progressState === "CONFIGURING" ? (
              <p>账号与工作区已提交，正在保存配置。</p>
            ) : null}
            {progressState === "VERIFYING_RUNTIME" ? (
              <p>配置已经写入，正在验证数据库、任务进程以及论文处理工具链。</p>
            ) : null}
            {progressState === "READY" ? <p>服务已就绪，正在进入登录页面。</p> : null}
            {progressState === "RECOVERABLE_FAILURE" ? (
              <p className="setup-error" role="alert">
                运行环境检查未通过。数据和已保存配置保持不变，请检查服务日志后重启服务重试。
              </p>
            ) : null}
            {progressState === "BLOCKED" ? (
              <p className="setup-error" role="alert">
                为保护现有数据，服务暂停了配置。请先备份数据库和存储卷，再根据服务日志恢复。
              </p>
            ) : null}
            {pollingMessage !== null ? <p className="setup-message" role="status">{pollingMessage}</p> : null}
          </section>
        ) : null}
      </div>

      {error !== null ? <p className="setup-error" role="alert">{error}</p> : null}

      {step < PROGRESS_STEP ? (
        <div className="setup-wizard-actions">
          <button className="setup-button-secondary" disabled={step === 0 || submitting} onClick={back} type="button">
            上一步
          </button>
          {step < REVIEW_STEP ? (
            <button disabled={submitting} onClick={advance} type="button">下一步</button>
          ) : (
            <button disabled={submitting} type="submit">
              {submitting ? "正在提交配置…" : "保存配置并开始使用"}
            </button>
          )}
        </div>
      ) : null}

      <p className="setup-privacy-note">
        密码、首次配置凭证与 API 凭据不会写入浏览器存储，也不会出现在配置摘要中。
      </p>
    </form>
  );
}
