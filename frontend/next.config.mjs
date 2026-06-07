/** @type {import('next').NextConfig} */
const isProd = process.env.NODE_ENV === "production" || process.env.NEXT_BUILD_TARGET === "static";

const nextConfig = {
  reactStrictMode: true,
  // In prod (when start-prod is used), export static HTML to backend/static/
  // In dev (npm run dev), keep server runtime so HMR works
  ...(isProd ? { output: "export", images: { unoptimized: true } } : {}),
  // Dev mode proxies /api/* to backend on :8000 (port differs)
  // Prod mode: both API and frontend on :8000, so no rewrite needed
  ...(isProd
    ? {}
    : {
        async rewrites() {
          return [
            { source: "/api/:path*", destination: "http://localhost:8000/api/:path*" },
            // Note: WebSocket routes are not proxied via Next.js rewrites
            // (Next 14.2 rejects `ws://` destinations). The frontend
            // `lib/ws.ts` will connect directly to ws://localhost:8000/ws/*.
          ];
        },
      }),
};
export default nextConfig;
