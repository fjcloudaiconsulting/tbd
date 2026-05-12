import { ImageResponse } from "next/og";
import {
  BRAND_BRASS,
  BRAND_FOG,
  BRAND_INK,
  BRAND_INK_DEEP,
  BRAND_INK_RAISED,
  BRAND_PARCHMENT,
  BRAND_SLATE,
} from "@/lib/brand";
import { siteDescription, siteName } from "@/lib/site";

export const alt = `${siteName}: know your money, plan what's next`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// 1200x630 social-share image. Pinned to the brand ground regardless of
// the visitor's theme — see BRAND.md "Brand surface" rules.
//
// Layout:
//   ┌────────────────────────────────────────────────────────────┐
//   │ [mark] THE BETTER DECISION                                 │
//   │                                                            │
//   │  There's no best decision.                                 │
//   │  Only better ones.                                         │
//   │                                                            │
//   │  <description>                                             │
//   │                                                            │
//   │  thebetterdecision.com           Personal finance, calmer  │
//   └────────────────────────────────────────────────────────────┘
export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          height: "100%",
          width: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "80px",
          background: `linear-gradient(135deg, ${BRAND_INK_DEEP} 0%, ${BRAND_INK} 55%, ${BRAND_INK_RAISED} 100%)`,
          color: BRAND_PARCHMENT,
          fontFamily: "serif",
        }}
      >
        {/* Top: mark + wordmark lockup */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "20px",
            fontSize: 26,
            letterSpacing: "0.16em",
            textTransform: "uppercase",
            color: BRAND_BRASS,
            fontFamily: "sans-serif",
            fontWeight: 600,
          }}
        >
          {/* Mark: two stacked chevrons, brass over slate echo */}
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="56"
            height="56"
            viewBox="0 0 32 32"
          >
            <path
              d="M 9 8 L 18 16 L 9 24"
              fill="none"
              stroke={BRAND_SLATE}
              strokeWidth={2.5}
              strokeLinecap="round"
              strokeLinejoin="round"
              opacity={0.55}
            />
            <path
              d="M 14 8 L 23 16 L 14 24"
              fill="none"
              stroke={BRAND_BRASS}
              strokeWidth={2.5}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          {siteName}
        </div>

        {/* Middle: locked tagline + description */}
        <div style={{ display: "flex", flexDirection: "column", gap: "28px" }}>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              fontSize: 88,
              fontWeight: 600,
              lineHeight: 1.05,
              letterSpacing: "-0.02em",
              color: BRAND_PARCHMENT,
            }}
          >
            <div style={{ display: "flex" }}>There&rsquo;s no best decision.</div>
            <div style={{ display: "flex", color: BRAND_BRASS }}>
              Only better ones.
            </div>
          </div>
          <div
            style={{
              display: "flex",
              fontSize: 28,
              lineHeight: 1.4,
              color: BRAND_FOG,
              fontFamily: "sans-serif",
              maxWidth: 900,
            }}
          >
            {siteDescription}
          </div>
        </div>

        {/* Bottom: domain + positioning line. Replaces the prior
            "14-day free trial" badge which contradicts current beta
            plan logic (see project_status memory 2980, 2026-04-23). */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end",
            fontFamily: "sans-serif",
            fontSize: 22,
            color: BRAND_SLATE,
          }}
        >
          <div>thebetterdecision.com</div>
          <div style={{ color: BRAND_BRASS, fontWeight: 500 }}>
            Personal finance, calmer.
          </div>
        </div>
      </div>
    ),
    size,
  );
}
