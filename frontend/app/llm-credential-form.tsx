"use client";

import { useEffect, useState } from "react";

import { storedKeyPlaceholder } from "./credential-hint";

import {
  getDefaultLlmCredential,
  replaceDefaultLlmCredential,
  type LlmCredentialMetadata,
  type LlmCredentialWrite,
} from "./api";

export const EMPTY_LLM_CREDENTIAL: LlmCredentialWrite = {
  provider: "glm",
  api_key: "",
  api_base: "https://open.bigmodel.cn/api/paas/v4",
  model: "glm-5.2",
};

type CredentialFieldsProps = {
  value: LlmCredentialWrite;
  onChange: (value: LlmCredentialWrite) => void;
  disabled?: boolean;
  keyPlaceholder?: string;
  locale?: "en" | "zh-CN";
};

const CREDENTIAL_FIELD_LABELS = {
  en: {
    provider: "Provider identifier",
    model: "Model",
    apiBase: "API base URL",
    apiCredential: "API credential",
  },
  "zh-CN": {
    provider: "服务商标识",
    model: "模型",
    apiBase: "API 基础地址",
    apiCredential: "API 凭据",
  },
} as const;

export function LlmCredentialFields({
  value,
  onChange,
  disabled,
  keyPlaceholder,
  locale = "en",
}: CredentialFieldsProps) {
  const labels = CREDENTIAL_FIELD_LABELS[locale];

  function update(field: keyof LlmCredentialWrite, nextValue: string) {
    onChange({ ...value, [field]: nextValue });
  }

  return (
    <div className="credential-fields">
      <div className="setup-form-grid">
        <label>
          <span>{labels.provider}</span>
          <input
            disabled={disabled}
            onChange={(event) => update("provider", event.target.value)}
            required
            value={value.provider}
          />
        </label>
        <label>
          <span>{labels.model}</span>
          <input
            disabled={disabled}
            onChange={(event) => update("model", event.target.value)}
            required
            value={value.model}
          />
        </label>
      </div>
      <label>
        <span>{labels.apiBase}</span>
        <input
          disabled={disabled}
          inputMode="url"
          onChange={(event) => update("api_base", event.target.value)}
          required
          type="url"
          value={value.api_base}
        />
      </label>
      <label>
        <span>{labels.apiCredential}</span>
        <input
          autoComplete="new-password"
          disabled={disabled}
          minLength={8}
          onChange={(event) => update("api_key", event.target.value)}
          placeholder={keyPlaceholder}
          required
          type="password"
          value={value.api_key}
        />
      </label>
    </div>
  );
}

export function ProtectedLlmCredentialSettings({
  onSaved,
}: {
  onSaved?: (metadata: LlmCredentialMetadata) => void;
}) {
  const [storageMode, setStorageMode] = useState<"protected" | "environment" | null>(null);
  const [metadata, setMetadata] = useState<LlmCredentialMetadata | null>(null);
  const [draft, setDraft] = useState<LlmCredentialWrite>({ ...EMPTY_LLM_CREDENTIAL });
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void getDefaultLlmCredential()
      .then((result) => {
        setStorageMode(result.storage_mode);
        setMetadata(result.credential);
        if (result.credential !== null) {
          const credential = result.credential;
          setDraft((current) => ({
            ...current,
            provider: credential.provider,
            api_base: credential.api_base ?? current.api_base,
            model: credential.model ?? current.model,
          }));
        }
      })
      .catch((reason) => {
        setMessage(reason instanceof Error ? reason.message : "Credential status unavailable.");
      });
  }, []);

  async function save() {
    if (draft.api_key.trim().length < 8) return;
    setSaving(true);
    setMessage(null);
    try {
      const result = await replaceDefaultLlmCredential(draft);
      setMetadata(result);
      setDraft((current) => ({ ...current, api_key: "" }));
      onSaved?.(result);
      setMessage("个人凭据已保存，新任务将使用此配置。");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Unable to save the credential.");
    } finally {
      setSaving(false);
    }
  }

  if (storageMode === "environment") {
    return (
      <section className="settings-card credential-settings-card">
        <strong>个人 LLM 凭据</strong>
        <p>当前服务使用预配置的受保护凭据；浏览器不会读取密钥明文。</p>
      </section>
    );
  }

  return (
    <section className="settings-card credential-settings-card">
      <div className="prompt-settings-heading">
        <div>
          <strong>个人 LLM 凭据</strong>
          <p>
            {metadata === null
              ? "添加个人凭据后可使用翻译、问答、论文地图和辅助修复。"
              : "输入新密钥并保存，即可替换已保存的凭据。"}
          </p>
        </div>
        <small>{metadata?.requires_reentry ? "需重新输入密钥" : metadata?.configured ? "已配置" : "未配置"}</small>
      </div>
      {metadata?.requires_reentry ? (
        <p role="alert">现有密钥暂时无法读取，请在本机重新输入并保存；已有论文和译文会保留。</p>
      ) : null}
      <p>翻译、问答和论文地图支持 GLM 与 DeepSeek。请填写对应服务的 API 地址和模型。</p>
      <LlmCredentialFields
        disabled={saving || storageMode === null}
        keyPlaceholder={storedKeyPlaceholder(metadata)}
        onChange={setDraft}
        value={draft}
      />
      <div className="prompt-settings-actions">
        <button
          disabled={saving || storageMode === null || draft.api_key.trim().length < 8}
          onClick={() => void save()}
          type="button"
        >
          {saving ? "正在保存…" : metadata === null ? "保存个人凭据" : "替换个人凭据"}
        </button>
        {message !== null ? <span role="status">{message}</span> : null}
      </div>
    </section>
  );
}
