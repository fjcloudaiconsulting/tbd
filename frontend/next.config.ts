import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  logging: {
    fetches: {
      fullUrl: false,
    },
  },
};

export default nextConfig;
