/** Dashboard unifié (§7.1 + §7.4) : activité en direct, indicateurs clés,
 *  fiabilité des réencodages et causes d'échec, économies, répartitions,
 *  progression, ressources. Remplace l'ancien couple Dashboard / Statistiques.
 *
 *  Le sélecteur de période filtre toutes les statistiques d'ACTIVITÉ
 *  (tentatives, échecs, espace récupéré, débit, gain) — leurs titres
 *  l'indiquent. Les cartes d'ÉTAT (codecs, progression, fichiers restants,
 *  ressources) décrivent la bibliothèque actuelle. */

import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  FileText,
  Pause,
  Play,
  RotateCcw,
  ScanSearch,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  fetchJson,
  type ActiveJob,
  type StatsPeriod,
  type ThroughputStats,
  type TimeUnit,
} from "@/api/client";
import {
  useCapabilities,
  useCodecStats,
  useFailures,
  useGainByBucket,
  useJobAction,
  useLibraries,
  useLibraryProgress,
  useQueueAction,
  useQueueState,
  useReliability,
  useRetryFailed,
  useSavingsOverTime,
  useStatsSummary,
  useThroughput,
  useTopRemaining,
  useTriggerScan,
} from "@/api/hooks";
import {
  axisProps,
  barCursor,
  bucketLabel,
  bucketTick,
  CHART,
  ChartCard,
  ChartTooltip,
  EmptyChart,
  gridProps,
  InlineBar,
  LegendDot,
  TooltipBox,
} from "@/components/charts";
import { AttemptBadge, StrategyBadge } from "@/components/diagnostics/badges";
import { JobDetailDialog } from "@/components/diagnostics/JobDetailDialog";
import { JobLogViewer } from "@/components/JobLogViewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { failureMeta, FAILURE_GROUP_LABELS, strategyMeta } from "@/lib/failures";
import { basename, formatBytes, formatDuration, formatPercent } from "@/lib/format";
import { useI18n, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useQueueSocket } from "@/ws/useQueueSocket";

// ---------------------------------------------------------------------------
// Période des statistiques (persistée par navigateur)
// ---------------------------------------------------------------------------
const PERIODS: { value: StatsPeriod; label: string; tag: string }[] = [
  { value: 1, label: "24 h", tag: "24 dernières heures" },
  { value: 7, label: "7 j", tag: "7 derniers jours" },
  { value: 30, label: "30 j", tag: "30 derniers jours" },
  { value: null, label: "Tout", tag: "depuis le début" },
];

/** Rappel discret de la période dans le titre d'une carte filtrée. */
function PeriodTag({ period }: { period: StatsPeriod }) {
  const t = useT();
  const entry = PERIODS.find((p) => p.value === period);
  if (!entry) return null;
  return (
    <span className="ml-1.5 font-normal normal-case tracking-normal">· {t(entry.tag)}</span>
  );
}

const GIB = 1024 ** 3;

function usePeriod(): [StatsPeriod, (p: StatsPeriod) => void] {
  const [period, setPeriod] = useState<StatsPeriod>(() => {
    try {
      const raw = localStorage.getItem("stats-period");
      if (raw === "all") return null;
      const n = Number(raw);
      return n === 1 || n === 7 || n === 30 ? n : 7;
    } catch {
      return 7;
    }
  });
  const update = (p: StatsPeriod) => {
    setPeriod(p);
    try {
      localStorage.setItem("stats-period", p === null ? "all" : String(p));
    } catch {
      /* stockage indisponible : préférence non mémorisée */
    }
  };
  return [period, update];
}

