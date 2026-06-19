import { GA_MEASUREMENT_ID, isApexBuild } from "@/lib/analytics";

export function GoogleAnalytics({ nonce }: { nonce?: string }) {
  if (!isApexBuild || !GA_MEASUREMENT_ID) return null;
  const nonceProp = nonce ? { nonce } : {};
  return (
    <>
      <script
        {...nonceProp}
        async
        src={`https://www.googletagmanager.com/gtag/js?id=${GA_MEASUREMENT_ID}`}
      />
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
