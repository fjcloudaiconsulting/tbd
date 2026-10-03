// Runtime-only config for the app image: one build serves every environment.
// The server reads TBD_* from process.env on each request; the root layout
// serialises the result into window.__TBD_CONFIG__ (see RuntimeConfigScript)
// so client code sees the same values. Nothing here is inlined at build time.
// The apex static export has no server: next.config.apex.ts inlines its few
// build-time TBD_* values instead, and the env fallback below picks them up.
export type RuntimeConfig = {
  apiUrl: string;
  googleSsoEnabled: boolean;
  captchaSiteKey: string;
  appVersion: string;
};

declare global {
  interface Window {
    __TBD_CONFIG__?: RuntimeConfig;
  }
}

export function readEnvConfig(): RuntimeConfig {
  return {
    apiUrl: process.env.TBD_API_URL || "",
    googleSsoEnabled: process.env.TBD_GOOGLE_SSO_ENABLED === "true",
    captchaSiteKey: process.env.TBD_CAPTCHA_SITE_KEY ?? "",
    appVersion: process.env.TBD_APP_VERSION || "dev",
  };
}

// Call at use time, never at module scope: on the client the config script
// must have run, and on the server the env must be read per request.
export function runtimeConfig(): RuntimeConfig {
  return (typeof window !== "undefined" && window.__TBD_CONFIG__) || readEnvConfig();
}
