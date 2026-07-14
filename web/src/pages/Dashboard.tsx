/** Dashboard (§7.1) : jobs en cours, KPIs, statut GPU, contrôles globaux. */

import { FileText, Pause, Play, ScanSearch, XCircle } from "lucide-react";
import { useState } from "react";
import {
  useCapabilities,
  useJobAction,
  useLibraries,
  useQueueAction,
  useQueueState,
  useStatsSummary,
  useTriggerScan,
} from "@/api/hooks";
import type { ActiveJob } from "@/api/client";
import { JobLogViewer } from "@/components/JobLogViewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/api/client";
import { basename, formatBytes, formatDuration, formatPercent } from "@/lib/format";
import { useI18n, useT } from "@/lib/i18n";
import { useQueueSocket } from "@/ws/useQueueSocket";

interface ResourcesInfo {
  cpu: { cores: number; app_percent: number | null; other_percent: number | null; host_percent: number | null };
  memory: { total_bytes: number | null; app_bytes: number | null; other_bytes: number | null; used_bytes: number | null };
  gpu: { busy_percent: number | null; freq_mhz: number | null; freq_max_mhz: number | null };
}

function SplitBar({
  label,
  appPart,
  otherPart,
  detail,
}: {
  label: string;
  appPart: number | null;
  otherPart: number | null;
  detail: string;
}) {
  const t = useT();
  return (
    <div>
      <div className="mb-0.5 flex justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono">{detail}</span>
      </div>
      <div className="flex h-2 overflow-hidden rounded bg-muted">
        {appPart !== null && (
          <div className="h-full bg-primary" style={{ width: `${Math.min(100, appPart)}%` }} title={t("app")} />
        )}
        {otherPart !== null && (
          <div
            className="h-full bg-muted-foreground/50"
            style={{ width: `${Math.min(100, otherPart)}%` }}
            title={t("reste du système")}
          />
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
  if (!data) return null;
  const { cpu, memory, gpu } = data;
  const cpuDetail =
    cpu.app_percent !== null
      ? `${t("app")} ${cpu.app_percent.toFixed(0)}% · ${t("reste du système")} ${(cpu.other_percent ?? 0).toFixed(0)}%`
      : t("indisponible");
  const memDetail =
    memory.used_bytes !== null && memory.total_bytes !== null
      ? `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`
      : t("indisponible");
  const gpuDetail =
    gpu.busy_percent !== null
      ? `${gpu.busy_percent.toFixed(0)}%${gpu.freq_mhz ? ` · ${gpu.freq_mhz} MHz` : ""}`
      : gpu.freq_mhz
        ? `${gpu.freq_mhz}/${gpu.freq_max_mhz ?? "?"} MHz`
        : t("indisponible");
  const total = memory.total_bytes ?? 1;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("Ressources")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2.5">
        <SplitBar
          label="CPU"
          appPart={cpu.app_percent}
          otherPart={cpu.other_percent}
          detail={cpuDetail}
        />
        <SplitBar
          label="RAM"
          appPart={null}
          otherPart={memory.used_bytes !== null ? (memory.used_bytes / total) * 100 : null}
          detail={memDetail}
        />
        <SplitBar
          label="GPU"
          appPart={gpu.busy_percent}
          otherPart={null}
          detail={gpuDetail}
        />
        <div className="flex gap-3 text-[10px] text-muted-foreground">
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-primary" /> {t("app")}
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-muted-foreground/50" />{" "}
            {t("reste du système")}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{label}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="font-mono text-2xl font-bold">{value}</div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

function ActiveJobCard({ job, onLogs }: { job: ActiveJob; onLogs: () => void }) {
  const jobAction = useJobAction();
  const { t, tPhase } = useI18n();
  return (
    <Card className="min-w-72 flex-1">
      <CardContent className="space-y-2 p-3">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-mono text-xs" title={job.path}>
            {basename(job.path)}
          </span>
          {job.dry_run && <Badge variant="warning">DRY RUN</Badge>}
        </div>
        <div className="h-1.5 overflow-hidden rounded bg-muted">
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
          <span className="truncate">{job.gpu} · slot {job.slot}</span>
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

export function Dashboard() {
  const t = useT();
  const { active } = useQueueSocket();
  const { data: summary } = useStatsSummary();
  const { data: queue } = useQueueState();
  const { data: caps } = useCapabilities();
  const { data: libraries } = useLibraries();
  const queueAction = useQueueAction();
  const scan = useTriggerScan();
  const [logJobId, setLogJobId] = useState<number | null>(null);

  const paused = queue?.paused ?? false;
  const totalSlots = queue?.workers ?? 0;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">{t("Dashboard")}</h1>
        <div className="flex gap-2">
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

      {/* Bandeau jobs en cours (§7.1) */}
      <div>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t("Jobs en cours")} ({active.length}/{totalSlots} {t("slots")})
        </h2>
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
              />
            ))}
          </div>
        )}
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Kpi
          label={t("Fichiers traités")}
          value={`${summary?.processed ?? 0} / ${summary?.total_files ?? 0}`}
          sub={`${summary?.queued ?? 0} ${t("en file d'attente")}`}
        />
        <Kpi label={t("Espace récupéré")} value={formatBytes(summary?.bytes_saved)} />
        <Kpi
          label={t("Gain moyen")}
          value={formatPercent(summary?.avg_gain_percent)}
        />
        <Kpi
          label={t("Débit")}
          value={`${(summary?.throughput_files_per_hour ?? 0).toFixed(1)} f/h`}
          sub={t("sur 24 h")}
        />
        <Kpi label={t("Temps restant estimé")} value={formatDuration(summary?.eta_s)} />
      </div>

      {/* Ressources machine (app vs système) */}
      <div className="grid gap-3 md:grid-cols-2">
        <ResourcesCard />
      </div>

      {/* Statut GPU */}
      <div>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          GPU
        </h2>
        <div className="grid gap-3 md:grid-cols-2">
          {(caps?.gpus ?? []).map((gpu) => (
            <Card key={gpu.gpu_id}>
              <CardContent className="flex items-center justify-between p-3">
                <div>
                  <div className="text-sm font-medium">{gpu.name}</div>
                  <div className="font-mono text-xs text-muted-foreground">
                    {gpu.drm_node} · {gpu.effective_api.toUpperCase()}
                    {gpu.fell_back_to_vaapi && " (fallback VAAPI)"}
                  </div>
                </div>
                <div className="flex gap-1.5">
                  {Object.entries(gpu.encoders).map(([name, res]) => (
                    <Badge key={name} variant={res.ok ? "success" : "destructive"}>
                      {name}
                    </Badge>
                  ))}
                  {gpu.p5_zero_copy && <Badge variant="info">P5 zéro-copie</Badge>}
                </div>
              </CardContent>
            </Card>
          ))}
          {caps && !caps.libplacebo_dv && (
            <Card className="border-warning">
              <CardContent className="p-3 text-xs text-warning">
                {t("libplacebo sans support Dolby Vision : les fichiers DV P5 seront marqués « non traité (politique) », jamais convertis en best-effort.")}
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      <Dialog open={logJobId !== null} onOpenChange={(open) => !open && setLogJobId(null)}>
        <DialogContent className="max-w-3xl">
          <DialogTitle>{t("Log du job")} #{logJobId}</DialogTitle>
          {logJobId !== null && <JobLogViewer jobId={logJobId} live />}
        </DialogContent>
      </Dialog>
    </div>
  );
}
