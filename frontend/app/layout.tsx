import type { Metadata } from "next";
import { WSProvider } from "@/components/layout/ws-provider";
import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quant Web Console",
  description: "XAUUSD+ trading framework — web console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-bg text-fg">
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
