import { requestLocalRuntimeQuit } from "./local-runtime-quit";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  return requestLocalRuntimeQuit(request);
}
