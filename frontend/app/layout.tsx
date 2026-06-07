import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quant Web Console",
  description: "XAUUSD+ trading framework — web console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-bg text-fg">{children}</body>
    </html>
  );
}
