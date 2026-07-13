/** Historique (§7.3) : table complète, export CSV, ligne dépliable. */

import { ChevronDown, ChevronRight, Copy, Download, RotateCcw } from "lucide-react";
import { Fragment, useState } from "react";
import type { Job } from "@/api/client";
import { useJobAction, useJobDetail, useJobs, useLibraries } from "@/api/hooks";
import { JobLogViewer } from "@/components/JobLogViewer";
import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { useT } from "@/lib/i18n";
import {
  basename,
  formatBitrate,
  formatBytes,
  formatDate,
  formatDuration,
  formatPercent,
  hdrLabel,
} from "@/lib/format";

const JOB_STATES = [
  "", "queued", "running", "validating", "replacing", "success", "failed",
  "cancelled", "skipped_no_gain",
];

function toCsv(jobs: Job[]): string {
  const header = [
    "date", "fichier", "bibliotheque", "gpu", "duree_s", "codec_avant",
    "codec_apres", "resolution", "hdr_avant", "hdr_apres", "bitrate_avant_kbps",
    "bitrate_apres_kbps", "taille_avant", "taille_apres", "gain_octets",
    "gain_pourcent", "etat",
  ];
  const rows = jobs.map((j) =>
    [
      j.finished_at ?? "",
      `"${(j.src_path ?? "").replaceAll('"', '""')}"`,
      j.library_id,
      j.gpu_id ?? "",
      j.duration_s ?? "",
      j.src_codec ?? "",
      j.dst_codec ?? "",
      j.src_resolution ?? "",
      j.src_hdr_format ?? "",
      j.dst_hdr_format ?? "",
      j.src_bitrate_kbps ?? "",
      j.dst_bitrate_kbps ?? "",
      j.src_size_bytes ?? "",
      j.dst_size_bytes ?? "",
      j.bytes_saved ?? "",
      j.gain_percent ?? "",
      j.state,
    ].join(","),
  );
  return [header.join(","), ...rows].join("\n");
}

