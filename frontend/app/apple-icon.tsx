import { ImageResponse } from "next/og";
import {
  BRAND_BRASS,
  BRAND_INK,
  BRAND_SLATE,
} from "@/lib/styles";

// Next.js App Router file convention. Generates the 180×180 PNG that
// iOS uses for "Add to home screen". Mirrors `icon.svg` visually so a
// device that prefers the apple-touch-icon and a device that prefers
// the SVG favicon see the same mark.
//
// We intentionally do NOT add a corresponding `icon.tsx` to keep the
// dev-time bundle small — `icon.svg` is sized at the 32px favicon point
// and scales crisply on every browser surface. The apple-icon must be
// PNG (iOS does not honor SVG apple-touch-icons), so this file exists.

export const size = { width: 180, height: 180 };
export const contentType = "image/png";

export default function AppleIcon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: BRAND_INK,
          // iOS draws its own rounded corners on apple-touch-icons,
          // so we render a square fill and let the OS mask.
        }}
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="120"
          height="120"
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
      </div>
    ),
    size,
  );
}
