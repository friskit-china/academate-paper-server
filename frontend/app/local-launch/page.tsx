import type { Metadata } from "next";

import { LocalLaunchExchange } from "./local-launch-exchange";

export const metadata: Metadata = { title: "正在打开 AcadeMate Paper" };

export default function LocalLaunchPage() {
  return <LocalLaunchExchange />;
}
