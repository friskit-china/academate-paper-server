"use client";

import { createContext, useContext, type ReactNode } from "react";

import type { RuntimeCapabilities } from "./runtime-capabilities";

const RuntimeCapabilityContext = createContext<RuntimeCapabilities | null | undefined>(undefined);

export function RuntimeCapabilityProvider({
  capabilities,
  children,
}: Readonly<{
  capabilities: RuntimeCapabilities | null;
  children: ReactNode;
}>) {
  return (
    <RuntimeCapabilityContext.Provider value={capabilities}>
      {children}
    </RuntimeCapabilityContext.Provider>
  );
}

export function useRuntimeCapabilities(): RuntimeCapabilities | null {
  const capabilities = useContext(RuntimeCapabilityContext);
  if (capabilities === undefined) {
    throw new Error("useRuntimeCapabilities must be used inside RuntimeCapabilityProvider.");
  }
  return capabilities;
}
