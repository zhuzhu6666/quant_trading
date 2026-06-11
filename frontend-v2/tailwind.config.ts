import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        glass: {
          DEFAULT: "rgba(255,255,255,0.6)",
          hover: "rgba(255,255,255,0.92)",
          border: "rgba(255,255,255,0.8)",
          "border-hover": "rgba(59,130,246,0.4)",
        },
        surface: { DEFAULT: "#f5f7fa", alt: "#e4e9f0" },
        fg: { DEFAULT: "#1a1e24", muted: "#7a7f8a", placeholder: "#a6abb4" },
        accent: { DEFAULT: "#3b82f6", hover: "#2563eb", muted: "rgba(59,130,246,0.12)" },
        up: { DEFAULT: "#16a34a", muted: "rgba(22,163,74,0.12)" },
        down: { DEFAULT: "#dc2626", muted: "rgba(220,38,38,0.12)" },
        warn: { DEFAULT: "#d97706", muted: "rgba(217,119,6,0.12)" },
        gradient: {
          blue: "linear-gradient(135deg, #3b82f6, #1d4ed8)",
          green: "linear-gradient(135deg, #16a34a, #15803d)",
          amber: "linear-gradient(135deg, #d97706, #b45309)",
          purple: "linear-gradient(135deg, #7c3aed, #5b21b6)",
          slate: "linear-gradient(135deg, #475569, #334155)",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        glass: "0 2px 8px rgba(0,0,0,0.04)",
        "glass-hover": "0 12px 40px rgba(59,130,246,0.15)",
        "gradient": "0 4px 16px rgba(59,130,246,0.25)",
      },
      animation: {
        "slide-down": "slideDown 0.35s cubic-bezier(0.34,1.56,0.64,1)",
        "slide-up": "slideUp 0.3s ease-out",
        "fade-in": "fadeIn 0.3s ease-out",
        "count-up": "countUp 0.3s ease both",
        "float": "float 3s ease-in-out infinite",
      },
      keyframes: {
        slideDown: {
          "0%": { transform: "translateY(-100%)", opacity: "0" },
          "100%": { transform: "translateY(0)", opacity: "1" },
        },
        slideUp: {
          "0%": { transform: "translateY(0)", opacity: "1" },
          "100%": { transform: "translateY(-100%)", opacity: "0" },
        },
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        countUp: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        float: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-6px)" },
        },
      },
    },
  },
  plugins: [],
};
export default config;
