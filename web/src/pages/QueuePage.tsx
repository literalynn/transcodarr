/** Queue (§7.2) : 3 onglets, table virtualisée, drag pour réordonner. */

import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowUpToLine, GripVertical, RefreshCw, RotateCcw, Search } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import type { Job, MediaFile } from "@/api/client";
import {
  useFileAction,
  useFiles,
  useInvalidate,
  useJobAction,
  useJobs,
  useReorderQueue,
} from "@/api/hooks";
import { StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  basename,
  formatBitrate,
  formatBytes,
  formatDate,
  formatPercent,
} from "@/lib/format";
import { useI18n, useT } from "@/lib/i18n";
import { useQueueSocket } from "@/ws/useQueueSocket";

const ROW_H = 36;

function WaitingTab({ search }: { search: string }) {
  const t = useT();
  const { data } = useFiles({ status: "queued", search, order: "queue", limit: 1000 });
  const reorder = useReorderQueue();
  const fileAction = useFileAction();
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

  return (
    <div className="overflow-x-auto">
      <div className="min-w-[720px]">
      <div className="grid grid-cols-[24px_1fr_90px_90px_90px_110px_90px] gap-2 border-b px-2 py-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <span />
        <span>{t("Fichier")}</span>
        <span>{t("Codec")}</span>
        <span>{t("Résolution")}</span>
        <span>{t("Bitrate")}</span>
        <span>{t("Gain estimé")}</span>
        <span />
      </div>
      <div ref={parentRef} className="h-[62vh] overflow-auto">
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {virtualizer.getVirtualItems().map((row) => {
            const file = items[row.index];
            if (!file) return null;
            return (
              <div
                key={file.id}
                draggable
                onDragStart={() => setDragIndex(row.index)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => onDrop(row.index)}
                className="absolute left-0 grid w-full cursor-grab grid-cols-[24px_1fr_90px_90px_90px_110px_90px] items-center gap-2 border-b border-border/50 px-2 text-sm hover:bg-accent/40"
                style={{ top: row.start, height: ROW_H }}
              >
                <GripVertical className="h-3.5 w-3.5 text-muted-foreground" />
                <span className="truncate font-mono text-xs" title={file.path}>
                  {file.manual_rank !== null && (
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
                <Button
                  size="sm"
                  variant="ghost"
                  title={t("Prioriser (tête de queue)")}
                  onClick={() => fileAction.mutate({ id: file.id, action: "force" })}
                >
                  <ArrowUpToLine className="h-3.5 w-3.5" />
                </Button>
              </div>
            );
          })}
        </div>
      </div>
      <div className="border-t px-2 py-1.5 text-xs text-muted-foreground">
        {data?.meta.total ?? 0} {t("fichier(s) en attente — glisser pour réordonner")}
      </div>
      </div>
    </div>
  );
}

function ActiveTab() {
  const { t, tPhase } = useI18n();
  const { active } = useQueueSocket();
  const jobAction = useJobAction();
  return (
    <div className="space-y-2 py-2">
      {active.length === 0 && (
        <div className="py-8 text-center text-sm text-muted-foreground">
          {t("Aucun encodage en cours.")}
        </div>
      )}
      {active.map((job) => (
        <div
          key={job.job_id}
          className="flex items-center gap-3 rounded-md border px-3 py-2"
        >
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-xs" title={job.path}>
              {job.path}
            </div>
            {job.phase && (
              <div className="truncate text-[11px] text-primary">{tPhase(job.phase)}</div>
            )}
            <div className="mt-1 h-1.5 overflow-hidden rounded bg-muted">
              <div
                className="h-full bg-primary transition-all"
                style={{ width: `${job.percent ?? 0}%` }}
              />
            </div>
          </div>
          <span className="w-16 text-right font-mono text-xs">
            {formatPercent(job.percent ?? 0)}
          </span>
          <span className="w-16 text-right font-mono text-xs">
            {job.fps ? `${job.fps.toFixed(0)} fps` : "—"}
          </span>
          <span className="w-14 text-right font-mono text-xs">
            {job.speed ? `${job.speed.toFixed(2)}x` : "—"}
          </span>
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

function FinishedTab({ search }: { search: string }) {
  const t = useT();
  const { data } = useJobs({ search, limit: 200 });
  const finished = useMemo(
    () =>
      (data?.items ?? []).filter((j: Job) =>
        ["success", "failed", "cancelled", "skipped_no_gain"].includes(j.state),
      ),
    [data],
  );
  return (
    <div className="max-h-[62vh] overflow-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-background">
          <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th className="px-2 py-1.5">{t("Date")}</th>
            <th className="px-2 py-1.5">{t("Fichier")}</th>
            <th className="px-2 py-1.5">{t("État")}</th>
            <th className="px-2 py-1.5 text-right">{t("Gain")}</th>
          </tr>
        </thead>
        <tbody>
          {finished.map((job) => (
            <tr key={job.id} className="border-b border-border/50 hover:bg-accent/40">
              <td className="px-2 py-1.5 font-mono text-xs">{formatDate(job.finished_at)}</td>
              <td className="max-w-md truncate px-2 py-1.5 font-mono text-xs" title={job.src_path ?? ""}>
                {basename(job.src_path ?? "")}
              </td>
              <td className="px-2 py-1.5">
                <StatusBadge status={job.state} />
                {job.dry_run && <span className="ml-1 text-xs text-warning">dry-run</span>}
              </td>
              <td className="px-2 py-1.5 text-right font-mono text-xs text-success">
                {job.bytes_saved !== null && job.bytes_saved !== undefined
                  ? formatBytes(job.bytes_saved)
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function QueuePage() {
  const t = useT();
  const [search, setSearch] = useState("");
  const [actionResult, setActionResult] = useState<string | null>(null);
  const invalidate = useInvalidate();

  const runAction = async (path: string) => {
    const resp = await fetch(path, { method: "POST" });
    const data = (await resp.json()) as { detail?: string };
    setActionResult(data.detail ?? "fait");
    invalidate("files", "queue", "jobs");
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-lg font-bold">{t("Queue")}</h1>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            title="Remet en queue les jobs figés en running/validating qui ne tournent plus (après un arrêt brutal)"
            onClick={() => void runAction("/api/v1/queue/recover")}
          >
            <RotateCcw className="h-3.5 w-3.5" /> {t("Relancer les jobs bloqués")}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            title="Re-queue les fichiers traités uniquement en dry-run (après passage en réel)"
            onClick={() => void runAction("/api/v1/files/requeue-dry-run")}
          >
            <RefreshCw className="h-3.5 w-3.5" /> {t("Re-queuer les dry-run")}
          </Button>
          {actionResult && (
            <span className="text-xs text-muted-foreground">{actionResult}</span>
          )}
        </div>
        <div className="relative w-full sm:w-72">
          <Search className="absolute left-2 top-2 h-4 w-4 text-muted-foreground" />
          <Input
            className="pl-7"
            placeholder={t("Rechercher par chemin…")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>
      <Tabs defaultValue="waiting">
        <TabsList>
          <TabsTrigger value="waiting">{t("En attente")}</TabsTrigger>
          <TabsTrigger value="active">{t("En cours")}</TabsTrigger>
          <TabsTrigger value="finished">{t("Terminés")}</TabsTrigger>
        </TabsList>
        <TabsContent value="waiting">
          <WaitingTab search={search} />
        </TabsContent>
        <TabsContent value="active">
          <ActiveTab />
        </TabsContent>
        <TabsContent value="finished">
          <FinishedTab search={search} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
