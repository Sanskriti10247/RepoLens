import type { Metadata } from "next";

import { SignInButton } from "./SignInButton";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <section className="page-head login" aria-labelledby="login-title">
      <h1 id="login-title">Sign in</h1>
      <p className="lede">Your repositories and their analyses are visible only to you.</p>
      <SignInButton />
    </section>
  );
}
