/**
 * Sign-in through Supabase Auth. Present only when NEXT_PUBLIC_SUPABASE_URL and
 * NEXT_PUBLIC_SUPABASE_ANON_KEY are set; without them the app runs in single-operator mode.
 */
import { createBrowserClient, createServerClient, type CookieMethodsServer } from "@supabase/ssr";

export interface SupabaseEnv {
  url: string;
  anonKey: string;
}

/** Inlined at build time (NEXT_PUBLIC_*), so it is the same on the server and in the browser. */
export function supabaseEnv(): SupabaseEnv | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  return url && anonKey ? { url, anonKey } : null;
}

export function browserClient(env: SupabaseEnv) {
  return createBrowserClient(env.url, env.anonKey);
}

export function serverClient(env: SupabaseEnv, cookies: CookieMethodsServer) {
  return createServerClient(env.url, env.anonKey, { cookies });
}

/** Where to go after sign-in: a same-site path only, never another host. */
export function safeNext(value: string | null): string {
  return value && value.startsWith("/") && !value.startsWith("//") && !value.startsWith("/\\") ? value : "/";
}
