// Design tokens for the storefront. Components read them through `theme`.
export const theme = {
  colors: {
    primary: "#1f6feb",
    danger: "#d1242f",
    text: "#1f2328",
    surface: "#ffffff",
  },
  space: [0, 4, 8, 16, 32],
  radii: { sm: "4px", md: "8px" },
  fonts: { body: "Inter, system-ui, sans-serif", mono: "ui-monospace, monospace" },
  fontSizes: { body: "14px", heading: "24px" },
  shadows: { card: "0 1px 3px rgba(0, 0, 0, 0.12)" },
} as const;

export type Theme = typeof theme;
