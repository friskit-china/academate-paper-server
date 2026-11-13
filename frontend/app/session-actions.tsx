"use client";

import { useEffect, useState } from "react";

import { ApiRequestError, getWebSessionStatus, logoutWebSession } from "./api";
import { useRuntimeCapabilities } from "./runtime-capability-provider";

export function SessionActions() {
  const capabilities = useRuntimeCapabilities();
  const [authenticated, setAuthenticated] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [failed, setFailed] = useState(false);
  const enabled = capabilities?.setup_state === "READY"
    && capabilities.identity_mode === "AUTHENTICATED"
    && capabilities.capabilities.auth_ui
    && !capabilities.capabilities.automatic_owner_session;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void getWebSessionStatus()
      .then(() => {
        if (!cancelled) setAuthenticated(true);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setAuthenticated(false);
        if (
          error instanceof ApiRequestError
          && error.status === 401
          && window.location.pathname !== "/login"
          && window.location.pathname !== "/register"
          && window.location.pathname !== "/setup"
        ) {
          window.location.replace("/login");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  if (!enabled || !authenticated) return null;

  async function logout() {
    if (submitting) return;
    setSubmitting(true);
    setFailed(false);
    try {
      await logoutWebSession();
      window.location.assign("/login");
    } catch {
      setFailed(true);
      setSubmitting(false);
    }
  }

  return (
    <div className="session-actions">
      {failed ? <span role="alert">退出失败，请重试</span> : null}
      <button disabled={submitting} onClick={() => void logout()} type="button">
        {submitting ? "正在退出…" : "退出登录"}
      </button>
    </div>
  );
}
