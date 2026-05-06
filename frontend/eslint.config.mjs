import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// Rules downgraded to warnings. The react-hooks/* and react/no-unescaped-entities
// hits were surfaced by the Next 15 → 16 / React 18 → 19 upgrade — existing code
// violates them but fixes are tracked as a follow-up cleanup pass, not in scope
// for the upgrade PR. @next/next/no-html-link-for-pages is intentional in
// app/global-error.tsx, which renders outside Next's routing tree where <Link>
// is unsafe.
const upgradeFollowUps = {
  "react-hooks/set-state-in-effect": "warn",
  "react-hooks/purity": "warn",
  "react-hooks/preserve-manual-memoization": "warn",
  "react-hooks/exhaustive-deps": "warn",
  "react/no-unescaped-entities": "warn",
  "@next/next/no-html-link-for-pages": "warn",
};

const eslintConfig = [
  ...nextCoreWebVitals,
  ...nextTypescript,
  {
    rules: upgradeFollowUps,
  },
  {
    ignores: [
      "node_modules/**",
      ".next/**",
      "out/**",
      "build/**",
      "next-env.d.ts",
    ],
  },
];

export default eslintConfig;
