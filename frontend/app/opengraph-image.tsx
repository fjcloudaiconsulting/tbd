import { ImageResponse } from "next/og";
import { siteDescription, siteName } from "@/lib/site";

export const alt = `${siteName}: know your money, plan what's next`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

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
          background:
            "linear-gradient(135deg, #070d18 0%, #0B1F3A 55%, #122a4a 100%)",
          color: "#E6EAF0",
          fontFamily: "serif",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "18px",
            fontSize: 22,
            letterSpacing: "0.18em",
            textTransform: "uppercase",
            color: "#D4A64A",
            fontFamily: "sans-serif",
            fontWeight: 600,
          }}
        >
          <div
            style={{
              width: 40,
              height: 40,
              borderRadius: 10,
              background: "#D4A64A",
              color: "#0B1F3A",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 24,
              fontWeight: 700,
              fontFamily: "sans-serif",
            }}
          >
            TBD
          </div>
          {siteName}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "28px" }}>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              fontSize: 88,
              fontWeight: 600,
              lineHeight: 1.05,
              letterSpacing: "-0.02em",
              color: "#E6EAF0",
            }}
          >
            <div style={{ display: "flex" }}>There&rsquo;s no best decision.</div>
            <div style={{ display: "flex", color: "#D4A64A" }}>
              Only better ones.
            </div>
          </div>
          <div
            style={{
              display: "flex",
              fontSize: 28,
              lineHeight: 1.4,
              color: "#9ba8bd",
              fontFamily: "sans-serif",
              maxWidth: 900,
            }}
          >
            {siteDescription}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end",
            fontFamily: "sans-serif",
            fontSize: 22,
            color: "#5a6a82",
          }}
        >
          <div>thebetterdecision.com</div>
          <div style={{ color: "#D4A64A", fontWeight: 500 }}>
            14-day free trial
          </div>
        </div>
      </div>
    ),
    size,
  );
}
