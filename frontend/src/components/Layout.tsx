import { NavLink, Outlet } from "react-router-dom";
import { useTheme, type ThemePreference } from "../hooks/useTheme";

const THEME_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

// V4 product consolidation (2026-09-02) -- information architecture.
//
// The product is now V4-first: the decision engine and its forward test are
// the primary surfaces. V4-only reset (2026-09-02): every retired surface is
// gone from the router, so nothing here can link to a dead route; it
// simply no longer competes with V4 for the top of the navigation.
//
// Every entry here points at a route that actually exists in App.tsx.

type NavItem = { to: string; label: string; end?: boolean };

const HOME_NAV_ITEMS: NavItem[] = [{ to: "/", label: "Dashboard", end: true }];

const RESEARCH_NAV_ITEMS: NavItem[] = [
  { to: "/search", label: "Company Search", end: true },
  { to: "/research", label: "AI Research" },
];

// The decision engine is the product. V4 leads.
const DECISION_NAV_ITEMS: NavItem[] = [
  { to: "/v4-decision-lab", label: "V4 Decision Lab" },
  { to: "/candidate-explorer", label: "Candidate Explorer" },
];

// The V4 forward test is the only evidence the product keeps.
const PERFORMANCE_NAV_ITEMS: NavItem[] = [
  { to: "/v4-shadow-track-record", label: "V4 Forward Track Record" },
];

const OPERATIONS_NAV_ITEMS: NavItem[] = [{ to: "/operations", label: "Live Operations" }];

const SETTINGS_NAV_ITEMS: NavItem[] = [
  { to: "/settings/providers", label: "Data Providers" },
  { to: "/settings/ai-provider", label: "AI Provider" },
  { to: "/settings/ibkr", label: "IBKR / TWS" },
  { to: "/settings/usage", label: "API Usage" },
  { to: "/system-status", label: "System Status" },
];


const NAV_SECTIONS: { heading: string | null; items: NavItem[] }[] = [
  { heading: null, items: HOME_NAV_ITEMS },
  { heading: "Research", items: RESEARCH_NAV_ITEMS },
  { heading: "Decision Engine", items: DECISION_NAV_ITEMS },
  { heading: "Performance", items: PERFORMANCE_NAV_ITEMS },
  { heading: "Operations", items: OPERATIONS_NAV_ITEMS },
  { heading: "Settings", items: SETTINGS_NAV_ITEMS },
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
        {NAV_SECTIONS.map((section) => (
          <div key={section.heading ?? "home"}>
            {section.heading && (
              <div className="sidebar-nav-heading">{section.heading}</div>
            )}
            <nav className="sidebar-nav">
              {section.items.map((item) => (
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
          </div>
        ))}
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
