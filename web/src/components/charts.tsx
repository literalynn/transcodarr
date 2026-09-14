/** Briques communes des graphiques (recharts) : jetons de couleur par rôle,
 *  axes et grille discrets, infobulle aux couleurs du thème, libellés des
 *  créneaux de temps renvoyés par l'API. */

import type { ReactNode } from "react";
import type { TimeUnit } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Lang } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export const CHART = {
  series: "hsl(var(--chart-1))",
  // Séries catégorielles : toujours dans cet ordre (paires validées)
  cat1: "hsl(var(--chart-1))",
  cat2: "hsl(var(--chart-2))",
  cat3: "hsl(var(--chart-3))",
  good: "hsl(var(--chart-good))",
  neutral: "hsl(var(--chart-neutral))",
  bad: "hsl(var(--chart-bad))",
  grid: "hsl(var(--chart-grid))",
  axis: "hsl(var(--chart-axis))",
  surface: "hsl(var(--card))",
} as const;

/** Props partagées des axes : traits fins, texte discret, sans trait d'axe Y. */
export const axisProps = {
  stroke: CHART.axis,
  fontSize: 11,
  tickLine: false,
  axisLine: { stroke: CHART.grid },
} as const;

export const gridProps = {
  stroke: CHART.grid,
  vertical: false,
} as const;

export const barCursor = { fill: "hsl(var(--accent))", opacity: 0.4 } as const;

// ---------------------------------------------------------------------------
// Créneaux de temps (UTC côté API)
// ---------------------------------------------------------------------------
function locale(lang: Lang): string {
  return lang === "fr" ? "fr-FR" : "en-US";
}

/** Graduation courte : « 14:00 » (heure locale) ou « 15/09 » (jour UTC). */
export function bucketTick(iso: string, unit: TimeUnit, lang: Lang): string {
  const d = new Date(iso);
  if (unit === "hour") {
    return d.toLocaleTimeString(locale(lang), { hour: "2-digit", minute: "2-digit" });
  }
  // Jour et semaine sont des dates UTC : pas de décalage vers la veille
  return d.toLocaleDateString(locale(lang), { day: "2-digit", month: "2-digit", timeZone: "UTC" });
}

/** Libellé complet d'un créneau pour l'infobulle. */
export function bucketLabel(iso: string, unit: TimeUnit, lang: Lang): string {
  const d = new Date(iso);
  const loc = locale(lang);
  if (unit === "hour") {
    const time = (x: Date) => x.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit" });
    const day = d.toLocaleDateString(loc, { weekday: "short", day: "numeric", month: "short" });
    return `${day}, ${time(d)} – ${time(new Date(d.getTime() + 3_600_000))}`;
  }
  const date = d.toLocaleDateString(loc, {
    weekday: unit === "day" ? "long" : undefined,
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  });
  return unit === "week" ? (lang === "fr" ? `Semaine du ${date}` : `Week of ${date}`) : date;
}

// ---------------------------------------------------------------------------
// Infobulles
// ---------------------------------------------------------------------------
export interface TooltipRow {
  label: ReactNode;
  value: ReactNode;
  color?: string;
}

/** Boîte d'infobulle : titre + lignes « pastille · libellé · valeur ». */
export function TooltipBox({ title, rows }: { title?: ReactNode; rows: TooltipRow[] }) {
  return (
    <div className="rounded-md border bg-card px-2.5 py-2 text-xs shadow-lg">
      {title !== undefined && <div className="mb-1 font-medium text-foreground">{title}</div>}
      <div className="space-y-0.5">
        {rows.map((row, i) => (
          <div key={i} className="flex items-center justify-between gap-4">
            <span className="flex items-center gap-1.5 whitespace-nowrap text-muted-foreground">
              {row.color && (
                <span className="inline-block h-2 w-2 rounded-sm" style={{ background: row.color }} />
              )}
              {row.label}
            </span>
            <span className="whitespace-nowrap font-mono text-foreground">{row.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

interface TooltipEntry {
  name?: string | number;
  value?: number | string;
  color?: string;
  dataKey?: string | number;
  payload?: Record<string, unknown>;
}

/** Infobulle générique branchée sur recharts (une ligne par série). */
export function ChartTooltip({
  active,
  payload,
  label,
  formatValue,
  formatLabel,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string | number;
  formatValue?: (value: number, entry: TooltipEntry) => string;
  formatLabel?: (label: string) => string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <TooltipBox
      title={label === undefined ? undefined : formatLabel ? formatLabel(String(label)) : label}
      rows={payload.map((entry) => ({
        label: entry.name,
        color: entry.color,
        value:
          typeof entry.value === "number" && formatValue
            ? formatValue(entry.value, entry)
            : entry.value,
      }))}
    />
  );
}

// ---------------------------------------------------------------------------
// Cartes et marques
// ---------------------------------------------------------------------------
export function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: color }} />
      {label}
    </span>
  );
}

export function ChartCard({
  title,
  action,
  children,
  className,
  bodyClassName,
}: {
  title: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
        <CardTitle>{title}</CardTitle>
        {action}
      </CardHeader>
      <CardContent className={cn("h-64", bodyClassName)}>{children}</CardContent>
    </Card>
  );
}

export function EmptyChart({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      {message}
    </div>
  );
}

/** Barre horizontale proportionnelle (listes classées : causes, progression…). */
export function InlineBar({
  value,
  max,
  color = CHART.series,
}: {
  value: number;
  max: number;
  color?: string;
}) {
  // Valeur non nulle toujours visible (2 % min.), valeur nulle : barre vide
  const pct = max > 0 && value > 0 ? Math.max(2, (value / max) * 100) : 0;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}
