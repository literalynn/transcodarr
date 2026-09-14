/** Fenêtre de diagnostic d'un job : pourquoi il a échoué, ce que
 *  Transcodarr fait ensuite, pré-vol, plan de pistes, validation,
 *  historique des tentatives du fichier, commande et log. */

import {
  AlertTriangle,
  ArrowRight,
  Ban,
  CheckCircle2,
  Copy,
  MinusCircle,
  RotateCcw,
  XCircle,
  Zap,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { JobDetail, JobDiagnostics, PreflightCheck, ValidationCheck } from "@/api/client";
import {
  useFileAction,
  useFileDetail,
  useJobAction,
  useJobDetail,
  useLibraries,
} from "@/api/hooks";
import { JobLogViewer } from "@/components/JobLogViewer";
import { Badge, StatusBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AttemptBadge, FailureBadge, StrategyBadge } from "@/components/diagnostics/badges";
import { DECISION_LABELS, failureMeta, strategyMeta } from "@/lib/failures";
import {
  basename,
  formatBitrate,
  formatBytes,
  formatDate,
  formatDuration,
  formatPercent,
  hdrLabel,
} from "@/lib/format";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const CHECK_LABELS: Record<string, string> = {
  "1_exit_code": "Code de sortie FFmpeg",
  "2_output_exists": "Fichier produit",
  "3_codec": "Codec cible",
  "4_duration": "Durée des pistes",
  "5_integrity": "Intégrité de la vidéo",
  "6_streams": "Pistes audio / sous-titres",
  "6b_timestamps": "Minutage des pistes copiées",
  "7_gain": "Gain de taille",
  "8_colorimetry": "Colorimétrie HDR",
  "8b_no_dovi": "Aucun Dolby Vision résiduel",
  "9_dv_colors": "Couleurs Dolby Vision",
};

const PREFLIGHT_LABELS: Record<string, string> = {
  source: "Accès à la source",
  sonde: "Analyse ffprobe",
  gain: "Gain atteignable",
  "décodage": "Test de décodage",
  "stratégie": "Stratégie",
  api: "API GPU",
  pistes: "Plan de pistes",
  disque: "Espace disque",
  assemblage: "Assemblage mkvmerge",
};

const ACTION_VARIANT = {
  encode: "info",
  copy: "default",
  convert: "warning",
  drop: "destructive",
} as const;

const ACTION_LABELS: Record<string, string> = {
  encode: "réencodée",
  copy: "copiée",
  convert: "convertie",
  drop: "exclue",
};

function StatusIcon({ status }: { status: string }) {
  if (status === "ok") return <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />;
  if (status === "warning") return <AlertTriangle className="h-4 w-4 shrink-0 text-warning" />;
  if (status === "error") return <XCircle className="h-4 w-4 shrink-0 text-destructive" />;
  return <MinusCircle className="h-4 w-4 shrink-0 text-muted-foreground" />;
}

function Section({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("rounded-md border bg-card p-3", className)}>
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      {children}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  return (
    <Button
      size="sm"
      variant="ghost"
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      }}
    >
      <Copy className="h-3.5 w-3.5" /> {copied ? t("Copié") : t("Copier")}
    </Button>
  );
}

