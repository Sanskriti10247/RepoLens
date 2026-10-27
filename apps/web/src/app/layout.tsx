import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, Newsreader } from "next/font/google";
import Link from "next/link";
import type { ReactNode } from "react";

import { ApiStatus } from "@/components/ApiStatus";
import { Mark } from "@/components/Mark";
import { InstanceNote, SiteNav } from "@/components/SiteNav";

import "./globals.css";

const serif = Newsreader({
  subsets: ["latin"],
  variable: "--font-serif",
  style: ["normal", "italic"],
  axes: ["opsz"],
  display: "swap",
});
const sans = IBM_Plex_Sans({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-sans", display: "swap" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = {
  title: { default: "RepoLens", template: "%s · RepoLens" },
  description: "Code graph, change impact and test selection for Python repositories",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f3ec" },
    { media: "(prefers-color-scheme: dark)", color: "#1c1a17" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${serif.variable} ${sans.variable} ${mono.variable}`}>
      <body>
        <div className="shell">
          <header className="rail">
            <Link href="/" className="wordmark">
              <Mark />
              <span>RepoLens</span>
            </Link>
            <p className="rail-tagline">Change impact and test selection for Python repositories</p>
            <SiteNav />
            <div className="rail-foot">
              <ApiStatus />
              <InstanceNote />
            </div>
          </header>
          <main className="content">{children}</main>
        </div>
      </body>
    </html>
  );
}
