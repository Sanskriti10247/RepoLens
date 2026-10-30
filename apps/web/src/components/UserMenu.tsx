"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { browserClient, supabaseEnv } from "@/lib/supabase";

interface Person {
  email: string | null;
  avatar: string | null;
}

/** The signed-in account and a way out. Renders nothing without Supabase sign-in. */
export function UserMenu() {
  const [supabase] = useState(() => {
    const env = supabaseEnv();
    return env ? browserClient(env) : null;
  });
  const router = useRouter();
  const [person, setPerson] = useState<Person | null>(null);

  useEffect(() => {
    if (!supabase) return;
    const { data } = supabase.auth.onAuthStateChange((_event, session) => {
      const user = session?.user;
      setPerson(user ? { email: user.email ?? null, avatar: (user.user_metadata?.avatar_url as string) ?? null } : null);
    });
    return () => data.subscription.unsubscribe();
  }, [supabase]);

  if (!supabase || !person) return null;
  async function signOut() {
    await supabase!.auth.signOut();
    router.replace("/login");
    router.refresh();
  }
  return (
    <div className="user-menu">
      {person.avatar && (
        // eslint-disable-next-line @next/next/no-img-element -- a remote avatar, not a page asset
        <img src={person.avatar} alt="" width={24} height={24} referrerPolicy="no-referrer" />
      )}
      <span className="user-email hide-sm" title={person.email ?? undefined}>
        {person.email}
      </span>
      <button type="button" className="link" onClick={signOut}>
        Sign out
      </button>
    </div>
  );
}