function FailurePanel({ job, diagnostics }: { job: JobDetail; diagnostics: JobDiagnostics }) {
  const t = useT();
  const meta = failureMeta(job.failure_category);
  const decision = diagnostics.decision;
  return (
    <div className="space-y-3 rounded-md border border-destructive/40 bg-destructive/5 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-destructive">
            <XCircle className="h-4 w-4" />
            {t(meta.label)}
          </div>
          <p className="mt-1 text-sm text-foreground">{t(meta.explanation)}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            <span className="font-medium">{t("Remède :")}</span> {t(meta.remedy)}
          </p>
        </div>
        {job.failure_category && (
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {job.failure_category}
          </code>
        )}
      </div>
      {decision && (
        <div
          className={cn(
            "flex flex-wrap items-center gap-2 rounded border px-2.5 py-2 text-xs",
            decision.kind === "final"
              ? "border-destructive/30 text-destructive"
              : "border-primary/30 text-primary",
          )}
        >
          {decision.kind === "final" ? <Ban className="h-3.5 w-3.5" /> : <Zap className="h-3.5 w-3.5" />}
          <span className="font-medium">{t(DECISION_LABELS[decision.kind] ?? decision.kind)}</span>
          {decision.next_strategy && (
            <>
              <ArrowRight className="h-3 w-3" />
              <Badge variant="info">{t(strategyMeta(decision.next_strategy).label)}</Badge>
            </>
          )}
          <span className="text-muted-foreground">— {decision.reason}</span>
        </div>
      )}
      {job.error_message && (
        <div className="font-mono text-xs text-foreground/90">{job.error_message}</div>
      )}
      {job.failure_detail && (
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">
              {t("Lignes d'erreur significatives")}
            </span>
            <CopyButton text={job.failure_detail} />
          </div>
          <pre className="log-viewer max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-black/40 p-2 font-mono text-[11px] leading-relaxed text-foreground/90">
            {job.failure_detail}
          </pre>
        </div>
      )}
    </div>
  );
}

function OutcomeNotice({ job, diagnostics }: { job: JobDetail; diagnostics: JobDiagnostics }) {
  const t = useT();
  if (job.state === "skipped_no_gain") {
    const reason = diagnostics.preflight?.gain?.hopeless
      ? diagnostics.preflight.gain.reason
      : job.error_message;
    return (
      <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
        <MinusCircle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
        <div>
          <div className="font-medium">
            {job.strategy === "preflight"
              ? t("Écarté avant encodage : gain prévu insuffisant")
              : t("Gain insuffisant : original conservé")}
          </div>
          {reason && <div className="mt-0.5 text-xs text-muted-foreground">{reason}</div>}
        </div>
      </div>
    );
  }
  const attempt = job.attempt ?? 1;
  if (job.state === "success" && attempt > 1) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-success/40 bg-success/5 p-3 text-sm">
        <CheckCircle2 className="h-4 w-4 text-success" />
        {t("Réussi après")} {attempt - 1} {t("tentative(s) en échec")}
        {job.strategy && job.strategy !== "standard" && (
          <>
            {" "}
            {t("grâce à la stratégie")} <Badge variant="info">{t(strategyMeta(job.strategy).label)}</Badge>
          </>
        )}
      </div>
    );
  }
  if (job.state === "cancelled" && job.error_message) {
    return (
      <div className="rounded-md border p-3 text-sm text-muted-foreground">{job.error_message}</div>
    );
  }
  return null;
}

function SummaryTab({ job }: { job: JobDetail }) {
  const t = useT();
  const rows: [string, (side: "src" | "dst") => string][] = [
    ["Codec", (s) => (s === "src" ? job.src_codec : job.dst_codec) ?? "—"],
    ["Résolution", (s) => (s === "src" ? job.src_resolution : job.dst_resolution) ?? "—"],
    ["HDR", (s) => hdrLabel(s === "src" ? job.src_hdr_format : job.dst_hdr_format)],
    ["Bitrate", (s) => formatBitrate(s === "src" ? job.src_bitrate_kbps : job.dst_bitrate_kbps)],
    ["Taille", (s) => formatBytes(s === "src" ? job.src_size_bytes : job.dst_size_bytes)],
  ];
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {(["src", "dst"] as const).map((side) => (
          <Section key={side} title={side === "src" ? t("avant") : t("après")}>
            <dl className="space-y-1 font-mono text-xs">
              {rows.map(([label, value]) => (
                <div key={label} className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">{t(label)}</dt>
                  <dd className="truncate">{value(side)}</dd>
                </div>
              ))}
            </dl>
          </Section>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ["Gain", job.bytes_saved != null ? `${formatBytes(job.bytes_saved)} (${formatPercent(job.gain_percent)})` : "—"],
          ["Durée du job", formatDuration(job.duration_s)],
          ["FPS moyen", job.avg_fps != null ? job.avg_fps.toFixed(0) : "—"],
          ["Vitesse", job.avg_speed != null ? `${job.avg_speed.toFixed(2)}x` : "—"],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md border bg-card p-2.5">
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{t(label!)}</div>
            <div className="mt-0.5 font-mono text-sm">{value}</div>
          </div>
        ))}
      </div>
      {job.trashed_path && (
        <div className="text-xs text-muted-foreground">
          {t("Original en corbeille :")} <span className="font-mono">{job.trashed_path}</span>
        </div>
      )}
    </div>
  );
}