function ExpandedRow({ jobId }: { jobId: number }) {
  const t = useT();
  const { data: job } = useJobDetail(jobId);
  if (!job) return <div className="p-4 text-sm text-muted-foreground">{t("Chargement…")}</div>;
  const cmd = (job.ffmpeg_cmd ?? []).join(" ");
  return (
    <div className="space-y-3 bg-accent/20 p-4">
      {/* Comparatif avant/après côte à côte (§7.3) */}
      <div className="grid grid-cols-2 gap-4">
        {(["avant", "après"] as const).map((side) => {
          const isSrc = side === "avant";
          const sideLabel = t(side);
          return (
            <div key={side} className="rounded-md border bg-card p-3">
              <div className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
                {sideLabel}
              </div>
              <dl className="space-y-1 font-mono text-xs">
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Codec</dt>
                  <dd>{(isSrc ? job.src_codec : job.dst_codec) ?? "—"}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Résolution</dt>
                  <dd>{(isSrc ? job.src_resolution : job.dst_resolution) ?? "—"}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">HDR</dt>
                  <dd>{hdrLabel(isSrc ? job.src_hdr_format : job.dst_hdr_format)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Bitrate</dt>
                  <dd>{formatBitrate(isSrc ? job.src_bitrate_kbps : job.dst_bitrate_kbps)}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Taille</dt>
                  <dd>{formatBytes(isSrc ? job.src_size_bytes : job.dst_size_bytes)}</dd>
                </div>
              </dl>
            </div>
          );
        })}
      </div>

      {job.validation_report && (
        <div className="rounded-md border bg-card p-3">
          <div className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
            {t("Rapport de validation")}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {(
              (job.validation_report as { checks?: { name: string; ok: boolean; detail: string }[] })
                .checks ?? []
            ).map((check) => (
              <span
                key={check.name}
                title={check.detail}
                className={`rounded px-1.5 py-0.5 font-mono text-xs ${
                  check.ok ? "bg-success/15 text-success" : "bg-destructive/15 text-destructive"
                }`}
              >
                {check.name}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-md border bg-card p-3">
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase text-muted-foreground">
            {t("Commande FFmpeg")}
          </span>
          <Button size="sm" variant="ghost" onClick={() => void navigator.clipboard.writeText(cmd)}>
            <Copy className="h-3.5 w-3.5" /> {t("Copier")}
          </Button>
        </div>
        <code className="block max-h-32 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-muted-foreground">
          {cmd || "—"}
        </code>
      </div>

      <JobLogViewer jobId={jobId} live={["running", "validating", "replacing"].includes(job.state)} />
    </div>
  );
}

export function HistoryPage() {
  const t = useT();
  const [state, setState] = useState("");
  const [libraryId, setLibraryId] = useState("");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  const limit = 50;
  const { data: libraries } = useLibraries();
  const { data } = useJobs({
    state: state || undefined,
    library_id: libraryId ? Number(libraryId) : undefined,
    search: search || undefined,
    limit,
    offset,
  });
  const jobAction = useJobAction();

  const exportCsv = () => {
    const blob = new Blob([toCsv(data?.items ?? [])], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "historique-transcodage.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-bold">{t("Historique")}</h1>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={libraryId} onChange={(e) => setLibraryId(e.target.value)} className="w-40">
            <option value="">{t("Toutes les bibliothèques")}</option>
            {(libraries ?? []).map((lib) => (
              <option key={lib.id} value={lib.id}>
                {lib.name}
              </option>
            ))}
          </Select>
          <Select value={state} onChange={(e) => setState(e.target.value)} className="w-40">
            {JOB_STATES.map((s) => (
              <option key={s} value={s}>
                {s === "" ? t("Tous les états") : s}
              </option>
            ))}
          </Select>
          <Input
            className="w-56"
            placeholder={t("Rechercher un fichier…")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Button variant="secondary" onClick={exportCsv}>
            <Download className="h-4 w-4" /> CSV
          </Button>
        </div>
      </div>

      <div className="overflow-auto rounded-md border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-card text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="w-6 px-2 py-2" />
              <th className="px-2 py-2">{t("Date")}</th>
              <th className="px-2 py-2">{t("Fichier")}</th>
              <th className="px-2 py-2">{t("Durée")}</th>
              <th className="px-2 py-2">{t("Codec")}</th>
              <th className="px-2 py-2">{t("HDR")}</th>
              <th className="px-2 py-2">{t("Bitrate")}</th>
              <th className="px-2 py-2">{t("Taille")}</th>
              <th className="px-2 py-2 text-right">{t("Gain")}</th>
              <th className="px-2 py-2">{t("État")}</th>
              <th className="px-2 py-2" />
            </tr>
          </thead>
          <tbody>
            {(data?.items ?? []).map((job) => (
              <Fragment key={job.id}>
                <tr
                  className="cursor-pointer border-b border-border/50 hover:bg-accent/40"
                  onClick={() => setExpanded(expanded === job.id ? null : job.id)}
                >
                  <td className="px-2 py-1.5">
                    {expanded === job.id ? (
                      <ChevronDown className="h-4 w-4" />
                    ) : (
                      <ChevronRight className="h-4 w-4" />
                    )}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">
                    {formatDate(job.finished_at ?? job.started_at)}
                  </td>
                  <td className="max-w-xs px-2 py-1.5">
                    <div className="flex items-center gap-1">
                      <span className="truncate font-mono text-xs" title={job.src_path ?? ""}>
                        {basename(job.src_path ?? "")}
                      </span>
                      <button
                        title={t("Copier le chemin complet")}
                        onClick={(e) => {
                          e.stopPropagation();
                          void navigator.clipboard.writeText(job.src_path ?? "");
                        }}
                        className="text-muted-foreground hover:text-foreground"
                      >
                        <Copy className="h-3 w-3" />
                      </button>
                    </div>
                  </td>
                  <td className="px-2 py-1.5 font-mono text-xs">{formatDuration(job.duration_s)}</td>
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">
                    {job.src_codec ?? "—"} → {job.dst_codec ?? "—"}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">
                    {hdrLabel(job.src_hdr_format)} → {hdrLabel(job.dst_hdr_format)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">
                    {formatBitrate(job.src_bitrate_kbps)} → {formatBitrate(job.dst_bitrate_kbps)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 font-mono text-xs">
                    {formatBytes(job.src_size_bytes)} → {formatBytes(job.dst_size_bytes)}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-xs text-success">
                    {job.bytes_saved !== null && job.bytes_saved !== undefined
                      ? `${formatBytes(job.bytes_saved)} (${formatPercent(job.gain_percent)})`
                      : "—"}
                  </td>
                  <td className="px-2 py-1.5">
                    <StatusBadge status={job.state} />
                    {job.dry_run && (
                      <span className="ml-1 text-[10px] font-bold text-warning">DRY</span>
                    )}
                  </td>
                  <td className="px-2 py-1.5">
                    {["failed", "cancelled", "skipped_no_gain"].includes(job.state) && (
                      <Button
                        size="sm"
                        variant="ghost"
                        title={t("Réessayer")}
                        onClick={(e) => {
                          e.stopPropagation();
                          jobAction.mutate({ id: job.id, action: "retry" });
                        }}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </Button>
                    )}
                  </td>
                </tr>
                {expanded === job.id && (
                  <tr>
                    <td colSpan={11}>
                      <ExpandedRow jobId={job.id} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {data?.meta.total ?? 0} {t("job(s) — page")} {Math.floor(offset / limit) + 1}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - limit))}
          >
            {t("Précédent")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={offset + limit >= (data?.meta.total ?? 0)}
            onClick={() => setOffset(offset + limit)}
          >
            {t("Suivant")}
          </Button>
        </div>
      </div>
    </div>
  );
}
