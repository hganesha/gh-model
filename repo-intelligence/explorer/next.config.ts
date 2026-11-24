import type { NextConfig } from "next";
import path from "node:path";

const config: NextConfig = {
  reactStrictMode: true,
  // A self-contained server for the container image (see Dockerfile); `next dev` is unaffected.
  output: "standalone",
  // The replay tests build a production server next to a running `next dev`; they must not share a folder.
  distDir: process.env.RKP_NEXT_DIST_DIR || ".next",
  poweredByHeader: false,
  // The explorer lives inside a larger repository; keep file tracing rooted here.
  outputFileTracingRoot: path.resolve(__dirname),
  turbopack: { root: path.resolve(__dirname) },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default config;
