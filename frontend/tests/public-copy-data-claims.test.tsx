// public-copy-data-claims.test.tsx — TBD-343.
//
// Four public surfaces published claims about data export and account
// deletion that were false against the shipped product. Three of them
// publish machine-readably, which is the aggravating factor: the landing
// FAQ and the /features FAQ both feed schema.org FAQPage JSON-LD,
// /features also feeds SoftwareApplication.featureList, and
// frontend/public/llms.txt is written specifically for AI crawlers and is
// on the apex output allowlist (scripts/build-apex.sh).
//
// WHAT THESE FENCES DO AND DO NOT KILL. They kill the LITERAL prior
// strings, and a surface silently dropping the shared retention constant.
// They do NOT fence the claim CLASS: a reworded but equally false answer
// ("a single tap in Settings", "every transaction list") passes. That is
// inherent to copy assertions and is recorded here rather than implied
// away, because a comment claiming more than the test delivers is how a
// fence becomes decoration.
//
// Named wrong implementations, each proven RED:
//   fence 1  faqData.ts   "Every list view exports to CSV"
//   fence 2  faqData.ts   "one click in Settings ... within seven days"
//   fence 3  features     "Export your data anytime." / "in one click."
//            plus the two /features JSON-LD strings ("export anytime" in
//            featureList, "export it anytime" in the FAQ answer)
//   fence 4  any surface that stops quoting DATA_DELETION_WINDOW_DAYS
//   fence 5  llms.txt reverting to the pre-fix export answer
//
// Fences 1/2 assert against the RENDERED FAQPage JSON-LD, not the source
// array, which is what covers the ticket's "verify the JSON-LD output
// actually changes" requirement.

import React from "react";
import path from "node:path";
import { readFileSync, readdirSync } from "node:fs";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import LandingPage from "@/app/page";
import FeaturesPage from "@/app/features/page";
import PrivacyPolicyPage from "@/app/privacy/page";
import TermsPage from "@/app/terms/page";
import {
  DATA_DELETION_WINDOW_DAYS,
  PRIVACY_CONTACT_EMAIL,
} from "@/lib/dataPolicy";

// Stub the client island that requires AuthProvider/useRouter — we only
// care about server-rendered copy and the JSON-LD <script> payloads.
vi.mock("@/components/landing/LandingAuthRedirect", () => ({
  default: () => null,
}));

