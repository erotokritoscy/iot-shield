/** @type {import('tailwindcss').Config} */
// Design tokens live in `src/index.css` as CSS variables and are *consumed* here (brief §7):
// changing the design means changing a token, never scattering hex values through components.
// Channel triplets rather than hex so Tailwind's `<alpha-value>` opacity modifiers still work.
const token = (name) => `rgb(var(${name}) / <alpha-value>)`;

export default {
  darkMode: ["class", '[data-theme="dark"]'],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: token("--c-paper"),
        surface: token("--c-surface"),
        raised: token("--c-raised"),
        line: token("--c-line"),
        grid: token("--c-grid"),
        muted: token("--c-muted"),
        ink: token("--c-ink"),
        dim: token("--c-dim"),
        accent: {
          DEFAULT: token("--c-accent"),
          ink: token("--c-accent-ink"),
          soft: token("--c-accent-soft"),
        },
        critical: token("--c-critical"),
        warning: token("--c-warning"),
        success: token("--c-success"),
        info: token("--c-info"),
        sev: {
          low: token("--c-sev-low"),
          medium: token("--c-sev-medium"),
          high: token("--c-sev-high"),
          critical: token("--c-sev-critical"),
          emergency: token("--c-sev-emergency"),
        },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Consolas", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      borderRadius: {
        DEFAULT: "4px",
        md: "5px",
        lg: "7px",
      },
    },
  },
  plugins: [],
};
