import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { loadRuntimeCapabilities } from "../runtime-capabilities.server";
import { SetupOwnerForm } from "./setup-owner-form";
import { loadSetupStatus } from "./setup-status.server";

export const metadata: Metadata = { title: "首次配置" };
export const dynamic = "force-dynamic";

const PREPARING_LABELS = {
  NEW: "正在初始化服务",
  MIGRATING: "正在升级现有数据",
} as const;

export default async function SetupPage() {
  const [capabilities, status] = await Promise.all([
    loadRuntimeCapabilities(),
    loadSetupStatus(),
  ]);
  if (status?.setup_state === "READY" || capabilities?.setup_state === "READY") {
    redirect("/library");
  }

  const setupState = status?.setup_state ?? capabilities?.setup_state ?? null;
  const wizardVisible = status !== null && (
    setupState === "AWAITING_OWNER"
    || setupState === "CONFIGURING"
    || setupState === "VERIFYING_RUNTIME"
  );

  return (
    <main className="setup-page">
      <section className="setup-card" aria-labelledby="setup-page-heading">
        <header className="setup-page-header">
          <p className="eyebrow">Server 首次配置</p>
          <h1 id="setup-page-heading">配置 AcadeMate Paper</h1>
          <p className="setup-intro">
            设置登录密码和工作区。你可以用同一密码在电脑、平板和手机上打开文库。
          </p>
        </header>

        {wizardVisible && status !== null ? <SetupOwnerForm initialStatus={status} /> : null}

        {setupState === "NEW" || setupState === "MIGRATING" ? (
          <section className="setup-preparing" aria-live="polite">
            <div className="setup-progress-indicator" aria-hidden="true"><span /></div>
            <h2>{PREPARING_LABELS[setupState]}</h2>
            <p>服务正在准备数据库和持久化目录。完成后，这个页面会进入分步配置向导。</p>
          </section>
        ) : null}

        {setupState === "RECOVERABLE_FAILURE" ? (
          <section className="setup-recovery" role="alert">
            <h2>运行环境检查需要处理</h2>
            <p>
              已保存的配置和用户数据保持不变。请查看 API 与 worker 日志，修复问题后重启服务；
              不要重置数据库或重新导入论文。
            </p>
          </section>
        ) : null}

        {setupState === "BLOCKED" ? (
          <section className="setup-recovery" role="alert">
            <h2>配置已暂停以保护数据</h2>
            <p>请先备份数据库与存储卷，再根据服务日志进行恢复。向导不会伪造成功状态。</p>
          </section>
        ) : null}

        {setupState === null || status === null ? (
          <p className="setup-error" role="alert">
            暂时无法读取可信的安装状态。配置写入保持关闭，请等待服务恢复后刷新页面。
          </p>
        ) : null}
      </section>
    </main>
  );
}
