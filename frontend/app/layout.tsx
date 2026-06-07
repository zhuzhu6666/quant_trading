import type { Metadata, Viewport } from "next";
import { WSProvider } from "@/components/layout/ws-provider";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import { ServiceWorkerRegister } from "@/components/layout/sw-register";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quant Web Console",
  description: "XAUUSD+ trading framework — web console",
  manifest: "/manifest.json",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Quant Console",
  },
};

export const viewport: Viewport = {
  themeColor: "#0d1117",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <link rel="manifest" href="/manifest.json" />
        <meta name="theme-color" content="#0d1117" />
      </head>
      <body className="min-h-screen bg-bg text-fg">
        <ServiceWorkerRegister />
        <WSProvider>
          <div className="flex">
            <Sidebar />
            <div className="flex-1 min-w-0">
              <Topbar />
              <main className="max-w-[1600px] mx-auto p-6">{children}</main>
            </div>
          </div>
        </WSProvider>
      </body>
    </html>
  );
}
