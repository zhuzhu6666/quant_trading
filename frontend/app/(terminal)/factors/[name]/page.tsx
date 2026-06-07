// Server component wrapper for the dynamic /factors/[name] route.
// generateStaticParams must live in a server component. We return one
// placeholder route just to satisfy next.js's "output: export" check
// (it requires >=1 prerenderRoutes). The actual page is fully client-side,
// and any /factors/<name> URL at runtime is served via the FastAPI
// SPA fallback to index.html.
import FactorDetailClient from "./factor-detail-client";

export function generateStaticParams() {
  return [{ name: "_" }];
}

export default function FactorDetailPage() {
  return <FactorDetailClient />;
}
