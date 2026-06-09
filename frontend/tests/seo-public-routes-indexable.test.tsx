import { describe, it, expect } from "vitest";
import type { Metadata } from "next";

import { metadata as rootMetadata } from "@/app/page";
import { metadata as loginMetadata } from "@/app/login/page";
import { metadata as registerMetadata } from "@/app/register/page";
import { metadata as privacyMetadata } from "@/app/privacy/page";
import { metadata as termsMetadata } from "@/app/terms/page";
import { metadata as docsMetadata } from "@/app/docs/page";
import { metadata as docsPlansMetadata } from "@/app/docs/plans/page";
import { metadata as featuresMetadata } from "@/app/features/page";
import { metadata as compareMetadata } from "@/app/compare/page";

const indexableMetadatas: ReadonlyArray<[string, Metadata]> = [
  ["/", rootMetadata],
  ["/register", registerMetadata],
  ["/privacy", privacyMetadata],
  ["/terms", termsMetadata],
  ["/docs", docsMetadata],
  ["/docs/plans", docsPlansMetadata],
  ["/features", featuresMetadata],
  ["/compare", compareMetadata],
] as const;

describe("indexable public routes opt back into index", () => {
  it.each(indexableMetadatas)("%s sets robots index/follow true", (_route, meta) => {
    expect(meta.robots).toEqual({ index: true, follow: true });
  });
});

describe("low-value auth routes stay out of the index", () => {
  it("/login is noindex (bare sign-in form, no search value)", () => {
    expect(loginMetadata.robots).toEqual({ index: false, follow: true });
  });
});
