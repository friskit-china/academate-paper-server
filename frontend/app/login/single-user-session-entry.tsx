"use client";

import { useEffect, useRef, useState } from "react";

import { ApiRequestError } from "../api";
import { establishAutomaticOwnerSession } from "./automatic-owner-session";
import { LoginForm } from "./login-form";

type EntryFailure = Readonly<{
  message: string;
  passwordFallback: boolean;
}>;

function entryFailure(error: unknown): EntryFailure {
  if (!(error instanceof ApiRequestError)) {
    return { message: "暂时无法连接认证服务，请稍后重试。", passwordFallback: false };
  }
  if (error.status === 403 || error.status === 409) {
    return {
      message: "当前部署未允许自动登录，请使用首次配置时设置的管理员密码。",
      passwordFallback: true,
    };
  }
  if (error.status === 423) {
    return { message: "首次配置尚未完成，请稍后重试。", passwordFallback: false };
  }
  if (error.status === 429) {
    return { message: "尝试次数过多，请稍后重试。", passwordFallback: false };
  }
  if (error.status === 502 || error.status === 503) {
    return { message: "认证服务仍在启动，请稍后重试。", passwordFallback: false };
  }
  return { message: "无法建立单用户会话，请稍后重试。", passwordFallback: false };
}

export function SingleUserSessionEntry() {
  const attemptRef = useRef<ReturnType<typeof establishAutomaticOwnerSession> | null>(null);
  const [attemptNumber, setAttemptNumber] = useState(0);
  const [failure, setFailure] = useState<EntryFailure | null>(null);

  useEffect(() => {
    let active = true;
    // React development Strict Mode can start an effect twice. Both effect instances observe the
    // same promise, so only one passwordless session POST can be made for this mounted entry.
    const attempt = attemptRef.current ?? establishAutomaticOwnerSession();
    attemptRef.current = attempt;
    void attempt
      .then((outcome) => {
        if (!active) return;
        if (outcome === "authenticated") {
          window.location.replace("/library");
          return;
        }
        setFailure({
          message: "浏览器没有接受登录 Cookie。请允许本站使用 Cookie，然后手动重试。",
          passwordFallback: false,
        });
      })
      .catch((error: unknown) => {
        if (active) setFailure(entryFailure(error));
      });
    return () => {
      active = false;
    };
  }, [attemptNumber]);

  function retry() {
    attemptRef.current = null;
    setFailure(null);
    setAttemptNumber((current) => current + 1);
  }

  if (failure?.passwordFallback) {
    return (
      <>
        <p className="login-error" role="alert">{failure.message}</p>
        <LoginForm personal />
      </>
    );
  }

  return (
    <div className="login-form" aria-live="polite">
      {failure === null ? (
        <p>正在为本机唯一管理员建立安全会话…</p>
      ) : (
        <>
          <p className="login-error" role="alert">{failure.message}</p>
          <button onClick={retry} type="button">重试</button>
        </>
      )}
      <p className="login-security-note">
        自动登录只在服务端明确允许的单用户部署中启用；数据访问仍会校验管理员与工作区范围。
      </p>
    </div>
  );
}
