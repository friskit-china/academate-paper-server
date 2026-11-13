"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";

import { maintainWebSession } from "./api-fetch";
import { useRuntimeCapabilities } from "./runtime-capability-provider";

// Keep native image/PDF/Range requests usable while a reader remains open. The Server still
// authenticates every request; no token or Cookie value is accessible to this component.
export function SessionKeepAlive() {
  const pathname = usePathname();
  const capabilities = useRuntimeCapabilities();
  useEffect(() => {
    const protectedPage = ["/library", "/papers", "/arxiv", "/settings", "/users", "/onboarding"]
      .some((path) => pathname === path || pathname?.startsWith(`${path}/`));
    if (!protectedPage || capabilities?.setup_state !== "READY"
        || capabilities.identity_mode !== "AUTHENTICATED") return;
    let disposed = false;
    let inFlight = false;
    const check = async () => {
      if (disposed || inFlight || document.visibilityState !== "visible") return;
      inFlight = true;
      try { await maintainWebSession(); } finally { inFlight = false; }
    };
    void check();
    const tick = window.setInterval(() => { void check(); }, 5 * 60 * 1000);
    const visible = () => { if (document.visibilityState === "visible") void check(); };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("focus", visible);
    return () => {
      disposed = true;
      window.clearInterval(tick);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("focus", visible);
    };
  }, [pathname, capabilities]);
  return null;
}
