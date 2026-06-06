import { BRAND_NAME } from "@/lib/brand";

// AnswerLead — a question-style lead block placed directly under the Hero.
// Tuned for answer surfaces (Google featured snippets, People Also Ask,
// AI Overviews) and generative-engine citation (ChatGPT, Perplexity,
// Claude, Gemini): a literal "What is ...?" heading followed by a single
// self-contained sentence that answers it without needing the rest of the
// page for context. The lead answer is kept near snippet length (~50 words).
//
// No em-dashes in customer copy (locked policy feedback_no_em_dashes).
export default function AnswerLead() {
  return (
    <section
      aria-labelledby="what-is-heading"
      className="mx-auto max-w-3xl px-6 pb-2 pt-6 lg:pt-8"
    >
      <h2
        id="what-is-heading"
        className="font-display text-2xl font-semibold leading-tight text-text-primary lg:text-3xl"
      >
        What is {BRAND_NAME}?
      </h2>
      <p className="mt-4 text-base leading-relaxed text-text-secondary lg:text-lg">
        {BRAND_NAME} is a personal finance app for individuals and households
        that brings your accounts, transactions, budgets, and forecasts into
        one calm view. It shows what you have, what is coming, and where your
        money goes, so you can decide on the same page you read, without
        spreadsheet fatigue.
      </p>
      <p className="mt-4 text-base leading-relaxed text-text-secondary lg:text-lg">
        It is free while in beta, EU-hosted, and runs in any browser. You can
        import a CSV from your bank or add transactions by hand, with no bank
        connection required to get started.
      </p>
    </section>
  );
}
