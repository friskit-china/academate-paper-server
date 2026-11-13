"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  ApiRequestError,
  getWebSessionStatus,
  loginWebSession,
} from "../api";
import { authenticatedLandingPath } from "../auth-status";

function loginErrorMessage(error: unknown, personal: boolean): string {
  if (!(error instanceof ApiRequestError)) return "暂时无法连接服务，请稍后重试。";
  if (error.status === 401) return personal ? "密码不正确。" : "用户名或密码不正确。";
  if (error.status === 423) return "首次配置尚未完成，请稍后再试。";
  if (error.status === 429) return "尝试次数过多，请稍后再试。";
  if (error.status === 502 || error.status === 503) return "服务仍在启动，请稍后再试。";
  return "登录失败，请稍后重试。";
}

export function LoginForm({ personal }: { personal: boolean }) {
  const submitInFlight = useRef(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [checkingSession, setCheckingSession] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getWebSessionStatus()
      .then((status) => {
        if (!cancelled) window.location.replace(authenticatedLandingPath(status));
      })
      .catch(() => {
        if (!cancelled) setCheckingSession(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitInFlight.current) return;
    submitInFlight.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await loginWebSession(personal ? { password } : { username: username.trim(), password });
      setPassword("");
      const status = await getWebSessionStatus();
      window.location.assign(authenticatedLandingPath(status));
    } catch (caught) {
      setError(loginErrorMessage(caught, personal));
      submitInFlight.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form className="login-form" onSubmit={submit}>
      {personal ? null : <label>
        <span>用户名</span>
        <input
          autoComplete="username"
          autoFocus
          disabled={checkingSession || submitting}
          maxLength={128}
          placeholder="admin"
          onChange={(event) => setUsername(event.target.value)}
          required
          value={username}
        />
      </label>}
      <label>
        <span>密码</span>
        <input
          autoComplete="current-password"
          autoFocus={personal}
          disabled={checkingSession || submitting}
          maxLength={1024}
          onChange={(event) => setPassword(event.target.value)}
          required
          type="password"
          value={password}
        />
      </label>
      {error !== null ? <p className="login-error" role="alert">{error}</p> : null}
      <button disabled={checkingSession || submitting} type="submit">
        {checkingSession ? "正在检查会话…" : submitting ? "正在登录…" : "登录并进入文库"}
      </button>

    </form>
  );
}
