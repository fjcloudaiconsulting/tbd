---
name: i18n and l10n Goals
description: User locale preferences — country drives date/number/timezone defaults. Number format is per-user, NOT per-currency.
type: project
---
User wants to add internationalization/localization features:

1. **Country preference** — user selects country, which suggests defaults for everything below
2. **Timezone support** — store/display dates in user's timezone, suggested from country but overridable
3. **Date format** — country-driven (DD/MM/YYYY, MM/DD/YYYY, etc.)
4. **Number format** — country-driven (1.000,00 vs 1,000.00 etc.)
5. **Language support** — multi-language UI (i18n)
6. **Multi-currency** — already partially supported (accounts have currency field), but needs proper currency conversion/display

**Key design decision:** Number format is a **single user-level preference**, NOT per-currency. Even though accounts can have different currencies, all amounts display in the same number format. Mixing formats (e.g., € 1.000,00 alongside $ 1,000.00) would make transaction lists inconsistent and confusing.

**Why:** Personal use across different contexts; the app should feel native regardless of locale.

**How to apply:** When building new features involving dates, amounts, or user-facing text, design with i18n/l10n in mind (e.g., use UTC internally, format dates/numbers at the display layer, keep UI strings extractable). All formatting happens client-side based on user preferences, not server-side.
