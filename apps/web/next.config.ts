import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Container builds (infra/docker/web/Dockerfile) emit a self-contained server. Local
  // `next build` / `next start` are unchanged.
  output: process.env.REPOLENS_WEB_STANDALONE === "1" ? "standalone" : undefined,
};

export default config;
