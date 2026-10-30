import { cookies } from "next/headers";

import { forward, proxyConfigFromEnv } from "@/lib/proxy";
import { serverClient, supabaseEnv } from "@/lib/supabase";

export const dynamic = "force-dynamic";
// A sleeping free-tier API takes a while to wake; allow the proxied request to wait for it
// (the default function limit on Vercel's free plan is 10 s, the maximum is 60 s).
export const maxDuration = 60;

type Context = { params: Promise<{ path: string[] }> };

/** The signed-in user's access token from the session cookie (refreshed if it expired). */
async function accessToken(): Promise<string | undefined> {
  const env = supabaseEnv();
  if (!env) return undefined;
  const store = await cookies();
  const supabase = serverClient(env, {
    getAll: () => store.getAll(),
    setAll: (items) => {
      for (const { name, value, options } of items) store.set(name, value, options);
    },
  });
  // The API verifies the token itself, so reading it from the cookie is enough here.
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token;
}

async function handle(request: Request, context: Context): Promise<Response> {
  const { path } = await context.params;
  const config = proxyConfigFromEnv();
  if (supabaseEnv()) {
    config.userMode = true;
    config.accessToken = await accessToken();
  }
  return forward(request, path, config);
}

export { handle as GET, handle as POST, handle as DELETE };
