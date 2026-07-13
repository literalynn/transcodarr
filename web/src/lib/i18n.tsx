/** i18n minimaliste : le français est la clé, dictionnaire EN en regard.
 *  `useT()` renvoie t() ; toute chaîne absente du dictionnaire reste en
 *  français (fallback sûr). La langue est persistée en localStorage. */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Lang = "fr" | "en";

const EN: Record<string, string> = {
  // Navigation / layout
  "Dashboard": "Dashboard",
  "Queue": "Queue",
  "Historique": "History",
  "Statistiques": "Statistics",
  "Bibliothèques": "Libraries",
  "GPU & Réglages": "GPU & Settings",
  "en ligne": "online",
  "hors ligne": "offline",
  "worker(s)": "worker(s)",
  "Basculer le thème": "Toggle theme",
  "⚠ DRY RUN — aucun fichier original ne sera remplacé":
    "⚠ DRY RUN — no original file will be replaced",
  // Dashboard
  "Reprendre": "Resume",
  "Pause": "Pause",
  "Lancer un scan": "Start a scan",
  "Jobs en cours": "Active jobs",
  "slots": "slots",
  "Queue en pause.": "Queue paused.",
  "Aucun encodage en cours.": "No encode running.",
  "Fichiers traités": "Processed files",
  "en file d'attente": "queued",
  "Espace récupéré": "Space saved",
  "Gain moyen": "Average gain",
  "Débit": "Throughput",
  "sur 24 h": "last 24 h",
  "Temps restant estimé": "Estimated time left",
  "Voir les logs": "View logs",
  "Logs": "Logs",
  "Annuler": "Cancel",
  "Log du job": "Job log",
  "Ressources": "Resources",
  "app": "app",
  "reste du système": "rest of system",
  "libre": "free",
  "indisponible": "unavailable",
  "libplacebo sans support Dolby Vision : les fichiers DV P5 seront marqués « non traité (politique) », jamais convertis en best-effort.":
    "libplacebo without Dolby Vision support: DV P5 files will be marked \"skipped (policy)\", never converted best-effort.",
  // Queue
  "En attente": "Waiting",
  "En cours": "Running",
  "Terminés": "Finished",
  "Rechercher par chemin…": "Search by path…",
  "Fichier": "File",
  "Codec": "Codec",
  "Résolution": "Resolution",
  "Bitrate": "Bitrate",
  "Gain estimé": "Estimated gain",
  "fichier(s) en attente — glisser pour réordonner":
    "file(s) waiting — drag to reorder",
  "Prioriser (tête de queue)": "Prioritize (head of queue)",
  "Relancer les jobs bloqués": "Recover stuck jobs",
  "Re-queuer les dry-run": "Re-queue dry-runs",
  "Date": "Date",
  "État": "State",
  "Gain": "Gain",
  // Historique
  "Toutes les bibliothèques": "All libraries",
  "Tous les états": "All states",
  "Rechercher un fichier…": "Search a file…",
  "Durée": "Duration",
  "HDR": "HDR",
  "Taille": "Size",
  "job(s) — page": "job(s) — page",
  "Précédent": "Previous",
  "Suivant": "Next",
  "avant": "before",
  "après": "after",
  "Rapport de validation": "Validation report",
  "Commande FFmpeg": "FFmpeg command",
  "Copier": "Copy",
  "Copier le chemin complet": "Copy full path",
  "Réessayer": "Retry",
  "Chargement…": "Loading…",
  "Rechercher dans le log…": "Search in log…",
  "Télécharger": "Download",
  "(log vide)": "(empty log)",
  // Stats
  "Espace récupéré (cumulé, Gio)": "Space saved (cumulative, GiB)",
  "Répartition des codecs (bibliothèque actuelle)":
    "Codec distribution (current library)",
  "Gain moyen par bucket et codec cible (%)":
    "Average gain by bucket and target codec (%)",
  "Débit d'encodage (fichiers/jour)": "Encoding throughput (files/day)",
  "Progression par bibliothèque (fichiers conformes)":
    "Progress by library (conforming files)",
  "Top fichiers les plus lourds non traités": "Largest unprocessed files",
  "Échecs — taux :": "Failures — rate:",
  "Aucun échec. 🎉": "No failure. 🎉",
  // Bibliothèques
  "Ajouter": "Add",
  "Scanner": "Scan",
  "désactivée": "disabled",
  "en pause": "paused",
  "dry-run :": "dry-run:",
  "oui": "yes",
  "non": "no",
  "(surcharge)": "(override)",
  "Codec de sortie": "Output codec",
  "Conteneur": "Container",
  "MKV (recommandé)": "MKV (recommended)",
  "Politique HDR": "HDR policy",
  "Préserver le HDR": "Preserve HDR",
  "Tonemap vers SDR": "Tonemap to SDR",
  "Ignorer les fichiers HDR": "Skip HDR files",
  "Politique Dolby Vision": "Dolby Vision policy",
  "Convertir en HDR10": "Convert to HDR10",
  "Ignorer les fichiers DV": "Skip DV files",
  "Ne traiter que les fichiers ajoutés après le…":
    "Only process files added after…",
  "Gain minimal (%)": "Minimum gain (%)",
  "Priorité": "Priority",
  "Cron de scan": "Scan cron",
  "Exclusions (globs, séparés par des virgules)":
    "Exclusions (globs, comma-separated)",
  "Activée": "Enabled",
  "Dry-run": "Dry-run",
  "hériter du global": "inherit global",
  "Échelle HEVC": "HEVC scale",
  "Échelle AV1": "AV1 scale",
  "Échelle active (codec de sortie de la bibliothèque)":
    "Active scale (library output codec)",
  "Bucket": "Bucket",
  "SDR cible": "SDR target",
  "SDR max": "SDR max",
  "SDR bufsize": "SDR bufsize",
  "HDR cible": "HDR target",
  "HDR max": "HDR max",
  "HDR bufsize": "HDR bufsize",
  "Enregistrer l'échelle": "Save scale",
  "Changer le codec de sortie ?": "Change output codec?",
  "Confirmer le changement": "Confirm change",
  "Supprimer la bibliothèque ?": "Delete library?",
  "Supprimer": "Delete",
  "Nouvelle bibliothèque": "New library",
  "Nom": "Name",
  "Chemin (validé côté serveur)": "Path (validated server-side)",
  "Créer": "Create",
  "création impossible": "creation failed",
  // GPU & Réglages
  "GPU": "GPU",
  "Détecter les nodes": "Detect nodes",
  "Workers": "Workers",
  "Activé": "Enabled",
  "Tester le GPU": "Test GPU",
  "Test en cours…": "Testing…",
  "fallback VAAPI actif": "VAAPI fallback active",
  "DV P5 zéro-copie OK": "DV P5 zero-copy OK",
  "Nodes détectés :": "Detected nodes:",
  "Réglages globaux (appliqués à chaud, sans redémarrage)":
    "Global settings (hot-applied, no restart)",
  "Sauvegarde de la configuration": "Configuration backup",
  "Pour ne jamais reperdre la config (§9.5.10)":
    "Never lose your config again (§9.5.10)",
  "Exporter (JSON)": "Export (JSON)",
  "Importer": "Import",
  "Dry-run global (aucun remplacement)": "Global dry-run (no replacement)",
  "Concurrence max": "Max concurrency",
  "Espace disque minimal (Go)": "Minimum free disk (GB)",
  "Stratégie tmp": "Tmp strategy",
  "Dossier temporaire": "Temp directory",
  "Corbeille": "Trash",
  "Rétention corbeille (jours)": "Trash retention (days)",
  "Suppression immédiate (sans corbeille) ⚠": "Immediate delete (no trash) ⚠",
  "Sortie dry-run": "Dry-run output",
  "Dossier des logs": "Log directory",
  "Rétention logs (jours)": "Log retention (days)",
  "Décodage de validation": "Validation decode",
  "Seuil SSIM DV (contrôle 9)": "DV SSIM threshold (check 9)",
  "Écart chroma max DV": "DV max chroma delta",
  "Retries max": "Max retries",
  "Abandon anticipé si gain projeté insuffisant":
    "Early abort when projected gain insufficient",
  "Chemin ffmpeg": "ffmpeg path",
  "Chemin ffprobe": "ffprobe path",
  "Chemin dovi_tool": "dovi_tool path",
  "Chemin mkvpropedit": "mkvpropedit path",
  "URL Jellyfin (optionnel)": "Jellyfin URL (optional)",
  "API key Jellyfin (optionnel)": "Jellyfin API key (optional)",
  // Badges d'état
  "en attente de scan": "pending scan",
  "en file": "queued",
  "traité": "done",
  "échec": "failed",
  "gain insuffisant": "no gain",
  "non traité (politique)": "skipped (policy)",
  "ignoré": "ignored",
  "encodage": "encoding",
  "validation": "validating",
  "remplacement": "replacing",
  "succès": "success",
  "annulé": "cancelled",
};

