import { Moon, Sun } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/controls";

type Theme = "light" | "dark";

const STORAGE_KEY = "iotshield-theme";

function current(): Theme {
  if (typeof document === "undefined") return "light";
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

/**
 * Light and dark are both first-class (X6): every colour is a token, and the toggle simply
 * swaps which block of tokens `:root` resolves to. The choice is written to localStorage and
 * re-applied by an inline script in `index.html` before first paint, so a reload never
 * flashes the wrong ramp.
 */
export function ThemeToggle() {
  const [theme, setTheme] = React.useState<Theme>(current);

  const apply = (next: Theme) => {
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // A blocked localStorage is not a reason to refuse to change theme.
    }
    setTheme(next);
  };

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => apply(theme === "dark" ? "light" : "dark")}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
    >
      {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
      <span className="sr-only">Toggle theme</span>
    </Button>
  );
}

/** The current theme, for the chart palette (a Vega spec cannot read a CSS variable). */
export function useTheme(): Theme {
  const [theme, setTheme] = React.useState<Theme>(current);
  React.useEffect(() => {
    const target = document.documentElement;
    const observer = new MutationObserver(() => setTheme(current()));
    observer.observe(target, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}
