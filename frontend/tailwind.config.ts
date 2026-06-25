import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Warm-tinted dark trading palette — deliberately not the generic slate/indigo default.
        ink: {
          950: "#0b0c0e",
          900: "#121317",
          850: "#181a1f",
          800: "#23262d",
          700: "#2e323b",
        },
        gain: "#34d399",
        loss: "#fb7185",
        flat: "#fbbf24",
        brass: "#e0b34d",
      },
      fontFamily: {
        serif: ["Georgia", "Cambria", "Times New Roman", "serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
