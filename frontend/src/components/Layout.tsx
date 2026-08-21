import { NavLink, Outlet } from "react-router-dom";
import { useTheme, type ThemePreference } from "../hooks/useTheme";

const THEME_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

const NAV_ITEMS = [
  { to: "/", label: "Search", end: true },
  { to: "/research", label: "AI Research" },
  { to: "/historical-replay", label: "Cross-Company Replay" },
  { to: "/track-record", label: "AI Track Record" },
  { to: "/benchmark-track-record", label: "Benchmark Track Record" },
];

const SETTINGS_NAV_ITEMS = [
  { to: "/settings/providers", label: "Data Providers" },
  { to: "/settings/ai-provider", label: "AI Provider" },
  { to: "/settings/ibkr", label: "IBKR" },
  { to: "/settings/usage", label: "API Usage" },
  { to: "/system-status", label: "System Status" },
];

export function Layout() {
  const [theme, setTheme] = useTheme();
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          Earnings Decision Lab
          <small>Research &amp; analytics</small>
        </div>
        <nav className="sidebar-nav">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-nav-heading">Settings</div>
        <nav className="sidebar-nav">
          {SETTINGS_NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div style={{ flex: 1 }} />
        <div className="sidebar-nav-heading">Theme</div>
        <div className="theme-toggle">
          {THEME_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              className={theme === opt.value ? "active" : ""}
              onClick={() => setTheme(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </aside>
      <main className="main-content">
        <Outlet />
      </main>
    </div>
  );
}