// Phases envoyées par le backend (français) : préfixes traduits à la volée
const PHASE_EN: [string, string][] = [
  ["abandon anticipé : gain projeté insuffisant", "early abort: projected gain insufficient"],
  ["calcul des statistiques de pistes", "computing track statistics"],
  ["injection des métadonnées HDR10", "injecting HDR10 metadata"],
  ["remplacement atomique", "atomic replace"],
  ["finalisation", "finalizing"],
  ["validation", "validation"],
  ["encodage", "encoding"],
];

interface I18nContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (fr: string) => string;
  tPhase: (phase: string | null | undefined) => string | null;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() =>
    localStorage.getItem("lang") === "en" ? "en" : "fr",
  );
  const setLang = useCallback((next: Lang) => {
    localStorage.setItem("lang", next);
    setLangState(next);
  }, []);
  const t = useCallback(
    (fr: string) => (lang === "en" ? (EN[fr] ?? fr) : fr),
    [lang],
  );
  const tPhase = useCallback(
    (phase: string | null | undefined) => {
      if (!phase) return null;
      if (lang === "fr") return phase;
      for (const [fr, en] of PHASE_EN) {
        if (phase.startsWith(fr)) return phase.replace(fr, en);
      }
      return phase;
    },
    [lang],
  );
  const value = useMemo(
    () => ({ lang, setLang, t, tPhase }),
    [lang, setLang, t, tPhase],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n hors du I18nProvider");
  return ctx;
}

export function useT(): (fr: string) => string {
  return useI18n().t;
}
