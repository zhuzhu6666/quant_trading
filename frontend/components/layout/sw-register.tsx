"use client";
import { useEffect } from "react";

/** Register the service worker. Per spec §7.4 PWA is "scaffold only" in v1. */
export function ServiceWorkerRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;
    // Only register in production (Next.js dev mode HMR + SW don't mix well)
    if (process.env.NODE_ENV !== "production") return;
    navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .then((reg) => {
        console.log("[sw] registered scope=", reg.scope);
      })
      .catch((err) => {
        console.warn("[sw] registration failed:", err);
      });
  }, []);
  return null;
}
