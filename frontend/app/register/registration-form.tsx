"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import { ApiRequestError, registerWebSession } from "../api";
import { consumeInvitationTokenFragment } from "./invite-token";
import { validateRegistrationDraft } from "./registration-state";

function registrationError(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return "暂时无法连接注册服务，请稍后重试。";
  if (error.status === 401) return "这个邀请链接无法使用，请联系管理员获取新的邀请链接。";
  if (error.status === 403 || error.status === 423) {
    return "当前安装未开放邀请注册，请联系管理员确认服务状态。";
  }
  if (error.status === 409) return "这个用户名不可用，请更换用户名后重试。";
  if (error.status === 422) return "注册信息未通过校验，请检查用户名、显示名称和密码。";
  if (error.status === 429) return "尝试次数过多，请稍后再试。";
  return "注册未能安全完成，请稍后重试或联系管理员。";
}

export function RegistrationForm() {
  const fragmentConsumed = useRef(false);
  const submitInFlight = useRef(false);
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  const [fragmentReady, setFragmentReady] = useState(false);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [passwordConfirmation, setPasswordConfirmation] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (fragmentConsumed.current) return;
    fragmentConsumed.current = true;
    setInviteToken(consumeInvitationTokenFragment(window.location, window.history));
    setFragmentReady(true);
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitInFlight.current || inviteToken === null) return;
    const validationError = validateRegistrationDraft({
      username,
      displayName,
      password,
      passwordConfirmation,
    });
    if (validationError !== null) {
      setError(validationError);
      return;
    }

    submitInFlight.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await registerWebSession({
        invite_token: inviteToken,
        username: username.trim(),
        display_name: displayName.trim(),
        password,
      });
      setInviteToken(null);
      setPassword("");
      setPasswordConfirmation("");
      window.location.replace("/onboarding");
    } catch (caught) {
      setError(registrationError(caught));
      submitInFlight.current = false;
      setSubmitting(false);
    }
  }

  if (!fragmentReady) {
    return <p className="setup-message" role="status">正在读取并清除一次性邀请凭证…</p>;
  }
  if (inviteToken === null) {
    return (
      <div className="registration-unavailable" role="alert">
        <h2>邀请链接不可用</h2>
        <p>请从管理员发送的完整注册链接重新打开；邀请凭证不会保存在地址栏或浏览器存储中。</p>
      </div>
    );
  }

  return (
    <form className="registration-form" onSubmit={submit}>
      <div className="setup-form-grid">
        <label>
          <span>用户名</span>
          <input
            autoComplete="username"
            disabled={submitting}
            maxLength={64}
            minLength={3}
            onChange={(event) => setUsername(event.target.value)}
            required
            value={username}
          />
        </label>
        <label>
          <span>显示名称</span>
          <input
            autoComplete="name"
            disabled={submitting}
            maxLength={128}
            onChange={(event) => setDisplayName(event.target.value)}
            required
            value={displayName}
          />
        </label>
      </div>
      <div className="setup-form-grid">
        <label>
          <span>密码</span>
          <input
            autoComplete="new-password"
            disabled={submitting}
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
            disabled={submitting}
            maxLength={1024}
            minLength={12}
            onChange={(event) => setPasswordConfirmation(event.target.value)}
            required
            type="password"
            value={passwordConfirmation}
          />
        </label>
      </div>
      <p className="setup-field-help">密码至少 12 个字符。注册后会直接进入你的个人设置向导。</p>
      {error !== null ? <p className="setup-error" role="alert">{error}</p> : null}
      <button disabled={submitting} type="submit">
        {submitting ? "正在注册…" : "注册并继续"}
      </button>
      <p className="setup-privacy-note">
        邀请凭证和密码只用于本次注册请求，不会写入浏览器持久存储或页面日志。
      </p>
    </form>
  );
}
