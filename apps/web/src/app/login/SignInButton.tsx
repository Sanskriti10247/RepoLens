"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { browserClient, safeNext, supabaseEnv } from "@/lib/supabase";

function Button() {
  const params = useSearchParams();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(params.get("error"));
  const env = supabaseEnv();
  if (!env) return <p className="notice">Sign-in is not configured on this instance.</p>;

  async function signIn() {
    setBusy(true);
    setError(null);
    const callback = new URL("/auth/callback", window.location.origin);
    callback.searchParams.set("next", safeNext(params.get("next")));
    const { error: failure } = await browserClient(env!).auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: callback.toString() },
    });
    if (failure) {
      setError(failure.message);
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" onClick={signIn} disabled={busy}>
        {busy ? "Opening Google…" : "Continue with Google"}
      </button>
      {error && (
        <p className="warn-text small" role="alert">
          Sign-in failed: {error}
        </p>
      )}
    </>
  );
}

export function SignInButton() {
  return (
    <Suspense>
      <Button />
    </Suspense>
  );
}
