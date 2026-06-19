import type { Metadata } from "next";
import { AuthProvider } from "@/components/auth/AuthProvider";
import { ThemeProvider } from "@/components/ThemeProvider";
import { TourProvider } from "@/components/tour/TourProvider";
import { siteDescription, siteName, siteTagline, siteUrl } from "@/lib/site";
import { readNonce } from "@/lib/nonce";
import { GoogleAnalytics } from "@/components/analytics/GoogleAnalytics";
import "./globals.css";

// Structural social-graph defaults only. Each public page must declare its
// own openGraph.{url,title,description} and twitter.{title,description} so
// unfurls of /login, /register, /privacy, /terms do not show landing copy.
export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: `${siteName}: ${siteTagline}`,
    template: `%s · ${siteName}`,
  },
  description: siteDescription,
  applicationName: siteName,
  openGraph: {
    type: "website",
    siteName,
    locale: "en_US",
  },
  twitter: {
    card: "summary_large_image",
  },
  // Safer default for an auth-walled SaaS: noindex by default.
  // The 7 indexable public pages (/, /login, /register, /privacy,
  // /terms, /docs, /docs/plans) opt back in via their own metadata.
  robots: {
    index: false,
    follow: false,
  },
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // The proxy generates a fresh per-request nonce and threads it via
  // the ``x-nonce`` request header (see ``frontend/proxy.ts``). Reading
  // it here forces dynamic rendering for the App Platform build, which
  // is required for nonce-based CSP per Next.js docs. The apex static
  // export uses ``next.config.apex.ts`` + CloudFront for its CSP, so
  // ``readNonce`` returns an empty string at apex build time and the
  // inline theme bootstrap below ships without a nonce attribute
  // (CloudFront's CSP doesn't require one).
  const nonce = await readNonce();
  const nonceProp = nonce ? { nonce } : {};
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <GoogleAnalytics nonce={nonce} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,400;0,9..144,500;0,9..144,600;0,9..144,700;1,9..144,400&family=Outfit:wght@300;400;500;600&display=swap"
          rel="stylesheet"
        />
        <script
          {...nonceProp}
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                var t = localStorage.getItem('tbd-theme');
                if (t === 'light') {
                  document.documentElement.setAttribute('data-theme', 'light');
                }
              })();
            `,
          }}
        />
      </head>
      <body className="min-h-screen">
        <ThemeProvider>
          <AuthProvider>
            <TourProvider>{children}</TourProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
