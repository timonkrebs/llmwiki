import type { NextConfig } from "next";
import path from "path";
import { withSentryConfig } from "@sentry/nextjs";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname),
  webpack: (config) => {
    // Ignore node-specific modules when bundling for the browser
    // See https://huggingface.co/docs/transformers.js/tutorials/next#step-2-install-and-configure-transformersjs
    config.resolve.alias = {
      ...config.resolve.alias,
      "sharp$": false,
      "onnxruntime-node$": false,
    }
    return config;
  },
};

export default withSentryConfig(nextConfig, {
  silent: true,
  disableLogger: true,
});
