import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { loadRuntimeCapabilities } from "../runtime-capabilities.server";
import { LoginForm } from "./login-form";
import { SingleUserSessionEntry } from "./single-user-session-entry";

export const metadata: Metadata = { title: "登录" };
export const dynamic = "force-dynamic";

export default async function LoginPage() {
  const capabilities = await loadRuntimeCapabilities();
  if (capabilities === null) {
    return (
      <main className="login-page">
        <section className="login-card" aria-labelledby="login-heading">
          <p className="eyebrow">AcadeMate Paper Server</p>
          <h1 id="login-heading">暂时无法确认登录方式</h1>
          <p className="login-intro" role="alert">
            应用无法读取服务端运行配置。请确认服务仍在启动，然后刷新此页面；系统不会在配置未知时尝试自动登录。
          </p>
        </section>
      </main>
    );
  }
  if (capabilities.setup_state !== "READY") redirect("/setup");

  const personal = capabilities.installation_mode === "SINGLE_USER";
  const automaticOwnerSession = capabilities.capabilities.automatic_owner_session;

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-heading">
        <p className="eyebrow">AcadeMate Paper Server</p>
        <h1 id="login-heading">
          {automaticOwnerSession ? "正在进入 AcadeMate Paper" : "登录 AcadeMate Paper"}
        </h1>
        <p className="login-intro">
          {automaticOwnerSession
            ? "当前是单用户部署，服务将为唯一管理员自动建立会话。每一次数据访问仍按管理员和工作区范围校验。"
            : personal ? "输入密码进入你的文库。" : "使用你的用户名和密码登录。"}
        </p>
        {automaticOwnerSession ? <SingleUserSessionEntry /> : <LoginForm personal={personal} />}
      </section>
    </main>
  );
}
