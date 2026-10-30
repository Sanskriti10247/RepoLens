"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef } from "react";

type StartViewTransition = (callback: () => Promise<void>) => { finished: Promise<void> };

/**
 * Cross-fades between pages with the View Transitions API where the browser has it: an
 * in-app link click starts a transition that completes once the new route has rendered.
 * Browsers without it (and visitors who asked for reduced motion) get the plain CSS
 * entrance on the new content instead.
 */
export function PageTransitions() {
  return (
    <Suspense fallback={null}>
      <Transitions />
    </Suspense>
  );
}

function Transitions() {
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const pending = useRef<(() => void) | null>(null);
  const location = `${pathname}?${search?.toString() ?? ""}`;

  // The route changed: let a waiting transition capture the new page.
  useEffect(() => {
    const done = pending.current;
    pending.current = null;
    if (done) requestAnimationFrame(() => done());
  }, [location]);

  useEffect(() => {
    const start = (document as Document & { startViewTransition?: StartViewTransition }).startViewTransition;
    if (typeof start !== "function") return;
    function onClick(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }
      if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
      const anchor = (event.target as Element | null)?.closest?.("a");
      if (!anchor || anchor.target || anchor.hasAttribute("download")) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin || url.pathname.startsWith("/api/")) return;
      // Same page (a hash, or the page already shown): leave it to the browser and the router.
      if (url.pathname === window.location.pathname) return;
      event.preventDefault();
      event.stopPropagation();
      const href = url.pathname + url.search + url.hash;
      start.call(
        document,
        () =>
          new Promise<void>((resolve) => {
            pending.current = resolve;
            // Never hold the page hostage if the route takes long to render.
            setTimeout(resolve, 700);
            router.push(href);
          }),
      );
    }
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, [router]);

  return null;
}
