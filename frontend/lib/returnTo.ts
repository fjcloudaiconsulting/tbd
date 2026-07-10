// Open-redirect guard for the ``?returnTo=`` login parameter.
//
// The forced-re-login flow (AppShell → /login → back to where you were)
// round-trips a destination through the URL. Because that value is
// attacker-controllable (anyone can craft a /login?returnTo=… link), it
// must be validated before we ever hand it to router.push / router.replace.
//
// Policy: honor ONLY a same-origin, absolute, relative path. Rather than
// hand-rolling positional checks (which missed embedded tab/newline/CR that
// browsers strip mid-parse, collapsing "/⇥/evil.com" into a cross-origin
// "//evil.com" at navigation time), we resolve the candidate against a
// sentinel origin with the URL constructor and require the origin to stay
// put. The login and setup routes are additionally rejected so a redirect
// can never loop back onto the auth screens. Anything that fails falls back
// to ``/dashboard``.
export const RETURN_TO_FALLBACK = "/dashboard";

// Auth/setup routes we must never redirect back to (would loop the user
// on the login/setup screen). Exact-match only — ``/loginhelp`` and
// ``/setuphelp`` are legitimate app paths and must pass.
const BLOCKED_RETURN_PATHS = new Set(["/login", "/setup"]);

// Sentinel we resolve every candidate against. A genuine same-origin path
// leaves this origin unchanged; anything that reaches out (protocol-relative,
// absolute URL, backslash-smuggled, control-char smuggled) shifts or drops it.
const SENTINEL_ORIGIN = "https://x.invalid";

export function sanitizeReturnTo(raw: string | null | undefined): string {
  // The caller (searchParams.get) has ALREADY percent-decoded the value
  // once. Decoding a second time throws on a legit literal "%" (e.g.
  // "/reports/50%-growth") and is itself a bypass-enabler, so we operate on
  // the value exactly as received.
  if (!raw) return RETURN_TO_FALLBACK;

  // Must be an absolute same-origin path with a leading slash. A missing
  // leading slash rejects ``https://``, ``javascript:``, and bare tokens up
  // front; the origin check below handles the "//host" / "/\host" forms.
  if (!raw.startsWith("/")) return RETURN_TO_FALLBACK;

  // Belt-and-suspenders against the strip trick: reject ANY C0 control
  // character. The WHATWG URL parser silently removes U+0009 (tab), U+000A
  // (LF) and U+000D (CR), so "/\t/evil.com" would otherwise reparse as the
  // protocol-relative "//evil.com" and redirect cross-origin.
  //
  // Also reject a literal backslash outright. Special-scheme URL parsing
  // rewrites "\" to "/", so a dot-segment payload like "/x/..\evil.com" can
  // normalize to a host-shaped path that smuggles a redirect past the origin
  // check. A backslash never appears in a legitimate returnTo path.
  if (/[\u0000-\u001f]/.test(raw)) return RETURN_TO_FALLBACK;

  if (raw.includes("\\")) return RETURN_TO_FALLBACK;

  let u: URL;
  try {
    u = new URL(raw, SENTINEL_ORIGIN);
  } catch {
    // Malformed input: never leak the error, just fall back.
    return RETURN_TO_FALLBACK;
  }

  // A cross-origin candidate (//evil.com, /\evil.com, https://evil.com,
  // javascript: → opaque "null" origin) moves the origin off the sentinel.
  if (u.origin !== SENTINEL_ORIGIN) return RETURN_TO_FALLBACK;

  // Never loop back onto the auth screens. Exact-path match only, so
  // /loginhelp and /setuphelp stay allowed.
  if (BLOCKED_RETURN_PATHS.has(u.pathname)) return RETURN_TO_FALLBACK;

  // Reconstruct from the parsed components so a legit query + fragment
  // survive while any smuggled host/control chars are gone.
  const result = `${u.pathname}${u.search}${u.hash}`;

  // Dot-segment normalization ("/..//evil.com", "/x/..//evil.com", …) can make
  // u.pathname begin with "//" while the sentinel origin above stays intact.
  // The reconstructed value is then protocol-relative and re-resolves
  // cross-origin when the router parses it against the real app origin, so
  // re-validate the reconstructed string before returning it.
  try {
    if (new URL(result, SENTINEL_ORIGIN).origin !== SENTINEL_ORIGIN)
      return RETURN_TO_FALLBACK;
  } catch {
    return RETURN_TO_FALLBACK;
  }
  return result;
}
