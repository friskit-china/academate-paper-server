import { proxyApiRequest } from "../runtime-bff";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type ApiRouteContext = {
  params: Promise<{ path?: string[] }>;
};

async function proxy(request: Request, context: ApiRouteContext): Promise<Response> {
  const { path = [] } = await context.params;
  return proxyApiRequest(request, path);
}

export const GET = proxy;
export const HEAD = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
