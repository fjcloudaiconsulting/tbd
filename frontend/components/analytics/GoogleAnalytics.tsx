import { GA_GATEWAY_PATH, GA_MEASUREMENT_ID, isApexBuild } from "@/lib/analytics";
import {
  CONSENT_STORAGE_KEY,
  CONSENT_TTL_MS,
  DEFAULT_DENIED,
} from "@/lib/consent";

// Serialize the default-denied Consent Mode object with single-quoted keys so
// the bootstrap reads like hand-written gtag and stays the single source of
// truth (lib/consent.ts) shared with the React banner.
const defaultConsentLiteral = Object.entries(DEFAULT_DENIED)
  .map(([k, v]) => `'${k}': '${v}'`)
  .join(", ");

// Inline bootstrap: set Consent Mode v2 defaults (everything non-essential
// denied) BEFORE gtag('config'), then synchronously re-apply a previously
// stored, non-expired choice so returning consenters are measured on first
// paint. The React banner (ConsentBanner.tsx) is what records that choice.
// GA runs in cookieless modeled mode until analytics_storage is granted.
const consentBootstrap = `window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('consent', 'default', { ${defaultConsentLiteral}, 'wait_for_update': 500 });
try {
  var raw = localStorage.getItem('${CONSENT_STORAGE_KEY}');
  if (raw) {
    var c = JSON.parse(raw);
    if (c && typeof c.analytics === 'boolean' && typeof c.marketing === 'boolean' && typeof c.ts === 'number' && (Date.now() - c.ts) <= ${CONSENT_TTL_MS}) {
      gtag('consent', 'update', {
        'analytics_storage': c.analytics ? 'granted' : 'denied',
        'ad_storage': c.marketing ? 'granted' : 'denied',
        'ad_user_data': c.marketing ? 'granted' : 'denied',
        'ad_personalization': c.marketing ? 'granted' : 'denied'
      });
    }
  }
} catch (e) {}
gtag('js', new Date());
gtag('config', '${GA_MEASUREMENT_ID}');`;

export function GoogleAnalytics({ nonce }: { nonce?: string }) {
  if (!isApexBuild || !GA_MEASUREMENT_ID) return null;
  const nonceProp = nonce ? { nonce } : {};
  return (
    <>
      {/* Consent Mode v2 default + stored-choice replay. Runs before the
          loader's config so collection is gated until consent. */}
      <script {...nonceProp} dangerouslySetInnerHTML={{ __html: consentBootstrap }} />
      {/* Google tag gateway: the loader is served first-party from
          GA_GATEWAY_PATH (CloudFront proxies it to the fps.goog origin),
          not from googletagmanager.com. */}
      <script {...nonceProp} async src={GA_GATEWAY_PATH} />
    </>
  );
}
