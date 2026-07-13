/** GPU & Réglages (§7.6) : GPU CRUD + test, réglages globaux, export/import. */

import { Download, FlaskConical, Plus, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { api, type ConfigExport, type Gpu } from "@/api/client";
import {
  useCapabilities,
  useGpuMutations,
  useGpus,
  useInvalidate,
  usePatchSettings,
  useSettings,
} from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

// ---------------------------------------------------------------------------
// GPU
// ---------------------------------------------------------------------------
function GpuRow({ gpu }: { gpu: Gpu }) {
  const { patch, remove, test } = useGpuMutations();
  const { data: caps } = useCapabilities();
  const gpuCap = caps?.gpus.find((g) => g.gpu_id === gpu.id);
  const [testResult, setTestResult] = useState<Record<string, { ok: boolean; error: string | null }> | null>(null);

  return (
    <Card>
      <CardContent className="space-y-2 p-3">
      <div className="flex flex-wrap items-center gap-3">
        <Input
          className="w-44"
          defaultValue={gpu.name}
          onBlur={(e) => patch.mutate({ id: gpu.id, patch: { name: e.target.value } })}
        />
        <Input
          className="w-48 font-mono"
          defaultValue={gpu.drm_node}
          onBlur={(e) => patch.mutate({ id: gpu.id, patch: { drm_node: e.target.value } })}
        />
        <Select
          className="w-24"
          value={gpu.api}
          onChange={(e) =>
            patch.mutate({ id: gpu.id, patch: { api: e.target.value as Gpu["api"] } })
          }
        >
          <option value="qsv">QSV</option>
          <option value="vaapi">VAAPI</option>
        </Select>
        <div className="flex items-center gap-1">
          <Label>Workers</Label>
          <Input
            type="number"
            min={1}
            max={8}
            className="w-16"
            defaultValue={gpu.worker_count}
            onBlur={(e) =>
              patch.mutate({ id: gpu.id, patch: { worker_count: Number(e.target.value) } })
            }
          />
        </div>
        <div className="flex items-center gap-1.5">
          <Switch
            checked={gpu.enabled}
            onCheckedChange={(enabled) => patch.mutate({ id: gpu.id, patch: { enabled } })}
          />
          <Label>Activé</Label>
        </div>
        <div className="ml-auto flex gap-2">
          <Button
            size="sm"
            variant="secondary"
            disabled={test.isPending}
            onClick={() =>
              test.mutate(gpu.id, {
                onSuccess: (data) =>
                  setTestResult(
                    data.results as Record<string, { ok: boolean; error: string | null }>,
                  ),
              })
            }
          >
            <FlaskConical className="h-3.5 w-3.5" />
            {test.isPending ? "Test en cours…" : "Tester le GPU"}
          </Button>
          <Button size="sm" variant="destructive" onClick={() => remove.mutate(gpu.id)}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {gpuCap?.fell_back_to_vaapi && <Badge variant="warning">fallback VAAPI actif</Badge>}
        {gpuCap?.p5_zero_copy && <Badge variant="info">DV P5 zéro-copie OK</Badge>}
        {Object.entries(testResult ?? gpuCap?.encoders ?? {}).map(([name, r]) => (
          <Badge key={name} variant={r.ok ? "success" : "destructive"} title={r.error ?? ""}>
            {name} {r.ok ? "✓" : "✗"}
          </Badge>
        ))}
      </div>
      </CardContent>
    </Card>
  );
}

function GpuSection() {
  const { data: gpus } = useGpus();
  const { create } = useGpuMutations();
  const [detected, setDetected] = useState<string[]>([]);

  const detect = async () => {
    const { data } = await api.GET("/api/v1/gpus/detect");
    setDetected(data?.nodes ?? []);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          GPU
        </h2>
        <div className="flex gap-2">
          <Button size="sm" variant="secondary" onClick={() => void detect()}>
            Détecter les nodes
          </Button>
          <Button
            size="sm"
            onClick={() =>
              create.mutate({
                name: "Nouveau GPU",
                drm_node: detected[0] ?? "/dev/dri/renderD128",
                api: "qsv",
                worker_count: 1,
                enabled: true,
              })
            }
          >
            <Plus className="h-3.5 w-3.5" /> Ajouter
          </Button>
        </div>
      </div>
      {detected.length > 0 && (
        <div className="text-xs text-muted-foreground">
          Nodes détectés : <span className="font-mono">{detected.join(", ")}</span>
        </div>
      )}
      {(gpus ?? []).map((gpu) => (
        <GpuRow key={gpu.id} gpu={gpu} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Réglages globaux
// ---------------------------------------------------------------------------
const SETTING_FIELDS: {
  key: string;
  label: string;
  type: "text" | "number" | "bool" | "select";
  options?: string[];
  mono?: boolean;
}[] = [
  { key: "dry_run_global", label: "Dry-run global (aucun remplacement)", type: "bool" },
  { key: "max_concurrent_jobs", label: "Concurrence max", type: "number" },
  { key: "min_free_disk_gb", label: "Espace disque minimal (Go)", type: "number" },
  { key: "tmp_strategy", label: "Stratégie tmp", type: "select", options: ["sibling", "data_tmp"] },
  { key: "tmp_dir", label: "Dossier temporaire", type: "text", mono: true },
  { key: "trash_dir", label: "Corbeille", type: "text", mono: true },
  { key: "trash_retention_days", label: "Rétention corbeille (jours)", type: "number" },
  { key: "immediate_delete", label: "Suppression immédiate (sans corbeille) ⚠", type: "bool" },
  { key: "dry_run_out_dir", label: "Sortie dry-run", type: "text", mono: true },
  { key: "log_dir", label: "Dossier des logs", type: "text", mono: true },
  { key: "log_retention_days", label: "Rétention logs (jours)", type: "number" },
  { key: "validation_decode", label: "Décodage de validation", type: "select", options: ["full", "sample"] },
  { key: "dv_ssim_threshold", label: "Seuil SSIM DV (contrôle 9)", type: "number" },
  { key: "dv_chroma_delta_max", label: "Écart chroma max DV", type: "number" },
  { key: "max_retries", label: "Retries max", type: "number" },
  { key: "ffmpeg_path", label: "Chemin ffmpeg", type: "text", mono: true },
  { key: "ffprobe_path", label: "Chemin ffprobe", type: "text", mono: true },
  { key: "dovi_tool_path", label: "Chemin dovi_tool", type: "text", mono: true },
  { key: "mkvpropedit_path", label: "Chemin mkvpropedit", type: "text", mono: true },
  { key: "jellyfin_url", label: "URL Jellyfin (optionnel)", type: "text", mono: true },
  { key: "jellyfin_api_key", label: "API key Jellyfin (optionnel)", type: "text", mono: true },
];

function SettingsSection() {
  const { data: settings } = useSettings();
  const patchSettings = usePatchSettings();
  const values = settings?.values ?? {};

  const set = (key: string, value: unknown) => patchSettings.mutate({ [key]: value });

  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        Réglages globaux (appliqués à chaud, sans redémarrage)
      </h2>
      <Card>
        <CardContent className="grid grid-cols-1 gap-x-6 gap-y-3 p-4 md:grid-cols-2">
          {SETTING_FIELDS.map((field) => {
            const raw = values[field.key];
            return (
              <div key={field.key} className="flex items-center justify-between gap-3">
                <Label className="flex-1">{field.label}</Label>
                {field.type === "bool" ? (
                  <Switch
                    checked={Boolean(raw)}
                    onCheckedChange={(v) => set(field.key, v)}
                  />
                ) : field.type === "select" ? (
                  <Select
                    className="w-40"
                    value={String(raw ?? "")}
                    onChange={(e) => set(field.key, e.target.value)}
                  >
                    {(field.options ?? []).map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </Select>
                ) : (
                  <Input
                    className={`w-64 ${field.mono ? "font-mono text-xs" : ""}`}
                    type={field.type}
                    step={field.type === "number" ? "any" : undefined}
                    defaultValue={raw === null || raw === undefined ? "" : String(raw)}
                    onBlur={(e) => {
                      const v = e.target.value;
                      if (field.type === "number") set(field.key, v === "" ? 0 : Number(v));
                      else set(field.key, v === "" ? null : v);
                    }}
                  />
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Export / import de configuration (§9.5.10)
// ---------------------------------------------------------------------------
function ConfigSection() {
  const invalidate = useInvalidate();
  const fileRef = useRef<HTMLInputElement>(null);
  const [importStatus, setImportStatus] = useState<string | null>(null);

  const doExport = async () => {
    const { data } = await api.GET("/api/v1/config/export");
    if (!data) return;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `transcoder-config-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const doImport = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text()) as ConfigExport;
      const { data, error } = await api.POST("/api/v1/config/import", { body: parsed });
      if (error) throw error;
      setImportStatus(
        `Import OK : ${data.libraries} bibliothèque(s), ${data.gpus} GPU, ${data.settings} réglage(s)`,
      );
      invalidate("libraries", "gpus", "settings", "bitrate-rules");
    } catch (err) {
      setImportStatus(`Échec de l'import : ${String((err as { detail?: string }).detail ?? err)}`);
    }
  };

  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        Sauvegarde de la configuration
      </h2>
      <Card>
        <CardHeader>
          <CardTitle>Pour ne jamais reperdre la config (§9.5.10)</CardTitle>
        </CardHeader>
        <CardContent className="flex items-center gap-3">
          <Button variant="secondary" onClick={() => void doExport()}>
            <Download className="h-4 w-4" /> Exporter (JSON)
          </Button>
          <Button variant="secondary" onClick={() => fileRef.current?.click()}>
            <Upload className="h-4 w-4" /> Importer
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept="application/json"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void doImport(file);
              e.target.value = "";
            }}
          />
          {importStatus && <span className="text-xs text-muted-foreground">{importStatus}</span>}
        </CardContent>
      </Card>
    </div>
  );
}

export function SettingsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-lg font-bold">GPU & Réglages</h1>
      <GpuSection />
      <SettingsSection />
      <ConfigSection />
    </div>
  );
}
