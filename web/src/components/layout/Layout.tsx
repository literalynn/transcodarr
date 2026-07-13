import {
  Activity,
  BarChart3,
  Cpu,
  FolderOpen,
  History,
  LayoutDashboard,
  ListOrdered,
  Moon,
  Sun,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useHealth } from "@/api/hooks";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/queue", label: "Queue", icon: ListOrdered },
  { to: "/history", label: "Historique", icon: History },
  { to: "/stats", label: "Statistiques", icon: BarChart3 },
  { to: "/libraries", label: "Bibliothèques", icon: FolderOpen },
  { to: "/settings", label: "GPU & Réglages", icon: Cpu },
];

export function Layout() {
  const { data: health } = useHealth();
  const [dark, setDark] = useState(
    () => localStorage.getItem("theme") !== "light",
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("theme", dark ? "dark" : "light");
  }, [dark]);

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-52 shrink-0 flex-col border-r bg-card">
        <div className="flex items-center gap-2 border-b px-4 py-3">
          <Activity className="h-5 w-5 text-primary" />
          <div>
            <div className="text-sm font-bold leading-tight">Transcodeur</div>
            <div className="font-mono text-[10px] text-muted-foreground">jftranscoder-v1</div>
          </div>
        </div>
        <nav className="flex-1 space-y-0.5 p-2">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                  isActive && "bg-accent font-medium text-foreground",
                )
              }
            >
              <Icon className="h-4 w-4" />
              {label}
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
              {health?.database ? "en ligne" : "hors ligne"}
            </span>
            <button
              onClick={() => setDark((d) => !d)}
              className="rounded p-1 hover:bg-accent"
              title="Basculer le thème"
            >
              {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
            </button>
          </div>
          <div className="mt-1 font-mono">{health?.workers ?? 0} worker(s)</div>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Badge DRY RUN impossible à rater (§9.4) */}
        {health?.dry_run_global && (
          <div className="flex items-center justify-center gap-2 bg-warning px-4 py-1.5 text-sm font-bold uppercase tracking-widest text-black">
            ⚠ DRY RUN — aucun fichier original ne sera remplacé
          </div>
        )}
        <main className="min-w-0 flex-1 p-5">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
