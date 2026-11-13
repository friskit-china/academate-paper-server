import { proxyAutomaticOwnerSessionRequest } from "../../v1/runtime-bff";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * Deliberately separate from the general /api/v1 bridge. The helper accepts only this exact
 * canonical same-origin POST before it attaches the server-only internal service credential.
 */
export async function POST(request: Request): Promise<Response> {
  return proxyAutomaticOwnerSessionRequest(request);
}
