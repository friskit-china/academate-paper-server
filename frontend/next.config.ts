import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Next 16's server can resolve the ESM side of this transitive package dynamically even when
  // output-file tracing observed only its CJS entry points. Keep the include narrow and let the
  // locked pnpm tree remain the dependency authority.
  outputFileTracingIncludes: {
    "/*": [
      "./node_modules/.pnpm/@swc+helpers@*/node_modules/@swc/helpers/esm/**/*",
    ],
  },
  poweredByHeader: false,
};

export default nextConfig;
