/** Bibliothèques (§7.5) : config complète + tableaux de bitrates HEVC/AV1,
 *  ou cible audio (codec, conteneur, débit) pour une bibliothèque musique. */

import { Plus, Save, ScanSearch, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  api,
  type AudioCodec,
  type AudioContainer,
  type BitrateRuleIn,
  type Codec,
  type Library,
  type MediaType,
} from "@/api/client";
import {
  useBitrateRules,
  useCreateLibrary,
  useDeleteLibrary,
  useLibraries,
  usePatchLibrary,
  usePutBitrateRules,
  useTriggerScan,
} from "@/api/hooks";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Input, Label, Select } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const BUCKETS = ["2160p", "1440p", "1080p", "720p", "576p", "480p"] as const;

// Miroir de core/audio.py (CONTAINERS, MAX_KBPS) : le serveur revalide
const AUDIO_CONTAINERS: Record<AudioCodec, AudioContainer[]> = {
  opus: ["opus", "ogg"],
  aac: ["m4a"],
  mp3: ["mp3"],
};
const AUDIO_MAX_KBPS: Record<AudioCodec, number> = { opus: 510, aac: 512, mp3: 320 };

type AudioTarget = Pick<Library, "audio_codec" | "audio_container" | "audio_bitrate_kbps">;

const DEFAULT_AUDIO_TARGET: AudioTarget = {
  audio_codec: "opus",
  audio_container: "opus",
  audio_bitrate_kbps: 128,
};

