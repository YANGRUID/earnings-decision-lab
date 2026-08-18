import { useEffect, useState } from "react";

export type ThemePreference = "light" | "dark" | "system";

const STORAGE_KEY = "ecl-theme";

function isThemePreference(value: string | null): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

function applyTheme(pref: ThemePreference) {
  const root = document.documentElement;
  if (pref === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", pref);
  }
}

/** Explicit light/dark/system choice, persisted in localStorage and applied
 * via a `data-theme` attribute that overrides the prefers-color-scheme
 * media query in index.css (see index.html for the pre-paint apply that
 * avoids a flash of the wrong theme). */
export function useTheme(): [ThemePreference, (pref: ThemePreference) => void] {
  const [theme, setThemeState] = useState<ThemePreference>(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  });

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const setTheme = (pref: ThemePreference) => {
    localStorage.setItem(STORAGE_KEY, pref);
    setThemeState(pref);
  };

  return [theme, setTheme];
}
