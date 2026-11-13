"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

import { getWebSessionStatus } from "./api";
import {
  getRegisteredNavigation,
  isRouteRegistered,
} from "./runtime-capabilities";
import { useRuntimeCapabilities } from "./runtime-capability-provider";

export function CapabilityNavigation() {
  const capabilities = useRuntimeCapabilities();
  const [canManageAdministration, setCanManageAdministration] = useState(false);
  const navigation = getRegisteredNavigation(capabilities);
  const shouldResolveAdministration = capabilities?.identity_mode === "AUTHENTICATED"
    && (
      capabilities.capabilities.user_management
      || capabilities.capabilities.invitations
    );

  useEffect(() => {
    if (!shouldResolveAdministration) return;
    let cancelled = false;
    void getWebSessionStatus()
      .then((status) => {
        if (!cancelled) {
          setCanManageAdministration(
            status.can_manage_users || status.can_manage_invitations,
          );
        }
      })
      .catch(() => {
        if (!cancelled) setCanManageAdministration(false);
      });
    return () => {
      cancelled = true;
    };
  }, [shouldResolveAdministration]);

  if (navigation.length === 0) return null;

  return (
    <nav aria-label="主要导航">
      {navigation.filter((item) => item.id !== "users" || canManageAdministration).map((item) => (
        <Link href={item.href} key={item.id}>{item.label}</Link>
      ))}
    </nav>
  );
}

export function RegisteredRoute({ children }: Readonly<{ children: ReactNode }>) {
  const capabilities = useRuntimeCapabilities();
  const pathname = usePathname();
  const isRecoveryRoute = pathname === "/setup"
    || pathname === "/local-launch"
    || pathname === "/login"
    || pathname === "/register"
    || pathname === "/onboarding";

  if (capabilities === null) {
    return (
      <section className="settings-card" role="alert">
        <h1>{isRecoveryRoute ? "暂时无法读取运行配置" : "Runtime capabilities unavailable"}</h1>
        <p>
          {isRecoveryRoute
            ? "应用无法验证服务端运行配置。请确认服务仍在启动，然后刷新页面。"
            : "The application could not verify its server-issued runtime profile."}
        </p>
      </section>
    );
  }

  if (!isRouteRegistered(capabilities, pathname)) {
    return (
      <section className="settings-card" role="alert">
        <h1>{isRecoveryRoute ? "当前无法打开配置或登录页面" : "Route unavailable"}</h1>
        <p>
          {isRecoveryRoute
            ? "当前版本或安装状态尚未启用此页面。"
            : "This route is not registered for the current edition."}
        </p>
      </section>
    );
  }

  return children;
}
