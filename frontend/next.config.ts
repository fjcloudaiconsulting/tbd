import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  logging: {
    fetches: {
      fullUrl: false,
    },
  },
  serverExternalPackages: ["pino", "pino-pretty"],
};

export default nextConfig;
