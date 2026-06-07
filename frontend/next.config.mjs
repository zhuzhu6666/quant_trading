/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
      // Note: WebSocket routes are not proxied via Next.js rewrites
      // (Next 14.2 rejects `ws://` destinations). The frontend
      // `lib/ws.ts` will connect directly to ws://localhost:8000/ws/*.
    ];
  },
};
export default nextConfig;
