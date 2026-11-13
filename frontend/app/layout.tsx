import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "katex/dist/katex.min.css";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import { BilingualModeToggle } from "./bilingual-mode-toggle";
import { CapabilityNavigation, RegisteredRoute } from "./capability-ui";
import { RuntimeCapabilityProvider } from "./runtime-capability-provider";
import { loadRuntimeCapabilities } from "./runtime-capabilities.server";
import { SessionActions } from "./session-actions";
import { SessionKeepAlive } from "./session-keep-alive";

export const metadata: Metadata = {
  title: {
    default: "AcadeMate Paper",
    template: "%s · AcadeMate Paper",
  },
  description: "A private-first bilingual research paper reading workspace.",
};

// Runtime capabilities are installation state, not build input. Without this boundary Next can
// prerender READY routes while the image is built (when the private API is intentionally absent)
// and permanently bake the fail-closed `null` result into library/settings HTML.
export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  const runtimeCapabilities = await loadRuntimeCapabilities();
  const homeHref = runtimeCapabilities?.setup_state === "READY" ? "/library" : "/setup";

  return (
    <html lang="zh-CN">
      <body>
        <RuntimeCapabilityProvider capabilities={runtimeCapabilities}>
          <SessionKeepAlive />
          <div className="app-shell">
            <header className="topbar">
              <Link className="brand" href={homeHref} aria-label="AcadeMate Paper 首页">
                <span className="brand-mark" aria-hidden="true">A</span>
                <span>
                  <strong>AcadeMate Paper</strong>
                  <small>Research reading workspace</small>
                </span>
              </Link>
              <div className="topbar-actions">
                <BilingualModeToggle />
                <CapabilityNavigation />
                <SessionActions />
              </div>
            </header>
            <main>
              <RegisteredRoute>{children}</RegisteredRoute>
            </main>
          </div>
        </RuntimeCapabilityProvider>
      </body>
    </html>
  );
}
