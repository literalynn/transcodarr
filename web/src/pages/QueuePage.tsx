/** Queue (§7.2 + §7.3) : file d'attente (drag pour réordonner), encodages en
 *  cours, historique filtrable de toutes les tentatives, et gestion des
 *  fichiers en échec (causes, méthodes essayées, relance ciblée).
 *  L'onglet actif et les filtres vivent dans l'URL (liens partageables). */

import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowUpToLine,
  Ban,
  Download,
  GripVertical,
  RefreshCw,
  RotateCcw,
  Search,
  TrendingDown,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { Job, MediaFile } from "@/api/client";
import {
  useFailures,
  useFileAction,
  useFiles,
  useInvalidate,
  useJobAction,
  useJobs,
  useLibraries,
  useQueueState,
  useReorderQueue,
  useRetryFailed,
  useReviewNoGain,
} from "@/api/hooks";
import { AttemptBadge, FailureBadge, StrategyBadge } from "@/components/diagnostics/badges";
import { JobDetailDialog } from "@/components/diagnostics/JobDetailDialog";
import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Input, Select } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { FAILURE_CATEGORIES, failureMeta, STRATEGIES, strategyMeta } from "@/lib/failures";
import {
  basename,
  formatBitrate,
  formatBytes,
  formatDate,
  formatDuration,
  formatPercent,
} from "@/lib/format";
import { useI18n, useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useQueueSocket } from "@/ws/useQueueSocket";

const ROW_H = 40;
const PAGE = 50;

type TabId = "waiting" | "active" | "history" | "failures";

function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(id);
  }, [value, delay]);
  return debounced;
}

function relativeFuture(iso: string | null | undefined, t: (s: string) => string): string | null {
  if (!iso) return null;
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return null;
  return `${t("dans")} ${formatDuration(diff / 1000)}`;
}

