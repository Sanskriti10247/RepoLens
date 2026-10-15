import { forward, proxyConfigFromEnv } from "@/lib/proxy";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

async function handle(request: Request, context: Context): Promise<Response> {
  const { path } = await context.params;
  return forward(request, path, proxyConfigFromEnv());
}

export { handle as GET, handle as POST, handle as DELETE };
