import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": __dirname + "/src" } },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("error", () => {});  // 静默 ECONNABORTED 等 Vite 代理噪音
        },
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
        configure: (proxy) => {
          proxy.on("error", () => {});  // 静默 WS 断开噪音 (页面切换/刷新)
          proxy.on("proxyReqWs", (_: any, req: any) => {
            req.on("error", () => {});  // 请求级错误也静默
          });
        },
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
