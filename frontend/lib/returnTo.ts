// Open-redirect guard for the ``?returnTo=`` login parameter.
//
// The forced-re-login flow (AppShell → /login → back to where you were)
// round-trips a destination through the URL. Because that value is
// attacker-controllable (anyone can craft a /login?returnTo=… link), it
// must be validated before we ever hand it to router.push / router.replace.
//
// Policy: honor ONLY a same-origin, absolute, relative path — i.e. a value
// that, after a single decode, starts with exactly one "/" and is not a
// protocol-relative ("//host") or backslash-smuggled ("/\host") URL, nor a
// scheme like ``javascript:`` / ``https:``. The login and setup routes are
// additionally rejected so a redirect can never loop back onto the auth
// screens. Anything that fails falls back to ``/dashboard``.
export const RETURN_TO_FALLBACK = "/dashboard";

// Auth/setup routes we must never redirect back to (would loop the user
// on the login/setup screen). Exact-match only — ``/loginhelp`` is a
// legitimate app path and must pass.
const BLOCKED_RETURN_PATHS = new Set(["/login", "/setup"]);

export function sanitizeReturnTo(raw: string | null | undefined): string {
  if (!raw) return RETURN_TO_FALLBACK;

  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    // Malformed percent-encoding (e.g. a lone "%"): never leak the error,
    // just fall back.
    return RETURN_TO_FALLBACK;
  }

  // Must be an absolute same-origin path with a single leading slash.
  // ``/[/\\]`` catches both protocol-relative ("//evil") and
  // backslash-smuggled ("/\evil") forms in one check; a missing leading
  // slash rejects ``https://``, ``javascript:``, and bare tokens.
  if (!decoded.startsWith("/") || /^\/[/\\]/.test(decoded)) {
    return RETURN_TO_FALLBACK;
  }

  const pathOnly = decoded.split("?")[0].split("#")[0];
  if (BLOCKED_RETURN_PATHS.has(pathOnly)) return RETURN_TO_FALLBACK;

  return decoded;
}
