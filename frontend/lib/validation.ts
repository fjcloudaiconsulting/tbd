/**
 * Client-side validation rules that mirror server-side schemas.
 *
 * These MUST stay in sync with backend/app/schemas/auth.py. If you change
 * one, change the other. A future refactor could codegen these from the
 * OpenAPI schema, but for a handful of fields manual duplication is fine.
 */

export const USERNAME_MIN_LENGTH = 3;
export const USERNAME_MAX_LENGTH = 64;
export const USERNAME_PATTERN = "^[a-zA-Z0-9._-]+$";
export const USERNAME_PATTERN_RE = /^[a-zA-Z0-9._-]+$/;
export const USERNAME_RULE_HINT =
  "3–64 characters. Letters, digits, dot, underscore, hyphen.";