// ---------------------------------------------------------------------------
// En attente
// ---------------------------------------------------------------------------
function WaitingTab({ search }: { search: string }) {
  const t = useT();
  const { data, isFetching } = useFiles(
    { status: "queued", search, order: "queue", limit: 1000 },
    { refetchInterval: 15000 },
  );
  const reorder = useReorderQueue();
  const parentRef = useRef<HTMLDivElement>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [localOrder, setLocalOrder] = useState<MediaFile[] | null>(null);

  const items = localOrder ?? data?.items ?? [];
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_H,
    overscan: 20,
  });

  const onDrop = (targetIndex: number) => {
    if (dragIndex === null || dragIndex === targetIndex) {
      setDragIndex(null);
      return;
    }
    const next = [...items];
    const moved = next.splice(dragIndex, 1)[0];
    if (moved) {
      next.splice(targetIndex, 0, moved);
      setLocalOrder(next);
      reorder.mutate(
        next.map((f) => f.id),
        { onSettled: () => setLocalOrder(null) },
      );
    }
    setDragIndex(null);
  };

  const grid = "grid-cols-[24px_minmax(0,1fr)_80px_90px_90px_110px_160px_40px]";
  return (
    <div className={cn("overflow-x-auto rounded-md border", isFetching && "opacity-90")}>
      <div className="min-w-[860px]">
        <div
          className={cn(
            "grid gap-2 border-b bg-card px-2 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground",
            grid,
          )}
        >
          <span />
          <span>{t("Fichier")}</span>
          <span>{t("Codec")}</span>
          <span>{t("Résolution")}</span>
          <span>{t("Bitrate")}</span>
          <span>{t("Gain estimé")}</span>
          <span>{t("Tentative")}</span>
          <span />
        </div>
        <div ref={parentRef} className="h-[62vh] overflow-auto">
          {items.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted-foreground">
              {t("Aucun fichier en attente.")}
            </div>
          ) : (
            <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
              {virtualizer.getVirtualItems().map((row) => {
                const file = items[row.index];
                if (!file) return null;
                const delay = relativeFuture(file.next_attempt_at, t);
                return (
                  <div
                    key={file.id}
                    draggable
                    onDragStart={() => setDragIndex(row.index)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => onDrop(row.index)}
                    className={cn(
                      "absolute left-0 grid w-full cursor-grab items-center gap-2 border-b border-border/50 px-2 text-sm hover:bg-accent/40",
                      grid,
                    )}
                    style={{ top: row.start, height: ROW_H }}
                  >
                    <GripVertical className="h-3.5 w-3.5 text-muted-foreground" />
                    <span className="truncate font-mono text-xs" title={file.path}>
                      {file.manual_rank !== null && file.manual_rank !== undefined && (
                        <span className="mr-1 rounded bg-primary/15 px-1 text-primary">
                          #{file.manual_rank}
                        </span>
                      )}
                      {basename(file.path)}
                    </span>
                    <span className="font-mono text-xs">{file.video_codec ?? "—"}</span>
                    <span className="font-mono text-xs">
                      {file.resolution_bucket ?? "—"}
                      {file.hdr_format && file.hdr_format !== "sdr" ? " HDR" : ""}
                    </span>
                    <span className="font-mono text-xs">{formatBitrate(file.video_bitrate_kbps)}</span>
                    <span className="font-mono text-xs text-success">
                      {formatBytes(file.estimated_gain_bytes)}
                    </span>
                    <span className="flex min-w-0 items-center gap-1 text-xs">
                      {(file.attempt_count ?? 0) > 0 ? (
                        <>
                          <AttemptBadge attempt={(file.attempt_count ?? 0) + 1} />
                          <StrategyBadge strategy={file.next_strategy} />
                          {delay && <span className="truncate text-muted-foreground">{delay}</span>}
                        </>
                      ) : (
                        <span className="text-muted-foreground">{t("1er essai")}</span>
                      )}
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      title={t("Prioriser (tête de queue)")}
                      onClick={() => reorder.mutate([file.id])}
                    >
                      <ArrowUpToLine className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
        <div className="border-t px-2 py-1.5 text-xs text-muted-foreground">
          {(data?.meta.total ?? 0).toLocaleString()} {t("fichier(s) en attente — glisser pour réordonner")}
          {(data?.meta.total ?? 0) > 1000 && ` · ${t("1 000 premiers affichés")}`}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// En cours
// ---------------------------------------------------------------------------
function ActiveTab({ onDetails }: { onDetails: (jobId: number) => void }) {
  const { t, tPhase } = useI18n();
  const { active } = useQueueSocket();
  const jobAction = useJobAction();
  return (
    <div className="space-y-2">
      {active.length === 0 && (
        <div className="rounded-md border py-10 text-center text-sm text-muted-foreground">
          {t("Aucun encodage en cours.")}
        </div>
      )}
      {active.map((job) => (
        <div key={job.job_id} className="flex flex-wrap items-center gap-3 rounded-md border px-3 py-2">
          <div className="min-w-0 flex-1 basis-72">
            <button
              className="block max-w-full truncate text-left font-mono text-xs hover:underline"
              title={job.path}
              onClick={() => onDetails(job.job_id)}
            >
              {job.path || t("Préparation…")}
            </button>
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
              {job.phase && <span className="text-[11px] text-primary">{tPhase(job.phase)}</span>}
              <StrategyBadge strategy={job.strategy} />
              <AttemptBadge attempt={job.attempt} />
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded bg-muted">
              <div className="h-full bg-primary transition-all" style={{ width: `${job.percent ?? 0}%` }} />
            </div>
          </div>
          <span className="w-16 text-right font-mono text-xs">{formatPercent(job.percent ?? 0)}</span>
          <span className="w-16 text-right font-mono text-xs">{job.fps ? `${job.fps.toFixed(0)} fps` : "—"}</span>
          <span className="w-14 text-right font-mono text-xs">{job.speed ? `${job.speed.toFixed(2)}x` : "—"}</span>
          <span className="w-16 text-right font-mono text-xs">{formatDuration(job.eta_s)}</span>
          <Button
            size="sm"
            variant="ghost"
            className="text-destructive"
            onClick={() => jobAction.mutate({ id: job.job_id, action: "cancel" })}
          >
            {t("Annuler")}
          </Button>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Historique
// ---------------------------------------------------------------------------
const OUTCOMES: { value: string; label: string }[] = [
  { value: "finished", label: "Tous" },
  { value: "success", label: "Réussis" },
  { value: "recovered", label: "Récupérés" },
  { value: "failed", label: "Échecs" },
  { value: "no_gain", label: "Sans gain" },
  { value: "cancelled", label: "Annulés" },
];

function toCsv(jobs: Job[]): string {
  const header = [
    "date", "fichier", "bibliotheque", "etat", "cause", "strategie", "tentative",
    "codec_avant", "codec_apres", "taille_avant", "taille_apres", "gain_octets",
    "gain_pourcent", "duree_s", "erreur",
  ];
  const esc = (v: unknown) => `"${String(v ?? "").replaceAll('"', '""')}"`;
  const rows = jobs.map((j) =>
    [
      j.finished_at ?? "", esc(j.src_path), j.library_id, j.state, j.failure_category ?? "",
      j.strategy ?? "", j.attempt ?? 1, j.src_codec ?? "", j.dst_codec ?? "",
      j.src_size_bytes ?? "", j.dst_size_bytes ?? "", j.bytes_saved ?? "", j.gain_percent ?? "",
      j.duration_s ?? "", esc(j.error_message),
    ].join(","),
  );
  return [header.join(","), ...rows].join("\n");
}

function HistoryTab({
  search,
  params,
  setParam,
  onDetails,
}: {
  search: string;
  params: URLSearchParams;
  setParam: (key: string, value: string | null) => void;
  onDetails: (jobId: number) => void;
}) {
  const t = useT();
  const { data: libraries } = useLibraries();
  const outcome = params.get("outcome") ?? "finished";
  const category = params.get("category") ?? "";
  const strategy = params.get("strategy") ?? "";
  const libraryId = params.get("library") ?? "";
  const sort = params.get("sort") ?? "recent";
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [outcome, category, strategy, libraryId, sort, search]);

  const { data, isFetching } = useJobs({
    outcome: category ? "failed" : outcome,
    failure_category: category || undefined,
    strategy: strategy || undefined,
    library_id: libraryId ? Number(libraryId) : undefined,
    search,
    sort,
    limit: PAGE,
    offset,
  });
  const libraryName = (id: number) => libraries?.find((l) => l.id === id)?.name ?? `#${id}`;

  const exportCsv = () => {
    const blob = new Blob([toCsv(data?.items ?? [])], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "transcodarr-historique.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex h-8 flex-wrap items-center rounded-md bg-muted p-0.5" role="radiogroup">
          {OUTCOMES.map((o) => (
            <button
              key={o.value}
              role="radio"
              aria-checked={!category && outcome === o.value}
              onClick={() => {
                setParam("category", null);
                setParam("outcome", o.value === "finished" ? null : o.value);
              }}
              className={cn(
                "rounded px-2.5 py-1 text-xs font-medium text-muted-foreground",
                !category && outcome === o.value && "bg-background text-foreground shadow-sm",
              )}
            >
              {t(o.label)}
            </button>
          ))}
        </div>
        <Select className="w-40" value={libraryId} onChange={(e) => setParam("library", e.target.value || null)}>
          <option value="">{t("Toutes les bibliothèques")}</option>
          {(libraries ?? []).map((lib) => (
            <option key={lib.id} value={lib.id}>
              {lib.name}
            </option>
          ))}
        </Select>
        <Select className="w-52" value={category} onChange={(e) => setParam("category", e.target.value || null)}>
          <option value="">{t("Toutes les causes")}</option>
          {Object.entries(FAILURE_CATEGORIES).map(([key, meta]) => (
            <option key={key} value={key}>
              {t(meta.label)}
            </option>
          ))}
        </Select>
        <Select className="w-40" value={strategy} onChange={(e) => setParam("strategy", e.target.value || null)}>
          <option value="">{t("Toutes les méthodes")}</option>
          {Object.entries(STRATEGIES).map(([key, meta]) => (
            <option key={key} value={key}>
              {t(meta.label)}
            </option>
          ))}
        </Select>
        <Select className="w-44" value={sort} onChange={(e) => setParam("sort", e.target.value === "recent" ? null : e.target.value)}>
          <option value="recent">{t("Plus récents")}</option>
          <option value="gain">{t("Plus gros gain")}</option>
          <option value="size">{t("Plus gros fichiers")}</option>
          <option value="duration">{t("Plus longs")}</option>
          <option value="attempt">{t("Plus de tentatives")}</option>
        </Select>
        <Button variant="secondary" size="sm" className="ml-auto" onClick={exportCsv}>
          <Download className="h-3.5 w-3.5" /> CSV
        </Button>
      </div>

      <div className={cn("overflow-x-auto rounded-md border transition-opacity", isFetching && "opacity-70")}>
        <table className="w-full min-w-[860px] text-sm">
          <thead>
            <tr className="border-b bg-card text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="px-2 py-2">{t("Date")}</th>
              <th className="px-2 py-2">{t("Fichier")}</th>
              <th className="px-2 py-2">{t("Issue")}</th>
              <th className="px-2 py-2">{t("Méthode")}</th>
              <th className="px-2 py-2">{t("Taille")}</th>
              <th className="px-2 py-2 text-right">{t("Gain")}</th>
              <th className="px-2 py-2 text-right">{t("Durée")}</th>
            </tr>
          </thead>
          <tbody>
            {(data?.items ?? []).map((job) => (
              <tr
                key={job.id}
                className="cursor-pointer border-b border-border/50 hover:bg-accent/40"
                onClick={() => onDetails(job.id)}
              >
                <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">
                  {formatDate(job.finished_at ?? job.started_at)}
                </td>
                <td className="max-w-[18rem] px-2 py-1.5">
                  <div className="truncate font-mono text-xs" title={job.src_path ?? ""}>
                    {basename(job.src_path ?? "")}
                  </div>
                  <div className="text-[11px] text-muted-foreground">{libraryName(job.library_id)}</div>
                </td>
                <td className="px-2 py-1.5">
                  <div className="flex flex-wrap items-center gap-1">
                    <StatusBadge status={job.state} />
                    {job.failure_category && <FailureBadge category={job.failure_category} />}
                    {job.dry_run && <span className="text-[10px] font-bold text-warning">DRY</span>}
                  </div>
                  {job.state === "skipped_no_gain" && job.strategy === "preflight" && (
                    <div className="text-[11px] text-muted-foreground">{t("écarté avant encodage")}</div>
                  )}
                </td>
                <td className="px-2 py-1.5">
                  <div className="flex flex-wrap items-center gap-1">
                    <StrategyBadge strategy={job.strategy} hideStandard={false} />
                    <AttemptBadge attempt={job.attempt} />
                  </div>
                </td>
                <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">
                  <div>
                    {formatBytes(job.src_size_bytes)} → {formatBytes(job.dst_size_bytes)}
                  </div>
                  <div className="text-[11px] text-muted-foreground">
                    {job.src_codec ?? "—"} → {job.dst_codec ?? "—"}
                  </div>
                </td>
                <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-xs text-success">
                  {job.bytes_saved != null && job.state === "success" ? (
                    <>
                      <div>{formatBytes(job.bytes_saved)}</div>
                      <div className="text-[11px]">{formatPercent(job.gain_percent)}</div>
                    </>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-xs">
                  {formatDuration(job.duration_s)}
                </td>
              </tr>
            ))}
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={7} className="py-10 text-center text-sm text-muted-foreground">
                  {t("Aucun job ne correspond à ces filtres.")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {(data?.meta.total ?? 0).toLocaleString()} {t("job(s) — page")} {Math.floor(offset / PAGE) + 1}
          {" / "}
          {Math.max(1, Math.ceil((data?.meta.total ?? 0) / PAGE))}
        </span>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            {t("Précédent")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={offset + PAGE >= (data?.meta.total ?? 0)}
            onClick={() => setOffset(offset + PAGE)}
          >
            {t("Suivant")}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Échecs (par fichier)
// ---------------------------------------------------------------------------
function FailuresTab({
  search,
  params,
  setParam,
  onFileDetails,
}: {
  search: string;
  params: URLSearchParams;
  setParam: (key: string, value: string | null) => void;
  onFileDetails: (fileId: number) => void;
}) {
  const t = useT();
  const category = params.get("category") ?? "";
  const libraryId = params.get("library") ?? "";
  const { data: libraries } = useLibraries();
  const { data: failureStats } = useFailures(null);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [message, setMessage] = useState<string | null>(null);
  const retry = useRetryFailed();
  const fileAction = useFileAction();
  useEffect(() => {
    setOffset(0);
    setSelected(new Set());
  }, [category, libraryId, search]);

  const { data, isFetching } = useFiles({
    status: "failed",
    failure_category: category || undefined,
    library_id: libraryId ? Number(libraryId) : undefined,
    search,
    order: "recent",
    with_job_count: true,
    limit: PAGE,
    offset,
  });
  const items = data?.items ?? [];
  const total = data?.meta.total ?? 0;
  const chips = (failureStats?.categories ?? []).filter((c) => c.failed_files > 0);
  const libraryName = (id: number) => libraries?.find((l) => l.id === id)?.name ?? `#${id}`;
  const allOnPageSelected = items.length > 0 && items.every((f) => selected.has(f.id));

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const done = (detail: string | null | undefined) => {
    setMessage(detail ?? null);
    setSelected(new Set());
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          onClick={() => setParam("category", null)}
          className={cn(
            "rounded-full border px-2.5 py-1 text-xs",
            !category ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent",
          )}
        >
          {t("Toutes les causes")} ({failureStats?.failed_files_total ?? 0})
        </button>
        {chips.map((c) => (
          <button
            key={c.category}
            onClick={() => setParam("category", c.category)}
            title={t(failureMeta(c.category).explanation)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-xs",
              category === c.category
                ? "border-destructive bg-destructive/10 text-destructive"
                : "text-muted-foreground hover:bg-accent",
            )}
          >
            {t(failureMeta(c.category).label)} ({c.failed_files})
          </button>
        ))}
      </div>

      {category && (
        <div className="rounded-md border bg-card p-3 text-sm">
          <div className="font-medium">{t(failureMeta(category).label)}</div>
          <div className="mt-0.5 text-muted-foreground">{t(failureMeta(category).explanation)}</div>
          <div className="mt-1 text-xs text-muted-foreground">
            <span className="font-medium">{t("Remède :")}</span> {t(failureMeta(category).remedy)}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Select className="w-40" value={libraryId} onChange={(e) => setParam("library", e.target.value || null)}>
          <option value="">{t("Toutes les bibliothèques")}</option>
          {(libraries ?? []).map((lib) => (
            <option key={lib.id} value={lib.id}>
              {lib.name}
            </option>
          ))}
        </Select>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {message && <span className="text-xs text-muted-foreground">{message}</span>}
          {selected.size > 0 && (
            <>
              <Button
                size="sm"
                disabled={retry.isPending}
                onClick={() => retry.mutate({ file_ids: [...selected] }, { onSuccess: (d) => done(d.detail) })}
              >
                <RotateCcw className="h-3.5 w-3.5" /> {t("Relancer la sélection")} ({selected.size})
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={async () => {
                  for (const id of selected) await fileAction.mutateAsync({ id, action: "ignore" });
                  done(`${selected.size} ${t("fichier(s) ignoré(s)")}`);
                }}
              >
                <Ban className="h-3.5 w-3.5" /> {t("Ignorer la sélection")}
              </Button>
            </>
          )}
          <Button
            size="sm"
            variant="secondary"
            disabled={retry.isPending || total === 0}
            onClick={() =>
              retry.mutate(
                {
                  failure_category: category || undefined,
                  library_id: libraryId ? Number(libraryId) : undefined,
                },
                { onSuccess: (d) => done(d.detail) },
              )
            }
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {category || libraryId ? t("Relancer ce filtre") : t("Relancer tous les échecs")} ({total})
          </Button>
        </div>
      </div>

      {/* Mobile : cartes */}
      <ul className={cn("space-y-2 md:hidden", isFetching && "opacity-70")}>
        {items.map((file) => (
          <li key={file.id} className="rounded-md border bg-card p-3">
            <div className="flex items-start gap-2">
              <input
                type="checkbox"
                className="mt-1"
                aria-label={basename(file.path)}
                checked={selected.has(file.id)}
                onChange={() => toggle(file.id)}
              />
              <button className="min-w-0 flex-1 text-left" onClick={() => onFileDetails(file.id)}>
                <div className="break-all font-mono text-xs">{basename(file.path)}</div>
                <div className="mt-0.5 text-[11px] text-muted-foreground">
                  {libraryName(file.library_id)} · {formatBytes(file.size_bytes)} ·{" "}
                  {(file.job_count ?? 0).toLocaleString()} {t("tent.")}
                </div>
                <div className="mt-1.5">
                  <FailureBadge category={file.failure_category ?? "unknown"} />
                </div>
              </button>
              <Button
                size="icon"
                variant="ghost"
                aria-label={t("Relancer avec toutes les méthodes")}
                onClick={() => retry.mutate({ file_ids: [file.id] }, { onSuccess: (d) => done(d.detail) })}
              >
                <RotateCcw className="h-4 w-4" />
              </Button>
            </div>
          </li>
        ))}
        {data && items.length === 0 && (
          <li className="rounded-md border py-10 text-center text-sm text-muted-foreground">
            {t("Aucun fichier en échec définitif.")}
          </li>
        )}
      </ul>

      {/* Bureau : tableau */}
      <div className={cn("hidden overflow-x-auto rounded-md border transition-opacity md:block", isFetching && "opacity-70")}>
        <table className="w-full min-w-[860px] text-sm">
          <thead>
            <tr className="border-b bg-card text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="w-8 px-2 py-2">
                <input
                  type="checkbox"
                  aria-label={t("Tout sélectionner")}
                  checked={allOnPageSelected}
                  onChange={() =>
                    setSelected((prev) => {
                      const next = new Set(prev);
                      for (const f of items) {
                        if (allOnPageSelected) next.delete(f.id);
                        else next.add(f.id);
                      }
                      return next;
                    })
                  }
                />
              </th>
              <th className="px-2 py-2">{t("Fichier")}</th>
              <th className="px-2 py-2">{t("Cause")}</th>
              <th className="px-2 py-2">{t("Méthodes essayées")}</th>
              <th className="px-2 py-2 text-right">{t("Tentatives")}</th>
              <th className="px-2 py-2" />
            </tr>
          </thead>
          <tbody>
            {items.map((file) => (
              <tr
                key={file.id}
                className="cursor-pointer border-b border-border/50 align-top hover:bg-accent/40"
                onClick={() => onFileDetails(file.id)}
              >
                <td className="px-2 py-2" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    aria-label={basename(file.path)}
                    checked={selected.has(file.id)}
                    onChange={() => toggle(file.id)}
                  />
                </td>
                <td className="max-w-sm px-2 py-2">
                  <div className="truncate font-mono text-xs" title={file.path}>
                    {basename(file.path)}
                  </div>
                  <div className="text-[11px] text-muted-foreground">
                    {libraryName(file.library_id)} · {file.video_codec ?? "?"} · {formatBytes(file.size_bytes)}
                  </div>
                </td>
                <td className="max-w-md px-2 py-2">
                  <FailureBadge category={file.failure_category ?? "unknown"} />
                  {file.last_error && (
                    <div className="mt-0.5 line-clamp-2 break-all font-mono text-[11px] text-muted-foreground" title={file.last_error}>
                      {file.last_error}
                    </div>
                  )}
                </td>
                <td className="px-2 py-2">
                  <div className="flex flex-wrap gap-1">
                    {(file.strategies_tried ?? []).length > 0 ? (
                      (file.strategies_tried ?? []).map((s) => (
                        <span key={s} className="rounded bg-muted px-1.5 py-0.5 text-[11px]" title={t(strategyMeta(s).description)}>
                          {t(strategyMeta(s).label)}
                        </span>
                      ))
                    ) : (
                      <span className="text-[11px] text-muted-foreground">{t("avant les replis")}</span>
                    )}
                  </div>
                </td>
                <td className="px-2 py-2 text-right font-mono text-xs">{(file.job_count ?? 0).toLocaleString()}</td>
                <td className="px-2 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                  <Button
                    size="sm"
                    variant="ghost"
                    title={t("Relancer avec toutes les méthodes")}
                    onClick={() => retry.mutate({ file_ids: [file.id] }, { onSuccess: (d) => done(d.detail) })}
                  >
                    <RotateCcw className="h-3.5 w-3.5" />
                  </Button>
                </td>
              </tr>
            ))}
            {data && items.length === 0 && (
              <tr>
                <td colSpan={6} className="py-10 text-center text-sm text-muted-foreground">
                  {t("Aucun fichier en échec définitif.")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {total > PAGE && (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>
            {total.toLocaleString()} {t("fichier(s)")} — {t("page")} {Math.floor(offset / PAGE) + 1}
          </span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              {t("Précédent")}
            </Button>
            <Button variant="outline" size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
              {t("Suivant")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Réévaluation des « sans gain » estimés
// ---------------------------------------------------------------------------
function NoGainReviewDialog({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: (detail: string) => void;
}) {
  const t = useT();
  const { data: libraries } = useLibraries();
  const [libraryId, setLibraryId] = useState<number | undefined>(undefined);
  const preview = useReviewNoGain();
  const apply = useReviewNoGain();
  const previewMutate = preview.mutate;

  useEffect(() => {
    if (open) previewMutate({ library_id: libraryId, dry_run: true });
  }, [open, libraryId, previewMutate]);

  const data = preview.data;
  const rows: { label: string; value: number | undefined; tone?: "good" | "muted" }[] = [
    { label: t("Abandons anticipés (taille projetée à 15 %)"), value: data?.early_aborts },
    { label: t("Prédictions du pré-vol ou du scan"), value: data?.predictions },
    { label: t("→ remis en file pour un encodage complet"), value: data?.requeued, tone: "good" },
    { label: t("→ confirmés sans gain (bitrate source déjà proche de la cible)"), value: data?.still_hopeless, tone: "muted" },
    { label: t("→ non évaluables (politique ou règle absente)"), value: data?.not_evaluable, tone: "muted" },
    { label: t("Gains mesurés après un encodage complet : conservés"), value: data?.measured_kept, tone: "muted" },
  ];

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogTitle>{t("Réévaluer les fichiers « sans gain »")}</DialogTitle>
        <p className="text-sm text-muted-foreground">
          {t(
            "Seules les décisions estimées sont réévaluées : l'ancien abandon anticipé jugeait « sans gain » des fichiers qui gagnent en réalité 15 à 58 %. Chaque fichier repasse la prédiction actuelle ; ceux qui laissent espérer un gain sont remis en file et encodés en entier, avec les 9 validations habituelles.",
          )}
        </p>
        <div className="mt-4 space-y-3">
          <Select
            value={libraryId ?? ""}
            onChange={(e) => setLibraryId(e.target.value ? Number(e.target.value) : undefined)}
            aria-label={t("Bibliothèque")}
          >
            <option value="">{t("Toutes les bibliothèques")}</option>
            {(libraries ?? []).map((lib) => (
              <option key={lib.id} value={lib.id}>
                {lib.name}
              </option>
            ))}
          </Select>
          {preview.isPending || !data ? (
            <div className="py-6 text-center text-sm text-muted-foreground">
              {preview.isError ? t("Aperçu impossible.") : t("Calcul de l'aperçu…")}
            </div>
          ) : (
            <dl className="space-y-1.5 rounded-md border p-3 text-sm">
              {rows.map((row) => (
                <div key={row.label} className="flex items-baseline justify-between gap-4">
                  <dt className={cn(row.tone === "muted" && "text-muted-foreground")}>{row.label}</dt>
                  <dd
                    className={cn(
                      "font-mono",
                      row.tone === "good" && "font-semibold text-success",
                      row.tone === "muted" && "text-muted-foreground",
                    )}
                  >
                    {(row.value ?? 0).toLocaleString()}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {t("Annuler")}
          </Button>
          <Button
            disabled={!data || data.requeued === 0 || apply.isPending || preview.isPending}
            onClick={() =>
              apply.mutate(
                { library_id: libraryId, dry_run: false },
                {
                  onSuccess: (result) => {
                    onDone(result.detail);
                    onClose();
                  },
                },
              )
            }
          >
            <RefreshCw className="h-3.5 w-3.5" />
            {t("Remettre en file")} ({(data?.requeued ?? 0).toLocaleString()})
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export function QueuePage() {
  const t = useT();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as TabId | null) ?? "waiting";
  const [searchInput, setSearchInput] = useState(params.get("q") ?? "");
  const search = useDebounced(searchInput);
  const [detailJobId, setDetailJobId] = useState<number | null>(null);
  const [detailFileId, setDetailFileId] = useState<number | null>(null);
  const [actionResult, setActionResult] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const invalidate = useInvalidate();
  const { data: queue } = useQueueState();
  const { active } = useQueueSocket();
  const { data: failureStats } = useFailures(null);

  const setParam = useMemo(
    () => (key: string, value: string | null) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value === null || value === "") next.delete(key);
          else next.set(key, value);
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );

  const runAction = async (path: string) => {
    const resp = await fetch(path, { method: "POST" });
    const data = (await resp.json()) as { detail?: string };
    setActionResult(data.detail ?? t("fait"));
    invalidate("files", "queue", "jobs", "stats");
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-bold">{t("Queue")}</h1>
        <div className="flex flex-wrap items-center gap-2">
          {actionResult && <span className="text-xs text-muted-foreground">{actionResult}</span>}
          <Button
            variant="secondary"
            size="sm"
            title={t("Remet en queue les jobs figés en running/validating qui ne tournent plus (après un arrêt brutal)")}
            onClick={() => void runAction("/api/v1/queue/recover")}
          >
            <RotateCcw className="h-3.5 w-3.5" /> {t("Relancer les jobs bloqués")}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            title={t("Re-queue les fichiers traités uniquement en dry-run (après passage en réel)")}
            onClick={() => void runAction("/api/v1/files/requeue-dry-run")}
          >
            <RefreshCw className="h-3.5 w-3.5" /> {t("Re-queuer les dry-run")}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            title={t("Réévalue les fichiers jugés « sans gain » sur une estimation (abandon anticipé, prédiction)")}
            onClick={() => setReviewOpen(true)}
          >
            <TrendingDown className="h-3.5 w-3.5" /> {t("Réévaluer les « sans gain »")}
          </Button>
          <div className="relative w-full sm:w-64">
            <Search className="absolute left-2 top-2 h-4 w-4 text-muted-foreground" />
            <Input
              className="pl-7"
              placeholder={t("Rechercher par chemin…")}
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
            />
          </div>
        </div>
      </div>

      <Tabs
        value={tab}
        onValueChange={(value) =>
          setParams(
            (prev) => {
              const next = new URLSearchParams();
              next.set("tab", value);
              const q = prev.get("q");
              if (q) next.set("q", q);
              return next;
            },
            { replace: true },
          )
        }
      >
        <TabsList className="h-auto flex-wrap">
          <TabsTrigger value="waiting">
            {t("En attente")} <Count n={queue?.waiting_count} />
          </TabsTrigger>
          <TabsTrigger value="active">
            {t("En cours")} <Count n={active.length} />
          </TabsTrigger>
          <TabsTrigger value="history">{t("Historique")}</TabsTrigger>
          <TabsTrigger value="failures">
            {t("Échecs")} <Count n={failureStats?.failed_files_total} tone="bad" />
          </TabsTrigger>
        </TabsList>
        <div className="mt-3">
          <TabsContent value="waiting">
            <WaitingTab search={search} />
          </TabsContent>
          <TabsContent value="active">
            <ActiveTab onDetails={setDetailJobId} />
          </TabsContent>
          <TabsContent value="history">
            <HistoryTab search={search} params={params} setParam={setParam} onDetails={setDetailJobId} />
          </TabsContent>
          <TabsContent value="failures">
            <FailuresTab search={search} params={params} setParam={setParam} onFileDetails={setDetailFileId} />
          </TabsContent>
        </div>
      </Tabs>

      <NoGainReviewDialog
        open={reviewOpen}
        onClose={() => setReviewOpen(false)}
        onDone={setActionResult}
      />
      <JobDetailDialog
        jobId={detailJobId}
        fileId={detailFileId}
        onClose={() => {
          setDetailJobId(null);
          setDetailFileId(null);
        }}
      />
    </div>
  );
}

function Count({ n, tone }: { n: number | null | undefined; tone?: "bad" }) {
  if (n === null || n === undefined) return null;
  return (
    <span
      className={cn(
        "ml-1.5 rounded-full px-1.5 py-px font-mono text-[10px]",
        tone === "bad" && n > 0 ? "bg-destructive/15 text-destructive" : "bg-background/60 text-muted-foreground",
      )}
    >
      {n.toLocaleString()}
    </span>
  );
}
