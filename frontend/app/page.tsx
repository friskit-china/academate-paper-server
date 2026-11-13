import { redirect } from "next/navigation";

import { loadRuntimeCapabilities } from "./runtime-capabilities.server";

export default async function HomePage() {
  const capabilities = await loadRuntimeCapabilities();
  if (capabilities !== null && capabilities.setup_state !== "READY") {
    redirect("/setup");
  }
  redirect("/library");
}
