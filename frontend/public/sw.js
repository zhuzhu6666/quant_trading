// sw.js — service worker scaffold for Quant Web Console.
//
// SCAFFOLD ONLY: this worker only logs lifecycle events. No offline caching,
// no fetch interception, no background sync. Future work would add:
//   - Cache API for /_next/static/* (already aggressively cached by nginx)
//   - Stale-while-revalidate for /api/market/bars
//   - Background sync for /api/paper/start (queue when offline)
//
// Per spec §7.4: PWA is "optional, responsive web is enough" for v1. This
// file is committed as a placeholder so future contributors know where to
// add offline support.

self.addEventListener("install", (event) => {
  console.log("[sw] install");
  // Don't skipWaiting() — let the new version take over only after user reloads
});

self.addEventListener("activate", (event) => {
  console.log("[sw] activate");
});

self.addEventListener("fetch", (event) => {
  // Pass-through (no caching). v2: add Cache API for static assets.
  return;
});