// readNonce uses next/headers, unavailable under Vitest.
vi.mock("@/lib/nonce", () => ({
  readNonce: async () => "",
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

type FaqNode = { name: string; acceptedAnswer: { text: string } };

/** Pull the FAQPage JSON-LD block out of the rendered landing page. */
async function landingFaqJsonLd(): Promise<FaqNode[]> {
  const ui = await LandingPage();
  const { container } = render(ui as React.ReactElement);
  const block = Array.from(
    container.querySelectorAll('script[type="application/ld+json"]'),
  )
    .map((s) => JSON.parse(s.textContent ?? "{}"))
    .find((p) => p["@type"] === "FAQPage");
  expect(block, "landing page must emit an FAQPage JSON-LD block").toBeDefined();
  return block.mainEntity;
}

/**
 * Resolve one FAQ answer by its EXACT question text.
 *
 * Deliberately not a substring/regex `.find()`. A regex match on the
 * question was shown to be shadowable: prepending two ordinary new
 * questions ("How do I export a report?", "Can I delete a single
 * transaction?") captured the lookup and let BOTH original false answers
 * be republished with the whole suite green. Exact-match plus a
 * uniqueness assertion closes that.
 */
function answerTo(entries: FaqNode[], question: string): string {
  const hits = entries.filter((e) => e.name === question);
  expect(
    hits.length,
    `expected exactly one FAQ entry named ${JSON.stringify(question)}; ` +
      `found ${hits.length}. If the question was reworded, update this ` +
      `fence deliberately rather than letting it stop asserting.`,
  ).toBe(1);
  return hits[0].acceptedAnswer.text;
}

function readLlmsTxt(): string {
  return readFileSync(
    path.join(path.resolve(__dirname, ".."), "public/llms.txt"),
    "utf-8",
  );
}

/** The window as it must appear in prose on every surface that states it. */
const WINDOW_PHRASE = `${DATA_DELETION_WINDOW_DAYS} days`;

describe("public copy — data export and deletion claims (TBD-343)", () => {
  it("fence 1: the FAQ does not claim every list view exports to CSV", async () => {
    const answer = answerTo(await landingFaqJsonLd(), "Can I export my data?");

    // Wrong implementation: "Yes. Every list view exports to CSV, and a
    // one-click full org export is in the works."
    expect(
      answer,
      "no list view has CSV export; only report widgets do",
    ).not.toMatch(/every list view/i);

    // Positive leg: name the surface that actually exports, so deleting
    // the false claim and saying nothing true does not pass.
    expect(answer).toMatch(/report/i);
  });

  it("fence 2: the FAQ does not claim self-serve one-click account deletion", async () => {
    const answer = answerTo(await landingFaqJsonLd(), "Can I delete my account?");

    // Wrong implementation: "Account deletion is one click in Settings.
    // It hard-deletes your data within seven days..."
    //
    // NOTE: the old answer also promised a confirmation email. That part
    // was NOT false — send_account_deleted_email exists
    // (backend/app/services/email_service.py) and fires from the admin
    // delete path an emailed erasure request would use. It is dropped
    // from the copy because the surrounding self-serve flow does not
    // exist, so this fence deliberately does NOT assert its absence:
    // doing so would go red against correct copy that mentioned it.
    expect(
      answer,
      "there is no self-serve delete endpoint on users or orgs",
    ).not.toMatch(/one[ -]click/i);
    expect(answer, "seven days contradicts the privacy policy").not.toMatch(
      /seven days/i,
    );

    // Positive leg: route the reader to the mechanism that does exist.
    expect(answer).toContain(PRIVACY_CONTACT_EMAIL);
  });

  it("fence 3: /features claims no one-click deletion and no unconditional export", async () => {
    const ui = await FeaturesPage();
    const { container } = render(ui as React.ReactElement);
    // textContent spans the JSON-LD <script> blocks too, so this reaches
    // SoftwareApplication.featureList and the /features FAQPage answer,
    // not only the visible bullets. Verified by injection.
    const text = container.textContent ?? "";

    // Sanity anchor: prove we really rendered the bullet group, so an
    // empty render cannot pass the negative assertions below.
    expect(text).toMatch(/EU-hosted and processed under EU law/i);

    // Wrong implementations: the two bullets, the featureList entry
    // ("EU-hosted, export anytime, ...") and the privacy FAQ answer
    // ("you can export it anytime").
    expect(text).not.toMatch(/in one click/i);
    expect(text).not.toMatch(/export (it |your data )?anytime/i);

    // Positive legs, one per corrected JSON-LD string.
    expect(text).toMatch(/CSV export from any report/i);
    expect(text).toContain(PRIVACY_CONTACT_EMAIL);
  });

  it("fence 4: every surface stating the deletion window quotes the shared constant", async () => {
    const faqAnswer = answerTo(
      await landingFaqJsonLd(),
      "Can I delete my account?",
    );
    const privacy = render(<PrivacyPolicyPage />).container.textContent ?? "";
    const terms = render(<TermsPage />).container.textContent ?? "";

    // Containment, not prose-scraping. An earlier draft extracted the
    // number with /delete[^.]{0,80}?within (\d+) days/ and went RED
    // against three CORRECT rewordings (a synonym, a longer clause, and
    // a second unrelated true retention window on the privacy page).
    // A fence that fires on correct copy gets deleted the first time
    // someone legitimately rewrites the policy.
    //
    // Wrong implementation this kills: any one surface replacing the
    // interpolated constant with a hardcoded literal, which is exactly
    // the pre-fix state (FAQ said seven, policy said thirty).
    for (const [label, text] of [
      ["landing FAQ", faqAnswer],
      ["privacy policy", privacy],
      ["terms", terms],
    ] as const) {
      expect(
        text,
        `${label} must state the deletion window as "${WINDOW_PHRASE}"`,
      ).toContain(WINDOW_PHRASE);
    }

    // Known limitation, recorded not implied: a contradiction expressed
    // in another unit ("completed within six months") is invisible here.
  });

  it("fence 5: llms.txt does not republish the corrected claims", () => {
    const llms = readLlmsTxt();

    // Wrong implementation: the verbatim pre-fix /features answer, which
    // this hand-maintained file duplicated. It ships to
    // https://thebetterdecision.com/llms.txt via the apex allowlist, so
    // it is the most machine-targeted surface in the repo.
    expect(llms).not.toMatch(/export (it |your data )?anytime/i);
    expect(llms).not.toMatch(/every list view/i);
    expect(llms).not.toMatch(/in one click/i);

    // Positive leg: the privacy answer must carry the real mechanism.
    expect(llms).toContain(PRIVACY_CONTACT_EMAIL);
  });

  it("fence 6 (TBD-592): every EU-residency answer carries the AI exception", async () => {
    // Wrong implementation: any one surface reverting to an unqualified "stays in the
    // EU" / "processed under EU law", which is false for what AI features send.
    const AI = /except content you send through AI features|exception is content you send through AI features/;
    expect(answerTo(await landingFaqJsonLd(), "Is my data secure?")).toMatch(AI);

    const ui = await FeaturesPage();
    const { container } = render(ui as React.ReactElement);
    const faq = Array.from(container.querySelectorAll('script[type="application/ld+json"]'))
      .map((el) => JSON.parse(el.textContent ?? "{}"))
      .find((b) => b["@type"] === "FAQPage");
    expect(answerTo(faq.mainEntity, "Is my financial data private?")).toMatch(AI);
    const bullets = Array.from(container.querySelectorAll("li")).map((li) => li.textContent ?? "");
    const eu = bullets.filter((b) => /processed under EU law/.test(b));
    expect(eu.length).toBeGreaterThan(0);
    for (const b of eu) expect(b).toMatch(AI);

    const llms = readLlmsTxt();
    const private_ = llms.slice(llms.indexOf("### Is my financial data private?"));
    expect(private_.split("\n\n")[1]).toMatch(AI);
  });

  it("fence 7 (TBD-592): no file in the tree makes an EU claim without the AI exception", () => {
    // Wrong implementation: any one surface (FAQ, comparison page or table, landing tile, llms.txt)
    // reverting to an unqualified "processed under EU law" or "moved across borders".
    const root = path.resolve(__dirname, "..");
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const e of readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name);
        if (e.isDirectory()) walk(p);
        else if (/\.(tsx?|txt)$/.test(e.name)) files.push(p);
      }
    };
    for (const d of ["app", "components", "lib", "public"]) walk(path.join(root, d));
    const CLAIM = /processed under EU law|stays in the EU|never leaves the EU|across borders/gi;
    let seen = 0;
    for (const f of files) {
      const src = readFileSync(f, "utf-8");
      for (const m of src.matchAll(CLAIM)) {
        seen++;
        const near = src.slice(Math.max(0, m.index - 150), m.index + 250);
        expect(near, `${path.relative(root, f)}: "${m[0]}" needs the AI exception nearby`).toMatch(/AI feature|AI provider/);
      }
    }
    expect(seen, "the scan must find the claims it guards").toBeGreaterThan(8);
  });

  it("guard: no em dashes in the FAQ answers (customer-facing copy)", async () => {
    for (const entry of await landingFaqJsonLd()) {
      expect(entry.acceptedAnswer.text).not.toMatch(/—/);
      expect(entry.name).not.toMatch(/—/);
    }
  });
});
