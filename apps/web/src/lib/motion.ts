"use client";

import { useEffect, useRef, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

function mediaQuery(): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  return window.matchMedia(QUERY);
}

/**
 * Whether the visitor asked for reduced motion. CSS handles most of it with the same media
 * query; components use this for motion that is driven from JavaScript (counting numbers,
 * staggered reveals) and mark themselves with ``data-motion`` so both agree.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => mediaQuery()?.matches ?? false);
  useEffect(() => {
    const query = mediaQuery();
    if (!query) return;
    const update = () => setReduced(query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);
  return reduced;
}

/**
 * ``target`` eased in over ``ms`` from the previous value, for counters that should glide
 * rather than jump. Returns ``target`` unchanged when motion is reduced.
 */
export function useEasedNumber(target: number, ms = 600): number {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    if (reduced || typeof requestAnimationFrame !== "function") {
      from.current = target;
      // Synchronise with the target when no animation runs (reduced motion, no rAF).
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setShown(target);
      return;
    }
    const start = performance.now();
    const origin = from.current;
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 3);
      const value = origin + (target - origin) * eased;
      from.current = value;
      setShown(value);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, ms, reduced]);
  return reduced ? target : shown;
}

/** Re-render every ``intervalMs`` and return the current time (for elapsed clocks). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/**
 * Like ``useEasedNumber`` but starting from zero, so a figure counts up the first time it
 * is shown. Returns ``target`` unchanged when motion is reduced.
 */
export function useCountUp(target: number, ms = 900): number {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    if (reduced || typeof requestAnimationFrame !== "function") {
      from.current = target;
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setShown(target);
      return;
    }
    const start = performance.now();
    const origin = from.current;
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 4);
      const value = origin + (target - origin) * eased;
      from.current = value;
      setShown(t < 1 ? value : target);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, ms, reduced]);
  return reduced ? target : Math.round(shown * 1000) / 1000;
}
