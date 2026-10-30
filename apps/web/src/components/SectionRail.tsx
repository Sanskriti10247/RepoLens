"use client";

import { useEffect, useState, type ReactNode } from "react";

/**
 * A long page with a sticky "on this page" rail on wide screens. The entry for the section
 * being read is marked as the reader scrolls. Sections that are not on the page (not loaded
 * yet, or not applicable) are left out of the rail.
 */
export function WithRail({ items, children }: { items: [string, string][]; children: ReactNode }) {
  const [present, setPresent] = useState<string[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const key = items.map(([id]) => id).join(" ");

  useEffect(() => {
    const ids = key.split(" ");
    const check = () => setPresent(ids.filter((id) => document.getElementById(id)));
    check();
    // Sections arrive as their data loads; keep the rail in step with the page.
    const observer = new MutationObserver(check);
    const main = document.getElementById("main");
    if (main) observer.observe(main, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [key]);

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const visible = new Map<string, number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.set(entry.target.id, entry.boundingClientRect.top);
          else visible.delete(entry.target.id);
        }
        const first = [...visible.entries()].sort((a, b) => a[1] - b[1])[0];
        if (first) setCurrent(first[0]);
      },
      { rootMargin: "-120px 0px -55% 0px" },
    );
    for (const id of present) {
      const element = document.getElementById(id);
      if (element) observer.observe(element);
    }
    return () => observer.disconnect();
  }, [present]);

  const shown = items.filter(([id]) => present.includes(id));
  return (
    <div className="with-rail">
      <nav className="page-rail" aria-label="On this page">
        <span className="label">On this page</span>
        <ol>
          {shown.map(([id, text]) => (
            <li key={id}>
              <a href={`#${id}`} className={current === id ? "is-current" : undefined} aria-current={current === id ? "location" : undefined}>
                {text}
              </a>
            </li>
          ))}
        </ol>
      </nav>
      <div className="rail-body">{children}</div>
    </div>
  );
}
