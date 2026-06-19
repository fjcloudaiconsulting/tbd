// Shared chevron toggle indicator for FAQ <details>/<summary> accordions.
// Used by Faq.tsx, /features, /compare, and VsPageLayout so all FAQ chevrons
// stay visually consistent without duplicating SVG markup.
export default function ChevronGlyph() {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      className="h-4 w-4 flex-shrink-0 text-text-muted transition-transform group-open:rotate-180"
    >
      <path
        d="M3 6l5 5 5-5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
