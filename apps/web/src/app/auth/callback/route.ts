import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { safeNext, serverClient, supabaseEnv } from "@/lib/supabase";

export const dynamic = "force-dynamic";

/** Google sends the visitor back here via Supabase; swap the one-time code for a session. */
export async function GET(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const env = supabaseEnv();
  const target = new URL(safeNext(url.searchParams.get("next")), url.origin);
  if (!env || !code) return NextResponse.redirect(new URL("/login?error=missing+code", url.origin));
  const store = await cookies();
  const supabase = serverClient(env, {
    getAll: () => store.getAll(),
    setAll: (items) => {
      for (const { name, value, options } of items) store.set(name, value, options);
    },
  });
  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) {
    const login = new URL("/login", url.origin);
    login.searchParams.set("error", error.message);
    return NextResponse.redirect(login);
  }
  return NextResponse.redirect(target);
}