// ---------------------------------------------------------------------------
// Cible audio d'une bibliothèque musique (création et édition)
// ---------------------------------------------------------------------------
function AudioTargetFields({
  value,
  onChange,
}: {
  value: AudioTarget;
  onChange: (change: Partial<AudioTarget>) => void;
}) {
  const t = useT();
  const maxKbps = AUDIO_MAX_KBPS[value.audio_codec];
  return (
    <>
      <div>
        <Label>{t("Codec audio cible")}</Label>
        <Select
          value={value.audio_codec}
          onChange={(e) => {
            const codec = e.target.value as AudioCodec;
            // Conteneur et débit suivent le codec : la combinaison reste valide
            onChange({
              audio_codec: codec,
              audio_container: AUDIO_CONTAINERS[codec][0],
              audio_bitrate_kbps: Math.min(value.audio_bitrate_kbps, AUDIO_MAX_KBPS[codec]),
            });
          }}
        >
          <option value="opus">Opus</option>
          <option value="aac">AAC</option>
          <option value="mp3">MP3</option>
        </Select>
      </div>
      <div>
        <Label>{t("Conteneur")}</Label>
        <Select
          value={value.audio_container}
          onChange={(e) => onChange({ audio_container: e.target.value as AudioContainer })}
        >
          {AUDIO_CONTAINERS[value.audio_codec].map((c) => (
            <option key={c} value={c}>
              .{c}
            </option>
          ))}
        </Select>
      </div>
      <div>
        <Label>
          {t("Débit cible (kbps)")} · 32–{maxKbps}
        </Label>
        <Input
          key={`${value.audio_codec}-${value.audio_bitrate_kbps}`}
          type="number"
          min={32}
          max={maxKbps}
          defaultValue={value.audio_bitrate_kbps}
          onBlur={(e) => onChange({ audio_bitrate_kbps: Number(e.target.value) })}
        />
      </div>
      {value.audio_container === "m4a" && (
        <p className="col-span-full text-xs text-warning">
          {t("M4A : les tags non standard (MusicBrainz, etc.) ne sont pas conservés.")}
        </p>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Tableau de bitrates éditable pour un codec (onglets HEVC / AV1, §7.5)
// ---------------------------------------------------------------------------
function BitrateTable({ libraryId, codec, active }: { libraryId: number; codec: Codec; active: boolean }) {
  const t = useT();
  const { data: rules } = useBitrateRules(libraryId);
  const putRules = usePutBitrateRules();
  const [draft, setDraft] = useState<Record<string, { target: number; max: number; bufsize: number }>>({});

  const codecRules = useMemo(
    () => (rules ?? []).filter((r) => r.codec === codec),
    [rules, codec],
  );

  useEffect(() => {
    const next: Record<string, { target: number; max: number; bufsize: number }> = {};
    for (const r of codecRules) {
      next[`${r.resolution_bucket}|${r.is_hdr ? 1 : 0}`] = {
        target: r.target_bitrate_kbps,
        max: r.max_bitrate_kbps,
        bufsize: r.bufsize_kbps,
      };
    }
    setDraft(next);
  }, [codecRules]);

  const setValue = (key: string, field: "target" | "max" | "bufsize", value: number) => {
    setDraft((d) => {
      const row = d[key] ?? { target: 0, max: 0, bufsize: 0 };
      return { ...d, [key]: { ...row, [field]: value } };
    });
  };

  const save = () => {
    // PUT transactionnel du jeu COMPLET (les deux codecs)
    const other = (rules ?? []).filter((r) => r.codec !== codec);
    const payload: BitrateRuleIn[] = [
      ...other.map((r) => ({
        codec: r.codec,
        resolution_bucket: r.resolution_bucket,
        is_hdr: r.is_hdr,
        target_bitrate_kbps: r.target_bitrate_kbps,
        max_bitrate_kbps: r.max_bitrate_kbps,
        bufsize_kbps: r.bufsize_kbps,
      })),
      ...Object.entries(draft).map(([key, v]) => {
        const [bucket, hdr] = key.split("|");
        return {
          codec,
          resolution_bucket: (bucket ?? "1080p") as BitrateRuleIn["resolution_bucket"],
          is_hdr: hdr === "1",
          target_bitrate_kbps: v.target,
          max_bitrate_kbps: v.max,
          bufsize_kbps: v.bufsize,
        };
      }),
    ];
    putRules.mutate({ libraryId, rules: payload });
  };

  return (
    <div className={cn("overflow-x-auto rounded-md border", active && "ring-1 ring-primary/50")}>
      {active && (
        <div className="border-b bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
          {t("Échelle active (codec de sortie de la bibliothèque)")}
        </div>
      )}
      <table className="w-full min-w-[680px] text-sm">
        <thead>
          <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th className="px-3 py-2">{t("Bucket")}</th>
            <th className="px-2 py-2">{t("SDR cible")}</th>
            <th className="px-2 py-2">{t("SDR max")}</th>
            <th className="px-2 py-2">{t("SDR bufsize")}</th>
            <th className="px-2 py-2">{t("HDR cible")}</th>
            <th className="px-2 py-2">{t("HDR max")}</th>
            <th className="px-2 py-2">{t("HDR bufsize")}</th>
          </tr>
        </thead>
        <tbody>
          {BUCKETS.map((bucket) => (
            <tr key={bucket} className="border-b border-border/50">
              <td className="px-3 py-1 font-mono text-xs font-semibold">{bucket}</td>
              {[false, true].flatMap((isHdr) =>
                (["target", "max", "bufsize"] as const).map((field) => {
                  const key = `${bucket}|${isHdr ? 1 : 0}`;
                  const value = draft[key]?.[field] ?? 0;
                  return (
                    <td key={`${key}-${field}`} className="px-2 py-1">
                      <Input
                        type="number"
                        className="h-7 w-24 font-mono text-xs"
                        value={value}
                        onChange={(e) => setValue(key, field, Number(e.target.value))}
                      />
                    </td>
                  );
                }),
              )}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex justify-end border-t p-2">
        <Button size="sm" onClick={save} disabled={putRules.isPending}>
          <Save className="h-3.5 w-3.5" /> {t("Enregistrer l'échelle")} {codec.toUpperCase()} (kbps)
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Éditeur d'une bibliothèque
// ---------------------------------------------------------------------------
function LibraryEditor({ library }: { library: Library }) {
  const t = useT();
  const patch = usePatchLibrary();
  const del = useDeleteLibrary();
  const scan = useTriggerScan();
  const music = library.media_type === "music";
  const [confirmCodec, setConfirmCodec] = useState<{ codec: Codec; count: number } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const requestCodecChange = async (codec: Codec) => {
    if (codec === library.output_codec) return;
    // Avertissement explicite : « N fichiers seront remis en file » (§7.5)
    const { data } = await api.GET("/api/v1/libraries/{library_id}/codec-change-preview", {
      params: { path: { library_id: library.id }, query: { codec } },
    });
    setConfirmCodec({ codec, count: data?.requeued_count ?? 0 });
  };

  return (
    <Card>
      <CardContent className="space-y-4 p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h3 className="font-semibold">{library.name}</h3>
            <Badge variant="info">{music ? t("Musique") : t("Vidéo")}</Badge>
            <span className="font-mono text-xs text-muted-foreground">{library.path}</span>
            {!library.enabled && <Badge variant="warning">{t("désactivée")}</Badge>}
            {library.paused && <Badge variant="warning">{t("en pause")}</Badge>}
            {(library.dry_run ?? null) !== null && (
              <Badge variant={library.dry_run ? "warning" : "success"}>
                {t("dry-run :")} {library.dry_run ? t("oui") : t("non")} {t("(surcharge)")}
              </Badge>
            )}
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" onClick={() => scan.mutate(library.id)}>
              <ScanSearch className="h-3.5 w-3.5" /> {t("Scanner")}
            </Button>
            <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(true)}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {music ? (
            <>
              <AudioTargetFields
                value={library}
                onChange={(change) => patch.mutate({ id: library.id, patch: change })}
              />
              <p className="col-span-full text-xs text-muted-foreground">
                {t("Changer la cible remet en file les fichiers déjà réencodés.")}
              </p>
            </>
          ) : (
            <>
              <div>
                <Label>{t("Codec de sortie")}</Label>
                <Select
                  value={library.output_codec}
                  onChange={(e) => void requestCodecChange(e.target.value as Codec)}
                >
                  <option value="hevc">HEVC (H.265)</option>
                  <option value="av1">AV1</option>
                </Select>
              </div>
              <div>
                <Label>{t("Conteneur")}</Label>
                <Select
                  value={library.container}
                  onChange={(e) =>
                    patch.mutate({
                      id: library.id,
                      patch: { container: e.target.value as Library["container"] },
                    })
                  }
                >
                  <option value="mkv">{t("MKV (recommandé)")}</option>
                  <option value="mp4">MP4</option>
                </Select>
              </div>
              <div>
                <Label>{t("Politique HDR")}</Label>
                <Select
                  value={library.hdr_policy}
                  onChange={(e) =>
                    patch.mutate({
                      id: library.id,
                      patch: { hdr_policy: e.target.value as Library["hdr_policy"] },
                    })
                  }
                >
                  <option value="preserve">{t("Préserver le HDR")}</option>
                  <option value="tonemap_sdr">{t("Tonemap vers SDR")}</option>
                  <option value="skip_hdr_files">{t("Ignorer les fichiers HDR")}</option>
                </Select>
              </div>
              <div>
                <Label>{t("Politique Dolby Vision")}</Label>
                <Select
                  value={library.dv_policy}
                  onChange={(e) =>
                    patch.mutate({
                      id: library.id,
                      patch: { dv_policy: e.target.value as Library["dv_policy"] },
                    })
                  }
                >
                  <option value="convert_to_hdr10">{t("Convertir en HDR10")}</option>
                  <option value="skip_dv_files">{t("Ignorer les fichiers DV")}</option>
                </Select>
              </div>
            </>
          )}
          <div>
            <Label>{t("Ne traiter que les fichiers ajoutés après le…")}</Label>
            <Input
              type="datetime-local"
              defaultValue={library.date_filter_after?.slice(0, 16) ?? ""}
              onBlur={(e) =>
                patch.mutate({
                  id: library.id,
                  patch: e.target.value
                    ? { date_filter_after: new Date(e.target.value).toISOString() }
                    : { clear_date_filter: true },
                })
              }
            />
          </div>
          <div>
            <Label>{t("Gain minimal (%)")}</Label>
            <Input
              type="number"
              defaultValue={library.min_gain_percent}
              onBlur={(e) =>
                patch.mutate({
                  id: library.id,
                  patch: { min_gain_percent: Number(e.target.value) },
                })
              }
            />
          </div>
          <div>
            <Label>{t("Priorité")}</Label>
            <Input
              type="number"
              defaultValue={library.priority}
              onBlur={(e) =>
                patch.mutate({ id: library.id, patch: { priority: Number(e.target.value) } })
              }
            />
          </div>
          <div>
            <Label>{t("Cron de scan")}</Label>
            <Input
              className="font-mono"
              defaultValue={library.scan_cron ?? ""}
              placeholder="0 3 * * *"
              onBlur={(e) =>
                patch.mutate({
                  id: library.id,
                  patch: e.target.value
                    ? { scan_cron: e.target.value }
                    : { clear_scan_cron: true },
                })
              }
            />
          </div>
          <div className="col-span-2">
            <Label>{t("Exclusions (globs, séparés par des virgules)")}</Label>
            <Input
              className="font-mono"
              defaultValue={library.exclude_globs.join(", ")}
              placeholder="*sample*, *extras*"
              onBlur={(e) =>
                patch.mutate({
                  id: library.id,
                  patch: {
                    exclude_globs: e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  },
                })
              }
            />
          </div>
          <div className="flex items-end gap-4 pb-1">
            <div className="flex items-center gap-2">
              <Switch
                checked={library.enabled}
                onCheckedChange={(enabled) => patch.mutate({ id: library.id, patch: { enabled } })}
              />
              <Label>{t("Activée")}</Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch
                checked={library.dry_run ?? false}
                onCheckedChange={(v) => patch.mutate({ id: library.id, patch: { dry_run: v } })}
              />
              <Label>{t("Dry-run")}</Label>
              {(library.dry_run ?? null) !== null && (
                <button
                  className="text-xs text-primary underline"
                  onClick={() => patch.mutate({ id: library.id, patch: { clear_dry_run: true } })}
                >
                  {t("hériter du global")}
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Deux onglets de bitrates, l'actif mis en évidence (§7.5) */}
        {!music && (
          <Tabs defaultValue={library.output_codec}>
            <TabsList>
              <TabsTrigger value="hevc">
                {t("Échelle HEVC")} {library.output_codec === "hevc" && "●"}
              </TabsTrigger>
              <TabsTrigger value="av1">
                {t("Échelle AV1")} {library.output_codec === "av1" && "●"}
              </TabsTrigger>
            </TabsList>
            <TabsContent value="hevc" className="mt-2">
              <BitrateTable
                libraryId={library.id}
                codec="hevc"
                active={library.output_codec === "hevc"}
              />
            </TabsContent>
            <TabsContent value="av1" className="mt-2">
              <BitrateTable
                libraryId={library.id}
                codec="av1"
                active={library.output_codec === "av1"}
              />
            </TabsContent>
          </Tabs>
        )}
      </CardContent>

      {/* Confirmation de changement de codec (§7.5) */}
      <Dialog open={confirmCodec !== null} onOpenChange={(o) => !o && setConfirmCodec(null)}>
        <DialogContent>
          <DialogTitle>{t("Changer le codec de sortie ?")}</DialogTitle>
          <p className="text-sm">
            Passer <b>{library.name}</b> en{" "}
            <b>{confirmCodec?.codec.toUpperCase()}</b> remettra en file d'attente{" "}
            <b className="text-warning">{confirmCodec?.count} fichier(s) déjà traité(s)</b>{" "}
            (le hash de règle inclut le codec).
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setConfirmCodec(null)}>
              {t("Annuler")}
            </Button>
            <Button
              onClick={() => {
                if (confirmCodec) {
                  patch.mutate({ id: library.id, patch: { output_codec: confirmCodec.codec } });
                }
                setConfirmCodec(null);
              }}
            >
              {t("Confirmer le changement")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Confirmation de suppression */}
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent>
          <DialogTitle>{t("Supprimer la bibliothèque ?")}</DialogTitle>
          <p className="text-sm">
            Supprime <b>{library.name}</b>, ses règles, son historique de jobs et l'état de
            ses fichiers. Les fichiers médias sur disque ne sont PAS touchés.
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setConfirmDelete(false)}>
              {t("Annuler")}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                del.mutate(library.id);
                setConfirmDelete(false);
              }}
            >
              {t("Supprimer")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export function LibrariesPage() {
  const t = useT();
  const { data: libraries } = useLibraries();
  const create = useCreateLibrary();
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newPath, setNewPath] = useState("");
  const [newType, setNewType] = useState<MediaType>("video");
  const [newAudio, setNewAudio] = useState<AudioTarget>(DEFAULT_AUDIO_TARGET);
  const [createError, setCreateError] = useState<string | null>(null);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">{t("Bibliothèques")}</h1>
        <Button onClick={() => setShowCreate(true)}>
          <Plus className="h-4 w-4" /> {t("Ajouter")}
        </Button>
      </div>
      {(libraries ?? []).map((lib) => (
        <LibraryEditor key={lib.id} library={lib} />
      ))}

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent>
          <DialogTitle>{t("Nouvelle bibliothèque")}</DialogTitle>
          <div className="space-y-3">
            <div>
              <Label>{t("Nom")}</Label>
              <Input value={newName} onChange={(e) => setNewName(e.target.value)} />
            </div>
            <div>
              <Label>{t("Chemin (validé côté serveur)")}</Label>
              <Input
                className="font-mono"
                placeholder="/srv/storage/…"
                value={newPath}
                onChange={(e) => setNewPath(e.target.value)}
              />
            </div>
            <div>
              <Label>{t("Type")}</Label>
              <Select value={newType} onChange={(e) => setNewType(e.target.value as MediaType)}>
                <option value="video">{t("Vidéo")}</option>
                <option value="music">{t("Musique")}</option>
              </Select>
            </div>
            {newType === "music" && (
              <AudioTargetFields
                value={newAudio}
                onChange={(change) => setNewAudio((a) => ({ ...a, ...change }))}
              />
            )}
            {createError && <p className="text-sm text-destructive">{createError}</p>}
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setShowCreate(false)}>
                {t("Annuler")}
              </Button>
              <Button
                disabled={!newName || !newPath || create.isPending}
                onClick={() =>
                  create.mutate(
                    {
                      name: newName,
                      path: newPath,
                      media_type: newType,
                      ...(newType === "music" ? newAudio : {}),
                    } as Parameters<typeof create.mutate>[0],
                    {
                      onSuccess: () => {
                        setShowCreate(false);
                        setNewName("");
                        setNewPath("");
                        setNewType("video");
                        setNewAudio(DEFAULT_AUDIO_TARGET);
                        setCreateError(null);
                      },
                      onError: (err) =>
                        setCreateError(
                          (err as { detail?: string }).detail ?? t("création impossible"),
                        ),
                    },
                  )
                }
              >
                {t("Créer")}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
