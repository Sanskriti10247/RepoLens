import type { ReactNode } from "react";

/**
 * Renders the small markdown subset the API produces (paragraphs, "- " lists, **bold**,
 * `code`) as React elements. No HTML is ever injected, so repository text stays inert.
 */
export function Markdownish({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="markdownish">
      {blocks.map((block, index) => {
        const lines = block.split("\n");
        const listLines = lines.filter((line) => line.startsWith("- "));
        const heading = lines[0] && !lines[0].startsWith("- ") ? lines[0] : null;
        if (listLines.length > 0) {
          return (
            <div key={index}>
              {heading && <p>{inline(heading)}</p>}
              <ul>
                {listLines.map((line, i) => (
                  <li key={i}>{inline(line.slice(2))}</li>
                ))}
              </ul>
            </div>
          );
        }
        return <p key={index}>{inline(block)}</p>;
      })}
    </div>
  );
}

export function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[E\d+\])/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) parts.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
    else if (token.startsWith("`")) parts.push(<code key={key++} className="inline-code">{token.slice(1, -1)}</code>);
    else
      parts.push(
        <a key={key++} href={`#${token.slice(1, -1)}`} className="cite">
          {token}
        </a>,
      );
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}
