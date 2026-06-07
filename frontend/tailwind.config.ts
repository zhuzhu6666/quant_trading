import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: { DEFAULT: "#0d1117", card: "#161b22", border: "#30363d" },
        fg: { DEFAULT: "#c9d1d9", muted: "#8b949e" },
        accent: "#58a6ff",
        up: "#3fb950",
        down: "#f85149",
        warn: "#d2991d",
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
