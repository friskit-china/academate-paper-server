"use client";

import { useEffect, useRef, useState } from "react";

import { consumeLaunchTokenFragment, exchangeLocalLaunchToken } from "./launch-token";

export function LocalLaunchExchange() {
  const exchangeStarted = useRef(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (exchangeStarted.current) return;
    exchangeStarted.current = true;
    const rawToken = consumeLaunchTokenFragment(window.location, window.history);
    if (rawToken === null) {
      setError("未收到有效的一次性浏览器启动凭证，请重新打开 AcadeMate Paper。");
      return;
    }

    void exchangeLocalLaunchToken(rawToken)
      .then(() => window.location.replace("/library"))
      .catch(() => {
        setError("无法建立本地浏览器会话，请真正退出并重新打开 AcadeMate Paper。");
      });
  }, []);

  return (
    <section className="setup-page">
      <div className="setup-card">
        <p className="eyebrow">本地版</p>
        <h1>正在打开你的文库</h1>
        {error === null ? (
          <p className="setup-message" role="status">
            正在验证一次性本地启动凭证并建立私有浏览器会话…
          </p>
        ) : (
          <p className="setup-error" role="alert">{error}</p>
        )}
        <p className="setup-privacy-note">
          启动凭证会在交换前从地址栏清除，浏览器中只保留 HttpOnly、同站点的会话 Cookie。
        </p>
      </div>
    </section>
  );
}
