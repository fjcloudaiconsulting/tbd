/**
 * Safe URL linkification for plain-text announcement bodies.
 *
 * The spec locks the body content shape to plain text + auto-linkify.
 * We never render HTML or markdown from the operator payload — we
 * accept plain text on the wire and replace any bare ``http(s)://``
 * URL we recognise with an anchor element.
 *
 * Safety posture:
 *   - The matcher only recognises absolute ``http://`` / ``https://``
 *     URLs. Bare hostnames, ``javascript:``, ``data:`` and other
 *     schemes never match the regex, so they cannot become hrefs.
 *   - The matched URL is passed through ``new URL`` so a constructed
 *     anchor href is always a parsed URL and never a free-form string
 *     spliced into the DOM. If parsing throws, we fall back to
 *     rendering the matched text as plain text.
 *   - The anchor opens in a new tab with ``rel="noopener noreferrer"``
 *     to deny window.opener access and Referer leakage.
 *
 * This is intentionally NOT a full URL-detection library. If we ever
 * need richer matching (mailto:, scheme-less domains, etc.) we'll
 * lift it into a dedicated helper with its own tests. Until then the
 * surface area stays narrow on purpose.
 */
import { Fragment } from "react";

// Match http(s) URLs. Conservative: must start with ``http`` and a
// scheme separator; trailing punctuation like ``.``, ``,``, ``)`` is
// trimmed below so a sentence-ending URL still renders cleanly.
const URL_REGEX = /\bhttps?:\/\/[^\s<>"']+/gi;

// Punctuation we strip from the END of a matched URL before rendering
// the anchor (the trailing char then renders as plain text). Common
// case: "see https://x.com/path." — the period belongs to the
// sentence, not the URL.
const TRAILING_PUNCT_RE = /[.,;:!?)\]]+$/;

interface LinkifyOptions {
  /** className applied to every rendered anchor */
  linkClassName?: string;
}

export function linkifyAnnouncementBody(
  body: string,
  { linkClassName }: LinkifyOptions = {},
): React.ReactNode {
  if (!body) return null;

  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  // Recreate the regex per call so the global lastIndex doesn't leak
  // across invocations.
  const re = new RegExp(URL_REGEX.source, URL_REGEX.flags);

  while ((match = re.exec(body)) !== null) {
    const matched = match[0];
    const start = match.index;
    const end = start + matched.length;

    // Push any text before this URL.
    if (start > lastIndex) {
      parts.push(body.slice(lastIndex, start));
    }

    // Strip trailing punctuation from the matched URL and push it as
    // plain text after the anchor.
    let url = matched;
    let trailing = "";
    const punctMatch = url.match(TRAILING_PUNCT_RE);
    if (punctMatch) {
      trailing = punctMatch[0];
      url = url.slice(0, url.length - trailing.length);
    }

    let href: string | null = null;
    try {
      // URL constructor rejects schemes other than what's in the
      // regex match, but we still pipe through it so the href is
      // always a parsed URL string rather than a raw splice.
      const parsed = new URL(url);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") {
        href = parsed.toString();
      }
    } catch {
      href = null;
    }

    if (href) {
      parts.push(
        <a
          key={`a-${start}`}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className={linkClassName}
        >
          {url}
        </a>,
      );
    } else {
      // URL parse failure — render as plain text.
      parts.push(url);
    }
    if (trailing) parts.push(trailing);
    lastIndex = end;
  }

  if (lastIndex < body.length) {
    parts.push(body.slice(lastIndex));
  }
  return <Fragment>{parts}</Fragment>;
}
