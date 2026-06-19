import { GA_GATEWAY_PATH, GA_MEASUREMENT_ID, isApexBuild } from "@/lib/analytics";

export function GoogleAnalytics({ nonce }: { nonce?: string }) {
  if (!isApexBuild || !GA_MEASUREMENT_ID) return null;
  const nonceProp = nonce ? { nonce } : {};
  return (
    <>
      {/* Google tag gateway: the loader is served first-party from
          GA_GATEWAY_PATH (CloudFront proxies it to the fps.goog origin),
          not from googletagmanager.com. The inline config below is
          unchanged — it still configures the GA_MEASUREMENT_ID property. */}
      <script {...nonceProp} async src={GA_GATEWAY_PATH} />
      <script
        {...nonceProp}
        dangerouslySetInnerHTML={{
          __html: `window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('js', new Date());
gtag('config', '${GA_MEASUREMENT_ID}');`,
        }}
      />
    </>
  );
}
