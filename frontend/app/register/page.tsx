import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { loadRuntimeCapabilities } from "../runtime-capabilities.server";
import { RegistrationForm } from "./registration-form";

export const metadata: Metadata = { title: "接受邀请" };
export const dynamic = "force-dynamic";

export default async function RegisterPage() {
  const capabilities = await loadRuntimeCapabilities();
  if (capabilities === null) {
    return (
      <main className="login-page">
        <section className="login-card" role="alert">
          <h1>暂时无法确认注册状态</h1>
          <p>服务端运行配置不可用。注册保持关闭，请稍后刷新页面。</p>
        </section>
      </main>
    );
  }
  if (capabilities.setup_state !== "READY") redirect("/setup");
  if (
    capabilities.edition !== "SERVER"
    || capabilities.identity_mode !== "AUTHENTICATED"
    || capabilities.installation_mode !== "MULTI_USER"
    || !capabilities.capabilities.registration
  ) redirect("/login");

  return (
    <main className="login-page registration-page">
      <section className="login-card" aria-labelledby="registration-heading">
        <p className="eyebrow">多用户部署 · 成员邀请</p>
        <h1 id="registration-heading">创建你的账号</h1>
        <p className="login-intro">
          账号会加入邀请所绑定的文库。你无需选择工作区、角色或权限。
        </p>
        <RegistrationForm />
      </section>
    </main>
  );
}