function PreflightTab({ diagnostics }: { diagnostics: JobDiagnostics }) {
  const t = useT();
  const checks: PreflightCheck[] = diagnostics.preflight?.checks ?? [];
  const streams = diagnostics.stream_plan?.streams ?? [];
  if (checks.length === 0 && streams.length === 0) {
    return (
      <div className="py-6 text-center text-sm text-muted-foreground">
        {t("Tentative antérieure au pré-vol : pas de diagnostic détaillé enregistré.")}
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {checks.length > 0 && (
        <Section title={t("Contrôles avant encodage")}>
          <ul className="space-y-1.5">
            {checks.map((c, i) => (
              <li key={`${c.name}-${i}`} className="flex items-start gap-2 text-sm">
                <StatusIcon status={c.status} />
                <span className="w-40 shrink-0 text-muted-foreground">{t(PREFLIGHT_LABELS[c.name] ?? c.name)}</span>
                <span className="min-w-0 break-words text-xs leading-5">{c.detail}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {streams.length > 0 && (
        <Section title={t("Plan de pistes")}>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-1 pr-2">#</th>
                  <th className="py-1 pr-2">{t("Type")}</th>
                  <th className="py-1 pr-2">{t("Codec")}</th>
                  <th className="py-1 pr-2">{t("Langue")}</th>
                  <th className="py-1 pr-2">{t("Action")}</th>
                  <th className="py-1">{t("Motif")}</th>
                </tr>
              </thead>
              <tbody>
                {streams.map((s) => (
                  <tr key={s.index} className="border-b border-border/50 align-top">
                    <td className="py-1 pr-2 font-mono">{s.index}</td>
                    <td className="py-1 pr-2">{s.kind}</td>
                    <td className="py-1 pr-2 font-mono">
                      {s.codec ?? "?"}
                      {s.target_codec && <span className="text-muted-foreground"> → {s.target_codec}</span>}
                    </td>
                    <td className="py-1 pr-2">{s.language ?? "—"}</td>
                    <td className="py-1 pr-2">
                      <Badge variant={ACTION_VARIANT[s.action]}>{t(ACTION_LABELS[s.action] ?? s.action)}</Badge>
                    </td>
                    <td className="py-1 text-muted-foreground">{s.reason ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      {diagnostics.strategy && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {t("Stratégie utilisée :")}
          <Badge variant="info" title={t(strategyMeta(diagnostics.strategy.used).description)}>
            {t(strategyMeta(diagnostics.strategy.used).label)}
          </Badge>
          {t("API GPU :")} <span className="font-mono">{diagnostics.strategy.gpu_api}</span>
          {diagnostics.treatment && (
            <>
              · <span>{diagnostics.treatment.reason}</span>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ValidationTab({ job }: { job: JobDetail }) {
  const t = useT();
  const report = job.validation_report as { checks?: ValidationCheck[]; outcome?: string } | null;
  const checks = report?.checks ?? [];
  if (checks.length === 0) {
    return (
      <div className="py-6 text-center text-sm text-muted-foreground">
        {t("Aucune validation : le job s'est arrêté avant.")}
      </div>
    );
  }
  return (
    <ul className="space-y-1.5">
      {checks.map((c) => (
        <li
          key={c.name}
          className={cn(
            "flex items-start gap-2 rounded-md border p-2 text-sm",
            !c.ok && "border-destructive/40 bg-destructive/5",
          )}
        >
          <StatusIcon status={c.ok ? "ok" : "error"} />
          <span className="w-48 shrink-0">{t(CHECK_LABELS[c.name] ?? c.name)}</span>
          <span className="min-w-0 break-words font-mono text-xs leading-5 text-muted-foreground">
            {c.detail}
          </span>
        </li>
      ))}
    </ul>
  );
}

function AttemptsTab({
  mediaFileId,
  currentJobId,
  onSelect,
}: {
  mediaFileId: number;
  currentJobId: number;
  onSelect: (jobId: number) => void;
}) {
  const t = useT();
  const { data: file } = useFileDetail(mediaFileId);
  if (!file) return <div className="py-4 text-sm text-muted-foreground">{t("Chargement…")}</div>;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {t("État actuel du fichier :")} <StatusBadge status={file.status} />
        {file.failure_category && <FailureBadge category={file.failure_category} />}
        {(file.strategies_tried ?? []).length > 0 && (
          <span>
            {t("Stratégies déjà tentées :")}{" "}
            {(file.strategies_tried ?? []).map((s) => t(strategyMeta(s).label)).join(", ")}
          </span>
        )}
        {file.next_strategy && (
          <span>
            {t("Prochaine :")} <Badge variant="info">{t(strategyMeta(file.next_strategy).label)}</Badge>
          </span>
        )}
      </div>
      <ol className="max-h-80 space-y-1 overflow-auto">
        {(file.jobs ?? []).map((j) => (
          <li key={j.id}>
            <button
              onClick={() => onSelect(j.id)}
              className={cn(
                "flex w-full flex-wrap items-center gap-2 rounded-md border px-2.5 py-1.5 text-left text-xs hover:bg-accent/50",
                j.id === currentJobId && "border-primary/60 bg-primary/5",
              )}
            >
              <span className="w-14 font-mono text-muted-foreground">#{j.id}</span>
              <span className="w-28 font-mono">{formatDate(j.finished_at ?? j.started_at)}</span>
              <StatusBadge status={j.state} />
              <StrategyBadge strategy={j.strategy} hideStandard={false} />
              <AttemptBadge attempt={j.attempt} />
              {j.failure_category && <FailureBadge category={j.failure_category} />}
              {j.state === "success" && j.gain_percent != null && (
                <span className="font-mono text-success">−{formatPercent(j.gain_percent)}</span>
              )}
              <span className="ml-auto font-mono text-muted-foreground">{formatDuration(j.duration_s)}</span>
            </button>
          </li>
        ))}
      </ol>
      {(file.job_count ?? 0) >= 200 && (
        <div className="text-xs text-muted-foreground">{t("Seules les 200 dernières tentatives sont affichées.")}</div>
      )}
    </div>
  );
}

export function JobDetailDialog({
  jobId,
  fileId,
  onClose,
}: {
  jobId: number | null;
  fileId?: number | null;
  onClose: () => void;
}) {
  const t = useT();
  const [currentId, setCurrentId] = useState<number | null>(jobId);
  const { data: fileForLatest } = useFileDetail(jobId === null && fileId ? fileId : null);
  useEffect(() => {
    setCurrentId(jobId);
  }, [jobId]);
  useEffect(() => {
    const latest = fileForLatest?.jobs?.[0];
    if (jobId === null && latest) setCurrentId(latest.id);
  }, [jobId, fileForLatest]);

  const open = jobId !== null || Boolean(fileId);
  const { data: job } = useJobDetail(currentId);
  const { data: currentFile } = useFileDetail(job?.media_file_id ?? null);
  const { data: libraries } = useLibraries();
  const jobAction = useJobAction();
  const fileAction = useFileAction();
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const diagnostics = useMemo(() => (job?.diagnostics ?? {}) as JobDiagnostics, [job]);
  const libraryName = libraries?.find((l) => l.id === job?.library_id)?.name;
  const isActive = job ? ["queued", "running", "validating", "replacing"].includes(job.state) : false;
  const cmd = (job?.ffmpeg_cmd ?? []).join(" ");

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setActionMessage(null);
          onClose();
        }
      }}
    >
      <DialogContent className="max-h-[92vh] max-w-5xl">
        {!job ? (
          <div className="py-10 text-center text-sm text-muted-foreground">
            {fileId && fileForLatest && (fileForLatest.jobs ?? []).length === 0
              ? t("Aucune tentative enregistrée pour ce fichier.")
              : t("Chargement…")}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="pr-8">
              <DialogTitle className="mb-1 break-all font-mono text-sm">
                {basename(job.src_path ?? "")}
              </DialogTitle>
              <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                <StatusBadge status={job.state} />
                <StrategyBadge strategy={job.strategy} hideStandard={false} />
                <AttemptBadge attempt={job.attempt} />
                {job.dry_run && <Badge variant="warning">DRY RUN</Badge>}
                <span>
                  #{job.id}
                  {libraryName ? ` · ${libraryName}` : ""} · {formatDate(job.finished_at ?? job.started_at)}
                </span>
              </div>
              <div className="mt-1 flex items-center gap-1">
                <span className="truncate font-mono text-[11px] text-muted-foreground" title={job.src_path ?? ""}>
                  {job.src_path}
                </span>
                <CopyButton text={job.src_path ?? ""} />
              </div>
            </div>

            {job.state === "failed" && <FailurePanel job={job} diagnostics={diagnostics} />}
            <OutcomeNotice job={job} diagnostics={diagnostics} />

            <Tabs defaultValue={job.state === "failed" ? "attempts" : "summary"} key={job.id}>
              <TabsList className="flex h-auto flex-wrap justify-start">
                <TabsTrigger value="summary">{t("Résumé")}</TabsTrigger>
                <TabsTrigger value="preflight">{t("Pré-vol & pistes")}</TabsTrigger>
                <TabsTrigger value="validation">{t("Validation")}</TabsTrigger>
                <TabsTrigger value="attempts">{t("Tentatives")}</TabsTrigger>
                <TabsTrigger value="log">{t("Commande & log")}</TabsTrigger>
              </TabsList>
              <div className="mt-3">
                <TabsContent value="summary">
                  <SummaryTab job={job} />
                </TabsContent>
                <TabsContent value="preflight">
                  <PreflightTab diagnostics={diagnostics} />
                </TabsContent>
                <TabsContent value="validation">
                  <ValidationTab job={job} />
                </TabsContent>
                <TabsContent value="attempts">
                  <AttemptsTab
                    mediaFileId={job.media_file_id}
                    currentJobId={job.id}
                    onSelect={setCurrentId}
                  />
                </TabsContent>
                <TabsContent value="log" className="space-y-3">
                  <Section title={t("Commande FFmpeg")}>
                    <div className="flex justify-end">{cmd && <CopyButton text={cmd} />}</div>
                    <code className="block max-h-32 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-muted-foreground">
                      {cmd || "—"}
                    </code>
                  </Section>
                  <JobLogViewer jobId={job.id} live={isActive} />
                </TabsContent>
              </div>
            </Tabs>

            <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-3">
              {actionMessage && <span className="mr-auto text-xs text-muted-foreground">{actionMessage}</span>}
              {["failed", "cancelled", "skipped_no_gain"].includes(job.state) &&
                currentFile &&
                !["done", "queued", "processing"].includes(currentFile.status) && (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={jobAction.isPending}
                  onClick={() =>
                    jobAction.mutate(
                      { id: job.id, action: "retry" },
                      { onSuccess: (d) => setActionMessage(d.detail ?? t("fait")) },
                    )
                  }
                >
                  <RotateCcw className="h-3.5 w-3.5" /> {t("Relancer avec toutes les méthodes")}
                </Button>
              )}
              {currentFile?.status === "queued" && (
                <span className="mr-auto text-xs text-muted-foreground">
                  {t("Fichier en file d'attente : nouvelle tentative prévue")}
                </span>
              )}
              {job.state === "skipped_no_gain" && currentFile?.status === "skipped_no_gain" && (
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    fileAction.mutate(
                      { id: job.media_file_id, action: "force" },
                      { onSuccess: (d) => setActionMessage(d.detail ?? t("fait")) },
                    )
                  }
                >
                  <Zap className="h-3.5 w-3.5" /> {t("Forcer le réencodage")}
                </Button>
              )}
              {!isActive && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    fileAction.mutate(
                      { id: job.media_file_id, action: "ignore" },
                      { onSuccess: (d) => setActionMessage(d.detail ?? t("fait")) },
                    )
                  }
                >
                  <Ban className="h-3.5 w-3.5" /> {t("Ignorer ce fichier")}
                </Button>
              )}
              {isActive && (
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={() => jobAction.mutate({ id: job.id, action: "cancel" })}
                >
                  <XCircle className="h-3.5 w-3.5" /> {t("Annuler")}
                </Button>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