function PeriodSelector({ value, onChange }: { value: StatsPeriod; onChange: (p: StatsPeriod) => void }) {
  const t = useT();
  return (
    <div className="inline-flex h-8 items-center rounded-md bg-muted p-0.5" role="radiogroup" aria-label={t("Période")}>
      {PERIODS.map((p) => (
        <button
          key={p.label}
          role="radio"
          aria-checked={value === p.value}
          onClick={() => onChange(p.value)}
          className={cn(
            "rounded px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors",
            value === p.value && "bg-background text-foreground shadow-sm",
          )}
        >
          {t(p.label)}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Jobs en cours
// ---------------------------------------------------------------------------
function ActiveJobCard({
  job,
  onLogs,
  onDetails,
}: {
  job: ActiveJob;
  onLogs: () => void;
  onDetails: () => void;
}) {
  const jobAction = useJobAction();
  const { t, tPhase } = useI18n();
  return (
    <Card className="min-w-0 flex-1 basis-80">
      <CardContent className="space-y-2 p-3">
        <div className="flex items-center justify-between gap-2">
          <button
            className="truncate text-left font-mono text-xs hover:underline"
            title={job.path}
            onClick={onDetails}
          >
            {basename(job.path) || t("Préparation…")}
          </button>
          <div className="flex shrink-0 gap-1">
            <StrategyBadge strategy={job.strategy} />
            <AttemptBadge attempt={job.attempt} />
            {job.dry_run && <Badge variant="warning">DRY RUN</Badge>}
          </div>
        </div>
        <div
          className="h-1.5 overflow-hidden rounded bg-muted"
          role="progressbar"
          aria-valuenow={Math.round(job.percent ?? 0)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full rounded bg-primary transition-all"
            style={{ width: `${job.percent ?? 0}%` }}
          />
        </div>
        <div className="flex items-center justify-between font-mono text-xs text-muted-foreground">
          <span>{formatPercent(job.percent ?? 0)}</span>
          <span>{job.fps ? `${job.fps.toFixed(0)} fps` : "—"}</span>
          <span>{job.speed ? `${job.speed.toFixed(2)}x` : "—"}</span>
          <span>ETA {formatDuration(job.eta_s)}</span>
        </div>
        {job.phase && (
          <div className="truncate text-xs text-primary" title={tPhase(job.phase) ?? ""}>
            {tPhase(job.phase)}
          </div>
        )}
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span className="truncate">
            {job.gpu} · slot {job.slot}
          </span>
          <div className="flex gap-1">
            <Button size="sm" variant="ghost" onClick={onLogs}>
              <FileText className="h-3.5 w-3.5" /> {t("Logs")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-destructive"
              onClick={() => jobAction.mutate({ id: job.job_id, action: "cancel" })}
            >
              <XCircle className="h-3.5 w-3.5" /> {t("Annuler")}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Indicateurs
// ---------------------------------------------------------------------------
function Kpi({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "good" | "bad";
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</div>
        <div
          className={cn(
            "mt-1 text-2xl font-bold",
            tone === "good" && "text-success",
            tone === "bad" && "text-destructive",
          )}
        >
          {value}
        </div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : formatPercent(value * 100);
}

// ---------------------------------------------------------------------------
// Fiabilité : tentatives par jour + répartition
// ---------------------------------------------------------------------------
function ReliabilityCard({ period }: { period: StatsPeriod }) {
  const { t, lang } = useI18n();
  const { data } = useReliability(period);
  const unit: TimeUnit = data?.unit ?? "day";
  const counts = data?.counts;
  const attempts = data?.attempts ?? 0;
  const pct = (n: number) => (attempts ? `${((n / attempts) * 100).toFixed(1)} %` : "—");

  return (
    <Card className="lg:col-span-2">
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
        <CardTitle>
          {t("Tentatives de réencodage")}
          <PeriodTag period={period} />
        </CardTitle>
        <div className="flex flex-wrap gap-3">
          <LegendDot color={CHART.good} label={t("Réussis")} />
          <LegendDot color={CHART.neutral} label={t("Sans gain")} />
          <LegendDot color={CHART.bad} label={t("Échecs")} />
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-[1fr_15rem]">
        <div className="h-60">
          {attempts === 0 ? (
            <EmptyChart message={t("Aucune tentative sur la période.")} />
          ) : (
            <ResponsiveContainer>
              <BarChart data={data?.timeline ?? []} margin={{ top: 4, right: 4, bottom: 0, left: -12 }}>
                <CartesianGrid {...gridProps} />
                <XAxis
                  dataKey="t"
                  {...axisProps}
                  minTickGap={16}
                  tickFormatter={(v: string) => bucketTick(v, unit, lang)}
                />
                <YAxis {...axisProps} axisLine={false} allowDecimals={false} />
                <Tooltip
                  cursor={barCursor}
                  content={<ChartTooltip formatLabel={(l) => bucketLabel(l, unit, lang)} />}
                />
                <Bar isAnimationActive={false} dataKey="success" name={t("Réussis")} stackId="a" fill={CHART.good} stroke="hsl(var(--card))" strokeWidth={1} maxBarSize={28} />
                <Bar isAnimationActive={false} dataKey="no_gain" name={t("Sans gain")} stackId="a" fill={CHART.neutral} stroke="hsl(var(--card))" strokeWidth={1} maxBarSize={28} />
                <Bar isAnimationActive={false} dataKey="failed" name={t("Échecs")} stackId="a" fill={CHART.bad} stroke="hsl(var(--card))" strokeWidth={1} radius={[4, 4, 0, 0]} maxBarSize={28} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
        <dl className="space-y-2.5 text-sm">
          <div className="flex items-baseline justify-between">
            <dt className="text-muted-foreground">{t("Tentatives")}</dt>
            <dd className="font-mono">{attempts.toLocaleString()}</dd>
          </div>
          <div className="flex items-baseline justify-between">
            <dt className="flex items-center gap-1.5"><LegendDot color={CHART.good} label={t("Réussis")} /></dt>
            <dd className="font-mono">{(counts?.success ?? 0).toLocaleString()} <span className="text-xs text-muted-foreground">{pct(counts?.success ?? 0)}</span></dd>
          </div>
          <div className="flex items-baseline justify-between">
            <dt><LegendDot color={CHART.neutral} label={t("Sans gain")} /></dt>
            <dd className="font-mono">{(counts?.no_gain ?? 0).toLocaleString()} <span className="text-xs text-muted-foreground">{pct(counts?.no_gain ?? 0)}</span></dd>
          </div>
          <div className="flex items-baseline justify-between">
            <dt><LegendDot color={CHART.bad} label={t("Échecs")} /></dt>
            <dd className="font-mono">{(counts?.failed ?? 0).toLocaleString()} <span className="text-xs text-muted-foreground">{pct(counts?.failed ?? 0)}</span></dd>
          </div>
          <div className="border-t pt-2.5 text-xs text-muted-foreground">
            <div className="flex justify-between">
              <span>{t("Abandons anticipés (sans gain)")}</span>
              <span className="font-mono">{(counts?.no_gain_early ?? 0).toLocaleString()}</span>
            </div>
            <div className="flex justify-between">
              <span>{t("Écartés avant encodage")}</span>
              <span className="font-mono">
                {((counts?.no_gain_preflight ?? 0) + (data?.files.predicted_no_gain ?? 0)).toLocaleString()}
              </span>
            </div>
            <div className="flex justify-between">
              <span>{t("Heures perdues en échecs")}</span>
              <span className="font-mono">{(data?.gpu_hours.failed ?? 0).toFixed(1)} h</span>
            </div>
            <div className="flex justify-between">
              <span>{t("Heures perdues sans gain")}</span>
              <span className="font-mono">{(data?.gpu_hours.no_gain ?? 0).toFixed(1)} h</span>
            </div>
          </div>
          {(data?.strategies ?? []).some((s) => s.strategy !== "standard") && (
            <div className="border-t pt-2.5 text-xs">
              <div className="mb-1 text-muted-foreground">{t("Réussites par méthode")}</div>
              {(data?.strategies ?? []).map((s) => (
                <div key={s.strategy} className="flex justify-between">
                  <span title={t(strategyMeta(s.strategy).description)}>{t(strategyMeta(s.strategy).label)}</span>
                  <span className="font-mono">
                    <span className="text-success">{s.success}</span> / <span className="text-destructive">{s.failed}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Causes d'échec
// ---------------------------------------------------------------------------
function FailureCausesCard({ period }: { period: StatsPeriod }) {
  const t = useT();
  const { data } = useFailures(period);
  const retry = useRetryFailed();
  const [message, setMessage] = useState<string | null>(null);
  const categories = (data?.categories ?? []).filter((c) => c.jobs > 0 || c.failed_files > 0);
  const max = Math.max(1, ...categories.map((c) => c.jobs));

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
        <CardTitle>
          {t("Causes d'échec")}
          <PeriodTag period={period} />
        </CardTitle>
        <div className="flex items-center gap-2">
          {(data?.failed_files_total ?? 0) > 0 && (
            <Button
              size="sm"
              variant="secondary"
              disabled={retry.isPending}
              onClick={() => retry.mutate({}, { onSuccess: (d) => setMessage(d.detail ?? null) })}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              {t("Relancer les échecs")} ({data?.failed_files_total})
            </Button>
          )}
          <Link to="/queue?tab=failures" className="text-xs text-primary hover:underline">
            {t("Détails")}
          </Link>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {message && <div className="text-xs text-muted-foreground">{message}</div>}
        {categories.length === 0 ? (
          <div className="py-6 text-center text-sm text-muted-foreground">{t("Aucun échec sur la période.")}</div>
        ) : (
          <ul className="max-h-[22rem] space-y-2.5 overflow-auto pr-1">
            {categories.map((c) => {
              const meta = failureMeta(c.category);
              return (
                <li key={c.category} className="space-y-1">
                  <Link
                    to={`/queue?tab=failures&category=${c.category}`}
                    className="block text-sm hover:underline"
                    title={`${t(meta.explanation)}\n→ ${t(meta.remedy)}`}
                  >
                    {t(meta.label)}
                    <span className="ml-1.5 text-[11px] text-muted-foreground">
                      {t(FAILURE_GROUP_LABELS[meta.group])}
                    </span>
                  </Link>
                  <InlineBar value={c.jobs} max={max} color={CHART.bad} />
                  <div className="flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
                    <span className="font-mono">
                      {c.jobs.toLocaleString()} {t("tent.")} · {c.files} {t("fich.")}
                      {c.failed_files > 0 && (
                        <span className="text-destructive"> · {c.failed_files} {t("en échec définitif")}</span>
                      )}
                    </span>
                    {c.failed_files > 0 && (
                      <button
                        className="shrink-0 text-primary hover:underline disabled:opacity-50"
                        disabled={retry.isPending}
                        onClick={() =>
                          retry.mutate(
                            { failure_category: c.category },
                            { onSuccess: (d) => setMessage(d.detail ?? null) },
                          )
                        }
                      >
                        {t("Relancer")}
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Espace récupéré et débit (période)
// ---------------------------------------------------------------------------
function SavingsCard({ period }: { period: StatsPeriod }) {
  const { t, lang } = useI18n();
  const { data } = useSavingsOverTime(period);
  const unit: TimeUnit = data?.unit ?? "day";
  const points = (data?.points ?? []).map((p) => ({ ...p, cumulative_gib: p.cumulative / GIB }));
  const empty = !data || data.total_bytes <= 0;

  return (
    <ChartCard
      title={
        <>
          {t("Espace récupéré cumulé (Gio)")}
          <PeriodTag period={period} />
        </>
      }
      action={
        !empty && (
          <span className="font-mono text-sm font-semibold text-success">
            {formatBytes(data.total_bytes)}
          </span>
        )
      }
    >
      {empty ? (
        <EmptyChart message={t("Aucun fichier remplacé sur la période.")} />
      ) : (
        <ResponsiveContainer>
          <AreaChart data={points} margin={{ top: 4, right: 4, bottom: 0, left: -8 }}>
            <CartesianGrid {...gridProps} />
            <XAxis
              dataKey="t"
              {...axisProps}
              minTickGap={24}
              tickFormatter={(v: string) => bucketTick(v, unit, lang)}
            />
            <YAxis
              {...axisProps}
              axisLine={false}
              tickFormatter={(v: number) => (v === 0 || v >= 10 ? v.toFixed(0) : v.toFixed(1))}
            />
            <Tooltip
              cursor={{ stroke: CHART.axis, strokeDasharray: "3 3" }}
              content={({ active, payload }) => {
                const point = payload?.[0]?.payload as (typeof points)[number] | undefined;
                if (!active || !point) return null;
                return (
                  <TooltipBox
                    title={bucketLabel(point.t, unit, lang)}
                    rows={[
                      { label: t("sur le créneau"), value: `+${formatBytes(point.bytes_saved)}` },
                      { label: t("cumulé"), value: formatBytes(point.cumulative), color: CHART.series },
                    ]}
                  />
                );
              }}
            />
            <Area
              isAnimationActive={false}
              type="monotone"
              dataKey="cumulative_gib"
              name={t("cumulé")}
              stroke={CHART.series}
              strokeWidth={2}
              fill={CHART.series}
              fillOpacity={0.15}
              dot={false}
              activeDot={{ r: 4, stroke: CHART.surface, strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </ChartCard>
  );
}

type ThroughputPoint = ThroughputStats["points"][number];

const THROUGHPUT_TITLES: Record<TimeUnit, string> = {
  hour: "Débit d'encodage (fichiers/heure)",
  day: "Débit d'encodage (fichiers/jour)",
  week: "Débit d'encodage (fichiers/semaine)",
};

function ThroughputCard({ period }: { period: StatsPeriod }) {
  const { t, lang } = useI18n();
  const { data } = useThroughput(period);
  const unit: TimeUnit = data?.unit ?? "day";
  const empty = !data || data.files === 0;

  return (
    <ChartCard
      title={
        <>
          {t(THROUGHPUT_TITLES[unit])}
          <PeriodTag period={period} />
        </>
      }
      action={
        !empty && (
          <span className="font-mono text-xs text-muted-foreground">
            <span className="text-sm font-semibold text-foreground">{data.files.toLocaleString()}</span>{" "}
            {t("fichiers")} · {data.gpu_hours.toFixed(1)} h GPU
          </span>
        )
      }
    >
      {empty ? (
        <EmptyChart message={t("Aucun fichier réencodé sur la période.")} />
      ) : (
        <ResponsiveContainer>
          <BarChart data={data.points} margin={{ top: 4, right: 4, bottom: 0, left: -12 }}>
            <CartesianGrid {...gridProps} />
            <XAxis
              dataKey="t"
              {...axisProps}
              minTickGap={16}
              tickFormatter={(v: string) => bucketTick(v, unit, lang)}
            />
            <YAxis {...axisProps} axisLine={false} allowDecimals={false} />
            <Tooltip
              cursor={barCursor}
              content={({ active, payload }) => {
                const point = payload?.[0]?.payload as ThroughputPoint | undefined;
                if (!active || !point) return null;
                return (
                  <TooltipBox
                    title={bucketLabel(point.t, unit, lang)}
                    rows={[
                      { label: t("fichiers"), value: point.files.toLocaleString(), color: CHART.series },
                      { label: t("temps GPU"), value: `${point.gpu_hours.toFixed(1)} h` },
                    ]}
                  />
                );
              }}
            />
            <Bar
              isAnimationActive={false}
              dataKey="files"
              name={t("fichiers")}
              fill={CHART.series}
              radius={[4, 4, 0, 0]}
              maxBarSize={28}
            />
          </BarChart>
        </ResponsiveContainer>
      )}
    </ChartCard>
  );
}

function GainByBucketCard({ period }: { period: StatsPeriod }) {
  const t = useT();
  const { data } = useGainByBucket(period);
  const gainData = (data ?? []).map((g) => ({
    name: `${g.resolution ?? "?"} ${(g.codec ?? "").toUpperCase()}`,
    gain: g.avg_gain_percent ?? 0,
    count: g.count,
  }));

  return (
    <ChartCard
      title={
        <>
          {t("Gain moyen par bucket et codec cible (%)")}
          <PeriodTag period={period} />
        </>
      }
    >
      {gainData.length === 0 ? (
        <EmptyChart message={t("Aucun fichier remplacé sur la période.")} />
      ) : (
        <ResponsiveContainer>
          <BarChart data={gainData} margin={{ top: 4, right: 4, bottom: 0, left: -12 }}>
            <CartesianGrid {...gridProps} />
            <XAxis dataKey="name" {...axisProps} />
            <YAxis {...axisProps} axisLine={false} />
            <Tooltip
              cursor={barCursor}
              content={({ active, payload }) => {
                const row = payload?.[0]?.payload as (typeof gainData)[number] | undefined;
                if (!active || !row) return null;
                return (
                  <TooltipBox
                    title={row.name}
                    rows={[
                      { label: t("gain moyen"), value: formatPercent(row.gain), color: CHART.series },
                      { label: t("fichiers"), value: row.count.toLocaleString() },
                    ]}
                  />
                );
              }}
            />
            <Bar
              isAnimationActive={false}
              dataKey="gain"
              name={t("gain moyen")}
              fill={CHART.series}
              radius={[4, 4, 0, 0]}
              maxBarSize={40}
            />
          </BarChart>
        </ResponsiveContainer>
      )}
    </ChartCard>
  );
}

// ---------------------------------------------------------------------------
// Codecs de la bibliothèque (état actuel)
// ---------------------------------------------------------------------------
/** Couleur par CODEC (jamais par rang) : les trois codecs qui comptent pour
 *  un transcodeur, dans l'ordre des séries validé ; le reste est regroupé. */
const CODEC_SLOTS: { codec: string; color: string }[] = [
  { codec: "hevc", color: CHART.cat1 },
  { codec: "av1", color: CHART.cat2 },
  { codec: "h264", color: CHART.cat3 },
];

const CODEC_NAMES: Record<string, string> = {
  hevc: "HEVC",
  av1: "AV1",
  h264: "H.264",
  mpeg4: "MPEG-4",
  mpeg2video: "MPEG-2",
  vc1: "VC-1",
  prores: "ProRes",
  msmpeg4v3: "MS-MPEG4",
  vp9: "VP9",
};

function codecName(codec: string): string {
  return CODEC_NAMES[codec] ?? codec.toUpperCase();
}

interface CodecSlice {
  key: string;
  label: string;
  value: number;
  color: string;
  /** Codecs regroupés dans « Autres » */
  members?: { codec: string; value: number }[];
}

function sharePercent(value: number, total: number): string {
  if (!total) return "—";
  const pct = (value / total) * 100;
  return pct > 0 && pct < 0.1 ? "< 0.1 %" : formatPercent(pct);
}

function CodecsCard() {
  const t = useT();
  const { data } = useCodecStats();
  const current = data?.current ?? {};
  const total = Object.values(current).reduce((sum, n) => sum + n, 0);

  const slices: CodecSlice[] = CODEC_SLOTS.filter((s) => (current[s.codec] ?? 0) > 0).map((s) => ({
    key: s.codec,
    label: codecName(s.codec),
    value: current[s.codec] ?? 0,
    color: s.color,
  }));
  const others = Object.entries(current)
    .filter(([codec, n]) => n > 0 && !CODEC_SLOTS.some((s) => s.codec === codec))
    .map(([codec, value]) => ({ codec, value }))
    .sort((a, b) => b.value - a.value);
  if (others.length > 0) {
    slices.push({
      key: "others",
      label: others.length === 1 ? codecName(others[0]!.codec) : t("Autres"),
      value: others.reduce((sum, o) => sum + o.value, 0),
      color: CHART.neutral,
      members: others.length > 1 ? others : undefined,
    });
  }

  return (
    <ChartCard title={t("Codecs de la bibliothèque")} bodyClassName="h-auto min-h-64">
      {total === 0 ? (
        <EmptyChart message={t("Pas encore de données.")} />
      ) : (
        <div className="grid items-center gap-5 sm:grid-cols-[11rem_minmax(0,1fr)]">
          <div className="relative mx-auto aspect-square w-44 max-w-full">
            <ResponsiveContainer>
              <PieChart>
                <Pie
                  data={slices}
                  dataKey="value"
                  nameKey="label"
                  innerRadius="68%"
                  outerRadius="100%"
                  startAngle={90}
                  endAngle={-270}
                  minAngle={3}
                  stroke={CHART.surface}
                  strokeWidth={2}
                  isAnimationActive={false}
                >
                  {slices.map((slice) => (
                    <Cell key={slice.key} fill={slice.color} />
                  ))}
                </Pie>
                <Tooltip
                  wrapperStyle={{ zIndex: 10 }}
                  content={({ active, payload }) => {
                    const slice = payload?.[0]?.payload as CodecSlice | undefined;
                    if (!active || !slice) return null;
                    return (
                      <TooltipBox
                        title={slice.label}
                        rows={[
                          {
                            label: t("fichiers"),
                            value: `${slice.value.toLocaleString()} · ${sharePercent(slice.value, total)}`,
                            color: slice.color,
                          },
                          ...(slice.members ?? []).map((m) => ({
                            label: codecName(m.codec),
                            value: m.value.toLocaleString(),
                          })),
                        ]}
                      />
                    );
                  }}
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <span className="font-mono text-xl font-bold">{total.toLocaleString()}</span>
              <span className="text-xs text-muted-foreground">{t("fichiers")}</span>
            </div>
          </div>
          <ul className="space-y-3">
            {slices.map((slice) => (
              <li key={slice.key}>
                <div className="flex items-baseline justify-between gap-3">
                  <span className="flex items-center gap-2 text-sm">
                    <span
                      className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
                      style={{ background: slice.color }}
                    />
                    {slice.label}
                  </span>
                  <span className="font-mono text-base font-semibold">
                    {sharePercent(slice.value, total)}
                  </span>
                </div>
                <div className="pl-[1.125rem] font-mono text-xs text-muted-foreground">
                  {slice.value.toLocaleString()} {t("fichiers")}
                  {slice.members && (
                    <span>
                      {" "}
                      ({slice.members.map((m) => `${codecName(m.codec)} ${m.value.toLocaleString()}`).join(" · ")})
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </ChartCard>
  );
}

// ---------------------------------------------------------------------------
// Ressources et GPU
// ---------------------------------------------------------------------------
interface ResourcesInfo {
  cpu: { cores: number; app_percent: number | null; other_percent: number | null; host_percent: number | null };
  memory: { total_bytes: number | null; app_bytes: number | null; other_bytes: number | null; used_bytes: number | null };
  gpu: { busy_percent: number | null; freq_mhz: number | null; freq_max_mhz: number | null };
}

function UsageBar({
  label,
  parts,
  detail,
}: {
  label: string;
  parts: { value: number | null; color: string; title: string }[];
  detail: string;
}) {
  return (
    <div>
      <div className="mb-0.5 flex justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono">{detail}</span>
      </div>
      <div className="flex h-2 gap-0.5 overflow-hidden rounded bg-muted">
        {parts.map(
          (p) =>
            p.value !== null && (
              <div
                key={p.title}
                className="h-full rounded-sm"
                style={{ width: `${Math.min(100, p.value)}%`, background: p.color }}
                title={p.title}
              />
            ),
        )}
      </div>
    </div>
  );
}

function ResourcesCard() {
  const t = useT();
  const { data } = useQuery({
    queryKey: ["stats", "resources"],
    refetchInterval: 5000,
    queryFn: () => fetchJson<ResourcesInfo>("/api/v1/stats/resources"),
  });
  const { data: caps } = useCapabilities();
  const cpu = data?.cpu;
  const memory = data?.memory;
  const gpu = data?.gpu;
  const total = memory?.total_bytes ?? 1;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("Ressources & GPU")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data && cpu && memory && gpu ? (
          <>
            <UsageBar
              label="CPU"
              detail={
                cpu.app_percent !== null
                  ? `${t("app")} ${cpu.app_percent.toFixed(0)}% · ${t("reste du système")} ${(cpu.other_percent ?? 0).toFixed(0)}%`
                  : t("indisponible")
              }
              parts={[
                { value: cpu.app_percent, color: CHART.series, title: t("app") },
                { value: cpu.other_percent, color: CHART.neutral, title: t("reste du système") },
              ]}
            />
            <UsageBar
              label="RAM"
              detail={
                memory.used_bytes !== null && memory.total_bytes !== null
                  ? `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`
                  : t("indisponible")
              }
              parts={[
                {
                  value: memory.used_bytes !== null ? (memory.used_bytes / total) * 100 : null,
                  color: CHART.neutral,
                  title: "RAM",
                },
              ]}
            />
            <UsageBar
              label="GPU"
              detail={
                gpu.busy_percent !== null
                  ? `${gpu.busy_percent.toFixed(0)}%${gpu.freq_mhz ? ` · ${gpu.freq_mhz} MHz` : ""}`
                  : t("indisponible")
              }
              parts={[{ value: gpu.busy_percent, color: CHART.series, title: t("app") }]}
            />
          </>
        ) : (
          <div className="text-sm text-muted-foreground">{t("Chargement…")}</div>
        )}
        <div className="space-y-2 border-t pt-3">
          {(caps?.gpus ?? []).map((g) => (
            <div key={g.gpu_id} className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{g.name}</div>
                <div className="font-mono text-xs text-muted-foreground">
                  {g.drm_node} · {g.effective_api.toUpperCase()}
                  {g.fell_back_to_vaapi && " (fallback VAAPI)"}
                </div>
              </div>
              <div className="flex flex-wrap gap-1">
                {Object.entries(g.encoders).map(([name, res]) => (
                  <Badge key={name} variant={res.ok ? "success" : "destructive"} title={res.error ?? ""}>
                    {name}
                  </Badge>
                ))}
                {g.alt_api && (
                  <Badge variant="info" title={t("API alternative utilisée par la stratégie de repli « Autre API GPU »")}>
                    {t("repli")} {g.alt_api.toUpperCase()}
                  </Badge>
                )}
                {g.p5_zero_copy && <Badge variant="info">P5 zéro-copie</Badge>}
              </div>
            </div>
          ))}
          {caps && !caps.libplacebo_dv && (
            <div className="text-xs text-warning">
              {t("libplacebo sans support Dolby Vision : les fichiers DV P5 seront marqués « non traité (politique) », jamais convertis en best-effort.")}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export function Dashboard() {
  const t = useT();
  const [period, setPeriod] = usePeriod();
  const { active } = useQueueSocket();
  const { data: summary } = useStatsSummary();
  const { data: queue } = useQueueState();
  const { data: libraries } = useLibraries();
  const { data: reliability } = useReliability(period);
  const { data: savings } = useSavingsOverTime(period);
  const { data: throughput } = useThroughput(period);
  const { data: topRemaining } = useTopRemaining();
  const { data: progress } = useLibraryProgress();
  const queueAction = useQueueAction();
  const scan = useTriggerScan();
  const [logJobId, setLogJobId] = useState<number | null>(null);
  const [detailJobId, setDetailJobId] = useState<number | null>(null);

  const paused = queue?.paused ?? false;
  const totalSlots = queue?.workers ?? 0;

  const failedNow = reliability?.files.failed_now ?? 0;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-bold">{t("Dashboard")}</h1>
        <div className="flex flex-wrap items-center gap-2">
          <PeriodSelector value={period} onChange={setPeriod} />
          <Button
            variant={paused ? "default" : "secondary"}
            onClick={() => queueAction.mutate({ action: paused ? "resume" : "pause" })}
          >
            {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
            {paused ? t("Reprendre") : t("Pause")}
          </Button>
          <Button
            variant="secondary"
            disabled={scan.isPending}
            onClick={() => {
              for (const lib of libraries ?? []) {
                if (lib.enabled) scan.mutate(lib.id);
              }
            }}
          >
            <ScanSearch className="h-4 w-4" /> {t("Lancer un scan")}
          </Button>
        </div>
      </div>

      {/* En direct */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t("Jobs en cours")} ({active.length}/{totalSlots} {t("slots")})
          </h2>
          <Link to="/queue" className="flex items-center gap-1 text-xs text-primary hover:underline">
            {queue?.waiting_count ?? 0} {t("en file d'attente")} <ArrowRight className="h-3 w-3" />
          </Link>
        </div>
        {active.length === 0 ? (
          <Card>
            <CardContent className="py-6 text-center text-sm text-muted-foreground">
              {paused ? t("Queue en pause.") : t("Aucun encodage en cours.")}
            </CardContent>
          </Card>
        ) : (
          <div className="flex flex-wrap gap-3">
            {active.map((job) => (
              <ActiveJobCard
                key={job.job_id}
                job={job}
                onLogs={() => setLogJobId(job.job_id)}
                onDetails={() => setDetailJobId(job.job_id)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Indicateurs clés */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Kpi
          label={t("Fichiers traités")}
          value={`${(summary?.processed ?? 0).toLocaleString()}`}
          sub={`${t("sur")} ${(summary?.total_files ?? 0).toLocaleString()} · ${(summary?.queued ?? 0).toLocaleString()} ${t("en file")}`}
        />
        <Kpi
          label={t("Espace récupéré")}
          value={formatBytes(savings?.total_bytes ?? 0)}
          sub={
            `${t("gain moyen")} ${formatPercent(savings?.avg_gain_percent)}` +
            (period !== null ? ` · ${t("total")} ${formatBytes(summary?.bytes_saved)}` : "")
          }
        />
        <Kpi
          label={t("Taux de réussite")}
          value={ratio(reliability?.success_rate)}
          tone={reliability?.success_rate != null ? (reliability.success_rate >= 0.9 ? "good" : reliability.success_rate < 0.6 ? "bad" : undefined) : undefined}
          sub={`${(reliability?.counts.success ?? 0).toLocaleString()} ${t("réussis")} · ${(reliability?.counts.failed ?? 0).toLocaleString()} ${t("échecs")}`}
        />
        <Kpi
          label={t("Réussis au 1er essai")}
          value={ratio(reliability?.first_try_rate)}
          sub={`${(reliability?.recovered_by_fallback ?? 0).toLocaleString()} ${t("récupérés après nouvel essai")}`}
        />
        <Kpi
          label={t("Fichiers en échec")}
          value={failedNow.toLocaleString()}
          tone={failedNow > 0 ? "bad" : "good"}
          sub={`${(reliability?.files.retrying_now ?? 0).toLocaleString()} ${t("en cours de nouvel essai")}`}
        />
        <Kpi
          label={t("Débit")}
          value={`${(throughput?.files_per_hour ?? 0).toFixed(1)} f/h`}
          sub={`${(throughput?.files ?? 0).toLocaleString()} ${t("fichiers")} · ${t("reste")} ${formatDuration(summary?.eta_s)}`}
        />
      </section>

      {/* Fiabilité */}
      <section className="grid gap-4 lg:grid-cols-3">
        <ReliabilityCard period={period} />
        <FailureCausesCard period={period} />
      </section>

      {/* Économies et débit (période), répartition des codecs (état) */}
      <section className="grid gap-4 lg:grid-cols-2">
        <SavingsCard period={period} />
        <ThroughputCard period={period} />
        <GainByBucketCard period={period} />
        <CodecsCard />
      </section>

      {/* Progression, restants, ressources */}
      <section className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>{t("Progression par bibliothèque")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {(progress ?? []).map((p) => (
              <div key={p.library_id}>
                <div className="mb-1 flex justify-between text-sm">
                  <span>{p.name}</span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {p.conform.toLocaleString()}/{p.total.toLocaleString()} · {formatPercent(p.percent)}
                  </span>
                </div>
                <InlineBar value={p.percent} max={100} color={CHART.good} />
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("Plus gros fichiers restants")}</CardTitle>
          </CardHeader>
          <CardContent className="max-h-72 overflow-auto">
            <table className="w-full text-sm">
              <tbody>
                {(topRemaining ?? []).map((f) => (
                  <tr key={f.id} className="border-b border-border/50">
                    <td className="max-w-[12rem] truncate py-1 font-mono text-xs" title={f.path}>
                      {basename(f.path)}
                    </td>
                    <td className="py-1 text-right font-mono text-xs">{formatBytes(f.size_bytes)}</td>
                    <td className="py-1 text-right font-mono text-xs text-success">
                      {f.estimated_gain_bytes ? `~${formatBytes(f.estimated_gain_bytes)}` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>

        <ResourcesCard />
      </section>

      <Dialog open={logJobId !== null} onOpenChange={(open) => !open && setLogJobId(null)}>
        <DialogContent className="max-w-3xl">
          <DialogTitle>
            {t("Log du job")} #{logJobId}
          </DialogTitle>
          {logJobId !== null && <JobLogViewer jobId={logJobId} live />}
        </DialogContent>
      </Dialog>
      <JobDetailDialog jobId={detailJobId} onClose={() => setDetailJobId(null)} />
    </div>
  );
}
