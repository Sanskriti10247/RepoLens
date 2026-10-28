import { forward, proxyConfigFromEnv } from "@/lib/proxy";

export const dynamic = "force-dynamic";
// A sleeping free-tier API takes a while to wake; allow the proxied request to wait for it
// (the default function limit on Vercel's free plan is 10 s, the maximum is 60 s).
export const maxDuration = 60;

type Context = { params: Promise<{ path: string[] }> };

async function handle(request: Request, context: Context): Promise<Response> {
  const { path } = await context.params;
  return forward(request, path, proxyConfigFromEnv());
}

export { handle as GET, handle as POST, handle as DELETE };
