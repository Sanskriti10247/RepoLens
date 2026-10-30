import type { Metadata, Viewport } from "next";
import { Instrument_Serif, JetBrains_Mono, Schibsted_Grotesk } from "next/font/google";
import Link from "next/link";
import Script from "next/script";
import type { ReactNode } from "react";

import { ApiStatus } from "@/components/ApiStatus";
import { Mark } from "@/components/Mark";
import { InstanceNote, SiteNav } from "@/components/SiteNav";
import { THEME_SCRIPT, ThemeToggle } from "@/components/ThemeToggle";

import "./globals.css";
import "./visuals.css";
import "./insights.css";

// Display: a high-contrast serif for titles and figures. Text: a newsroom grotesk.
// Code: a mono with clear zero and long-name legibility.
const display = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-display",
  display: "swap",
});
const sans = Schibsted_Grotesk({ subsets: ["latin"], variable: "--font-sans", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = {
  title: { default: "RepoLens", template: "%s · RepoLens" },
  description: "Code graph, health report, change impact and test selection for a Git repository",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3efe6" },
    { media: "(prefers-color-scheme: dark)", color: "#13120f" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <body>
        {/* Apply a saved theme before the first paint so the page never flashes. */}
        <Script id="theme" strategy="beforeInteractive">
          {THEME_SCRIPT}
        </Script>
        <a href="#main" className="skip-link">
          Skip to content
        </a>
        <header className="masthead">
          <div className="masthead-inner">
            <Link href="/" className="wordmark" aria-label="RepoLens home">
              <Mark />
              <span>RepoLens</span>
            </Link>
            <SiteNav />
            <div className="masthead-end">
              <ApiStatus />
              <ThemeToggle />
            </div>
          </div>
        </header>
        <InstanceNote />
        <main className="content" id="main">
          {children}
        </main>
        <footer className="colophon">
          <span>RepoLens reads code as text. Repository code runs only inside the test sandbox.</span>
          <span>
            <a href="/api/docs" target="_blank" rel="noreferrer">
              API reference
            </a>
            <span className="sep">·</span>
            <a href="https://github.com/Sanskriti10247/RepoLens" target="_blank" rel="noreferrer">
              Source
            </a>
          </span>
        </footer>
      </body>
    </html>
  );
}
