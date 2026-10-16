"use client";

import { useEffect, useRef } from "react";

interface Props {
  content: string;
  highlight?: { start: number; end: number } | null;
  maxHeight?: number;
}

/** Plain-text source with line numbers; never interprets repository content as HTML. */
export function SourceView({ content, highlight, maxHeight = 520 }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const lines = content.split("\n");
  useEffect(() => {
    if (!highlight || !container.current) return;
    const target = container.current.querySelector<HTMLElement>(`[data-line="${highlight.start}"]`);
    if (target) container.current.scrollTop = Math.max(target.offsetTop - 60, 0);
  }, [highlight, content]);
  return (
    <div className="source" ref={container} style={{ maxHeight }}>
      <table>
        <tbody>
          {lines.map((text, index) => {
            const number = index + 1;
            const marked = highlight && number >= highlight.start && number <= highlight.end;
            return (
              <tr key={number} data-line={number} className={marked ? "marked" : undefined}>
                <td className="ln">{number}</td>
                <td className="code">{text || " "}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
