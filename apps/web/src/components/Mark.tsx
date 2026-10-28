/** The RepoLens mark: a loupe held over the point where a branch leaves the trunk. */
export function Mark({ size = 26 }: { size?: number }) {
  return (
    <svg
      className="mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {/* trunk */}
      <path d="M8.6 2.8c.3 4.6-.2 9.4.1 14.1.2 4.1-.1 8.2.2 12.3" stroke="currentColor" strokeWidth="1.7" />
      {/* branch leaving the trunk, running under the lens */}
      <path d="M8.8 22.4c.4-4.1 3.1-5.6 6.1-6.6 2.2-.8 3.3-1.9 3.5-3.9" stroke="var(--accent)" strokeWidth="1.7" />
      <circle cx="18.5" cy="11.6" r="1.5" fill="var(--accent)" />
      <circle cx="8.9" cy="29.2" r="1.5" fill="currentColor" />
      {/* lens, drawn in one stroke that overshoots its start */}
      <path
        d="M11.6 11.9c-.1-3.7 2.9-6.5 6.6-6.4 3.6.1 6.3 2.9 6.2 6.5-.1 3.5-3 6.3-6.6 6.2-3.4-.1-6-2.7-6.2-6 .1-1.6.7-2.9 1.8-3.9"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      {/* handle */}
      <path d="M23 16.7l5.3 5.6" stroke="currentColor" strokeWidth="2.6" />
    </svg>
  );
}
