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
import { basename, formatBytes, formatDuration, formatPercent } from "@/lib/format";
import { useQueueSocket } from "@/ws/useQueueSocket";

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
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span className="truncate">{job.gpu} · slot {job.slot}</span>
          <div className="flex gap-1">
            <Button size="sm" variant="ghost" onClick={onLogs}>
              <FileText className="h-3.5 w-3.5" /> Logs
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-destructive"
              onClick={() => jobAction.mutate({ id: job.job_id, action: "cancel" })}
            >
              <XCircle className="h-3.5 w-3.5" /> Annuler
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function Dashboard() {
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
        <h1 className="text-lg font-bold">Dashboard</h1>
        <div className="flex gap-2">
          <Button
            variant={paused ? "default" : "secondary"}
            onClick={() => queueAction.mutate({ action: paused ? "resume" : "pause" })}
          >
            {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
            {paused ? "Reprendre" : "Pause"}
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
            <ScanSearch className="h-4 w-4" /> Lancer un scan
          </Button>
        </div>
      </div>

      {/* Bandeau jobs en cours (§7.1) */}
      <div>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Jobs en cours ({active.length}/{totalSlots} slots)
        </h2>
        {active.length === 0 ? (
          <Card>
            <CardContent className="py-6 text-center text-sm text-muted-foreground">
              {paused ? "Queue en pause." : "Aucun encodage en cours."}
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
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Kpi
          label="Fichiers traités"
          value={`${summary?.processed ?? 0} / ${summary?.total_files ?? 0}`}
          sub={`${summary?.queued ?? 0} en file d'attente`}
        />
        <Kpi label="Espace récupéré" value={formatBytes(summary?.bytes_saved)} />
        <Kpi
          label="Gain moyen"
          value={formatPercent(summary?.avg_gain_percent)}
        />
        <Kpi
          label="Débit"
          value={`${(summary?.throughput_files_per_hour ?? 0).toFixed(1)} f/h`}
          sub="sur 24 h"
        />
        <Kpi label="Temps restant estimé" value={formatDuration(summary?.eta_s)} />
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
                libplacebo sans support Dolby Vision : les fichiers DV P5 seront marqués
                « non traité (politique) », jamais convertis en best-effort.
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      <Dialog open={logJobId !== null} onOpenChange={(open) => !open && setLogJobId(null)}>
        <DialogContent className="max-w-3xl">
          <DialogTitle>Log du job #{logJobId}</DialogTitle>
          {logJobId !== null && <JobLogViewer jobId={logJobId} live />}
        </DialogContent>
      </Dialog>
    </div>
  );
}
