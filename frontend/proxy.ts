import { type NextRequest, NextResponse } from "next/server";

import { guardAuthenticatedNavigation } from "./app/navigation-session-guard";

/**
 * This is only a navigation guard. FastAPI verifies the opaque token here and still derives every
 * RequestPrincipal independently for the API request that follows.
 */
export async function proxy(request: NextRequest): Promise<Response> {
  return (await guardAuthenticatedNavigation(request)) ?? NextResponse.next();
}

export const config = {
  matcher: [
    "/library/:path*",
    "/papers/:path*",
    "/arxiv/:path*",
    "/settings/:path*",
    "/users/:path*",
    "/onboarding/:path*",
    "/developer-docs/:path*",
  ],
};
