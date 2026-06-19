import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Apple-style neutral palette
        apple: {
          bg: "#F5F5F7",
          surface: "#FFFFFF",
          "surface-raised": "#FAFAFA",
          border: "rgba(0,0,0,0.06)",
          "border-strong": "rgba(0,0,0,0.1)",
          divider: "rgba(0,0,0,0.04)",
        },
        // Text colors
        text: {
          primary: "#1D1D1F",
          secondary: "#86868B",
          tertiary: "#D2D2D7",
          inverse: "#FFFFFF",
        },
        // Accent — Apple blue
        accent: {
          DEFAULT: "#0071E3",
          hover: "#0077ED",
          pressed: "#0068D1",
          light: "#E8F4FD",
          lighter: "#F5FAFF",
        },
        // Semantic
        success: {
          DEFAULT: "#34C759",
          light: "#E8F5E9",
        },
        danger: {
          DEFAULT: "#FF3B30",
          light: "#FFEBEE",
        },
        warning: {
          DEFAULT: "#FF9500",
          light: "#FFF3E0",
        },
        info: {
          DEFAULT: "#5AC8FA",
          light: "#E3F2FD",
        },
        // Glass effect
        glass: {
          DEFAULT: "rgba(255,255,255,0.72)",
          border: "rgba(255,255,255,0.3)",
          hover: "rgba(255,255,255,0.88)",
        },
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"SF Pro Text"',
          '"Segoe UI"',
          'Roboto',
          'Helvetica',
          'Arial',
          'sans-serif',
        ],
        mono: [
          '"SF Mono"',
          'SFMono-Regular',
          'ui-monospace',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
      fontSize: {
        "2xs": ["11px", { lineHeight: "1.4", letterSpacing: "0.01em" }],
        xs: ["12px", { lineHeight: "1.5", letterSpacing: "0" }],
        sm: ["13px", { lineHeight: "1.5", letterSpacing: "0" }],
        base: ["14px", { lineHeight: "1.5", letterSpacing: "0" }],
        lg: ["17px", { lineHeight: "1.4", letterSpacing: "-0.01em" }],
        xl: ["22px", { lineHeight: "1.3", letterSpacing: "-0.02em" }],
        "2xl": ["28px", { lineHeight: "1.2", letterSpacing: "-0.02em" }],
      },
      borderRadius: {
        "2xl": "16px",
        "3xl": "20px",
        "4xl": "24px",
      },
      boxShadow: {
        "apple-sm": "0 1px 2px rgba(0,0,0,0.04)",
        "apple-md": "0 2px 8px rgba(0,0,0,0.06)",
        "apple-lg": "0 8px 24px rgba(0,0,0,0.08)",
        "apple-xl": "0 12px 40px rgba(0,0,0,0.12)",
        "card": "0 0 0 1px rgba(0,0,0,0.04), 0 2px 12px rgba(0,0,0,0.06)",
        "card-hover": "0 0 0 1px rgba(0,0,0,0.04), 0 8px 24px rgba(0,0,0,0.10)",
        "card-active": "0 0 0 1px rgba(0,0,0,0.06), 0 1px 4px rgba(0,0,0,0.04)",
        "glass": "0 8px 32px rgba(0,0,0,0.08)",
        "inset": "inset 0 0 0 1px rgba(0,0,0,0.06)",
        "inset-accent": "inset 0 0 0 1px rgba(0,113,227,0.3)",
      },
      spacing: {
        "18": "4.5rem",
        "22": "5.5rem",
      },
      transitionTimingFunction: {
        "apple": "cubic-bezier(0.25, 0.1, 0.25, 1.0)",
        "apple-spring": "cubic-bezier(0.32, 0.72, 0, 1)",
        "apple-bounce": "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
      transitionDuration: {
        "350": "350ms",
        "450": "450ms",
      },
      animation: {
        "fade-in": "fadeIn 0.4s ease-out",
        "slide-up": "slideUp 0.4s ease-out",
        "slide-down": "slideDown 0.4s ease-out",
        "pulse-soft": "pulseSoft 2s ease-in-out infinite",
        "count-up": "countUp 0.5s ease-out",
        "float": "float 3s ease-in-out infinite",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        slideDown: {
          "0%": { opacity: "0", transform: "translateY(-12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.5" },
        },
        countUp: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        float: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-4px)" },
        },
      },
      backdropBlur: {
        xs: "2px",
      },
    },
  },
  plugins: [],
};

export default config;
