import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { ApiStatus } from "@/components/ApiStatus";

import "./globals.css";

export const metadata: Metadata = {
  title: "RepoLens",
  description: "Code graph, change impact and test selection for Python repositories",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="topbar">
            <Link href="/" className="brand">
              RepoLens
            </Link>
            <ApiStatus />
          </header>
          <nav className="sidebar" aria-label="Primary">
            <Link href="/">Repositories</Link>
            <Link href="/queue">Job queue</Link>
            <a href="/api/docs" target="_blank" rel="noreferrer">
              API docs
            </a>
          </nav>
          <main className="content">{children}</main>
        </div>
      </body>
    </html>
  );
}
