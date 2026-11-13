import type { Metadata } from "next";

import { OnboardingWizard } from "./onboarding-wizard";

export const metadata: Metadata = { title: "个人设置向导" };

export default function OnboardingPage() {
  return (
    <main className="setup-page onboarding-page">
      <section className="setup-card" aria-label="个人设置向导">
        <header className="setup-page-header">
          <p className="eyebrow">PERSONAL ONBOARDING</p>
          <h1>完成个人设置</h1>
          <p className="setup-intro">只需几步确认个人资料、可选 LLM 凭据和阅读偏好。</p>
        </header>
        <OnboardingWizard />
      </section>
    </main>
  );
}
