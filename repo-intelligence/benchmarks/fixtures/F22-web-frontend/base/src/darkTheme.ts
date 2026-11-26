// The dark storefront theme keeps the base brand colour.
import { theme } from "./theme";

export const darkTheme = {
  colors: {
    primary: theme.colors.primary,
    text: "#e6edf3",
    surface: "#0d1117",
  },
};

export const darkTokens = {
  link: darkTheme.colors.primary,
};
