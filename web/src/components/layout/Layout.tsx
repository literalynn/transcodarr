import {
  Activity,
  Cpu,
  FolderOpen,
  LayoutDashboard,
  ListOrdered,
  Menu,
  Moon,
  Sun,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useHealth } from "@/api/hooks";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/queue", label: "Queue", icon: ListOrdered },
  { to: "/libraries", label: "Bibliothèques", icon: FolderOpen },
  { to: "/settings", label: "GPU & Réglages", icon: Cpu },
];

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const { data: health } = useHealth();
  const { t } = useI18n();
  const [dark, setDark] = useState(() => localStorage.getItem("theme") !== "light");
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("theme", dark ? "dark" : "light");
  }, [dark]);

  return (
    <>
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <Activity className="h-5 w-5 text-primary" />
        <div>
          <div className="text-sm font-bold leading-tight">Transcodarr</div>
          <div className="font-mono text-[10px] text-muted-foreground">
            v{health?.version ?? "…"}
            {health?.build_date ? ` · ${health.build_date}` : ""}
          </div>
        </div>
      </div>
      <nav className="flex-1 space-y-0.5 p-2">
        {NAV.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                isActive && "bg-accent font-medium text-foreground",
              )
            }
          >
            <Icon className="h-4 w-4" />
            {t(label)}
          </NavLink>
        ))}
      </nav>
      <div className="border-t p-3 text-xs text-muted-foreground">
        <div className="flex items-center justify-between">
          <span
            className={cn(
              "flex items-center gap-1.5",
              health?.database ? "text-success" : "text-destructive",
            )}
          >
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-current" />
            {health?.database ? t("en ligne") : t("hors ligne")}
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setDark((d) => !d)}
              className="rounded p-1 hover:bg-accent"
              title={t("Basculer le thème")}
            >
              {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
            </button>
          </div>
        </div>
        <div className="mt-1 font-mono">
          {health?.workers ?? 0} {t("worker(s)")}
        </div>
      </div>
    </>
  );
}

export function Layout() {
  const { data: health } = useHealth();
  const { t } = useI18n();
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="flex min-h-screen">
      {/* Sidebar desktop */}
      <aside className="sticky top-0 hidden h-screen w-52 shrink-0 flex-col border-r bg-card md:flex">
        <SidebarContent />
      </aside>

      {/* Drawer mobile */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div className="absolute inset-0 bg-black/60" onClick={() => setMobileOpen(false)} />
          <aside className="absolute left-0 top-0 flex h-full w-64 flex-col border-r bg-card">
            <button
              className="absolute right-2 top-2 rounded p-1.5 text-muted-foreground hover:bg-accent"
              onClick={() => setMobileOpen(false)}
            >
              <X className="h-4 w-4" />
            </button>
            <SidebarContent onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Barre mobile */}
        <div className="flex items-center gap-2 border-b bg-card px-3 py-2 md:hidden">
          <button
            className="rounded p-1.5 text-muted-foreground hover:bg-accent"
            onClick={() => setMobileOpen(true)}
          >
            <Menu className="h-5 w-5" />
          </button>
          <Activity className="h-4 w-4 text-primary" />
          <span className="text-sm font-bold">Transcodarr</span>
        </div>

        {/* Badge DRY RUN impossible à rater (§9.4) */}
        {health?.dry_run_global && (
          <div className="flex items-center justify-center gap-2 bg-warning px-4 py-1.5 text-center text-xs font-bold uppercase tracking-widest text-black sm:text-sm">
            {t("⚠ DRY RUN — aucun fichier original ne sera remplacé")}
          </div>
        )}
        <main className="min-w-0 flex-1 p-3 sm:p-5">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
