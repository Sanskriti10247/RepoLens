import { NextResponse, type NextRequest } from "next/server";

import { safeNext, serverClient, supabaseEnv } from "@/lib/supabase";

// Reachable without a session. `/api` answers 401 itself, as JSON, rather than redirecting.
const PUBLIC_PREFIXES = ["/login", "/auth/callback", "/api/"];

/** Refresh the Supabase session cookie and send signed-out visitors to /login. */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const env = supabaseEnv();
  if (!env) return NextResponse.next();

  let response = NextResponse.next({ request });
  const supabase = serverClient(env, {
    getAll: () => request.cookies.getAll(),
    setAll: (cookies) => {
      for (const { name, value } of cookies) request.cookies.set(name, value);
      response = NextResponse.next({ request });
      for (const { name, value, options } of cookies) response.cookies.set(name, value, options);
    },
  });
  // Refreshes an expired session and verifies the token's signature, unlike getSession().
  const { data } = await supabase.auth.getClaims();
  const path = request.nextUrl.pathname;
  if (data?.claims || PUBLIC_PREFIXES.some((prefix) => path === prefix || path.startsWith(prefix))) {
    return response;
  }
  const login = request.nextUrl.clone();
  login.pathname = "/login";
  login.search = "";
  login.searchParams.set("next", safeNext(path + request.nextUrl.search));
  return NextResponse.redirect(login);
}

export const config = {
  // Everything except build output, images and files with an extension (icons, fonts).
  matcher: ["/((?!_next/static|_next/image|.*\\.[a-zA-Z0-9]+$).*)"],
};
