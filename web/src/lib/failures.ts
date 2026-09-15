/** Catalogue des causes d'échec et des stratégies (miroir de
 *  app/transcoder/core/failures.py et strategy.py). Libellés en français =
 *  clés i18n ; l'anglais vit dans lib/i18n.tsx. */

export type FailureGroup = "container" | "source" | "hardware" | "validation" | "environment";

export interface FailureMeta {
  label: string;
  /** Ce qui s'est passé, en clair */
  explanation: string;
  /** Ce que Transcodarr fait automatiquement */
  remedy: string;
  group: FailureGroup;
}

export const FAILURE_CATEGORIES: Record<string, FailureMeta> = {
  mux_subtitle: {
    label: "Sous-titres incompatibles",
    explanation:
      "Une piste de sous-titres (souvent mov_text des MP4) ne peut pas être écrite dans le conteneur de sortie.",
    remedy:
      "Conversion en SubRip quand elle est obligatoire, puis reconstruction sans perte des SubRip. Les ASS ne sont jamais réencodés ni supprimés.",
    group: "container",
  },
  mux_audio: {
    label: "Audio incompatible",
    explanation: "Le codec audio (PCM Blu-ray/DVD…) n'a pas d'équivalent dans le conteneur de sortie.",
    remedy: "Conversion automatique en FLAC, sans perte.",
    group: "container",
  },
  mux_data: {
    label: "Piste de données non supportée",
    explanation:
      "Timecode, métadonnées ou pièce jointe sans type MIME : Matroska refuse ces pistes.",
    remedy: "Pistes exclues automatiquement du fichier produit.",
    group: "container",
  },
  mux_other: {
    label: "Écriture du fichier impossible",
    explanation: "Le muxer n'a pas pu écrire l'en-tête du fichier de sortie.",
    remedy: "Nouvelle tentative avec la liste blanche stricte de pistes, puis l'autre API GPU.",
    group: "container",
  },
  source_corrupt: {
    label: "Source partiellement corrompue",
    explanation:
      "Des trames de la source ne se décodent pas (fichier abîmé, téléchargement incomplet).",
    remedy: "Décodage tolérant : trames corrompues écartées, flux propre réencodé puis validé.",
    group: "source",
  },
  timestamps: {
    label: "Horodatages invalides",
    explanation:
      "Horodatages absents ou non croissants dans la source (AVI DivX, TS, remux approximatifs).",
    remedy: "Horodatages régénérés (mode tolérant).",
    group: "source",
  },
  source_unreadable: {
    label: "Source illisible",
    explanation: "Le fichier est inaccessible, vide ou n'a pas pu être analysé par ffprobe.",
    remedy: "Réévalué automatiquement au prochain scan si le fichier change.",
    group: "source",
  },
  source_busy: {
    label: "Source en cours d'écriture",
    explanation:
      "Le fichier vient d'être modifié ou a été remplacé pendant la lecture : copie Radarr/Sonarr en cours, ou NAS.",
    remedy: "Nouvel essai automatique après un délai.",
    group: "environment",
  },
  hw_encoder: {
    label: "Erreur de l'encodeur GPU",
    explanation: "L'encodeur matériel (QSV/VAAPI) a refusé ou interrompu l'encodage.",
    remedy: "Nouvelle tentative avec l'autre API du GPU (QSV ↔ VAAPI).",
    group: "hardware",
  },
  hw_device: {
    label: "GPU indisponible",
    explanation: "Impossible d'initialiser le périphérique GPU.",
    remedy: "Nouvel essai automatique après un délai.",
    group: "hardware",
  },
  encoder_crash: {
    label: "FFmpeg a planté",
    explanation: "Le processus d'encodage s'est arrêté brutalement (signal).",
    remedy: "Nouvelle tentative en mode tolérant, puis avec l'autre API GPU.",
    group: "hardware",
  },
  interrupted: {
    label: "Encodage interrompu",
    explanation: "Arrêt du conteneur ou du processus pendant l'encodage.",
    remedy: "Nouvel essai automatique après un délai.",
    group: "environment",
  },
  output_missing: {
    label: "Fichier produit absent",
    explanation: "L'encodage s'est terminé sans produire de fichier exploitable.",
    remedy: "Nouvelle tentative en mode tolérant.",
    group: "validation",
  },
  codec_mismatch: {
    label: "Mauvais codec en sortie",
    explanation: "Le fichier produit n'est pas dans le codec cible.",
    remedy: "Nouvelle tentative avec l'autre API GPU.",
    group: "validation",
  },
  duration_mismatch: {
    label: "Durée incohérente",
    explanation:
      "Les pistes du fichier produit ne durent pas autant que celles de la source (troncature).",
    remedy:
      "La durée réelle des pistes est désormais mesurée : une durée conteneur gonflée par un sous-titre n'échoue plus.",
    group: "validation",
  },
  output_corrupt: {
    label: "Vidéo produite corrompue",
    explanation: "La vidéo réencodée ne se décode pas proprement.",
    remedy: "Nouvelle tentative en mode tolérant, puis avec l'autre API GPU.",
    group: "validation",
  },
  validator_crash: {
    label: "Vérification interrompue",
    explanation:
      "Le décodeur de vérification s'est arrêté sans message (ancien contrôle : décodait aussi l'audio copié).",
    remedy: "Seul le flux vidéo est vérifié, avec seconde passe mono-thread en cas d'arrêt.",
    group: "validation",
  },
  stream_mismatch: {
    label: "Pistes manquantes",
    explanation: "Le fichier produit n'a pas toutes les pistes audio/sous-titres prévues.",
    remedy: "Nouvelle tentative avec la liste blanche stricte de pistes.",
    group: "validation",
  },
  subtitle_timing: {
    label: "Minutage des sous-titres modifié",
    explanation:
      "Pendant la copie, FFmpeg a déplacé des événements de sous-titres (horodatages non croissants) : le fichier produit aurait des sous-titres désynchronisés.",
    remedy:
      "Original conservé, puis nouvelle tentative : SubRip reconstruits, ou assemblage mkvmerge qui recopie les sous-titres bit à bit (ASS jamais réencodés).",
    group: "validation",
  },
  colorimetry: {
    label: "Colorimétrie HDR invalide",
    explanation: "Flags BT.2020/PQ ou métadonnées HDR absents en sortie.",
    remedy: "Original conservé ; vérifier mkvpropedit et la politique HDR.",
    group: "validation",
  },
  dv_colors: {
    label: "Couleurs Dolby Vision altérées",
    explanation: "La conversion Dolby Vision produit des couleurs incorrectes (contrôle anti-vert/violet).",
    remedy: "Original conservé : aucune conversion risquée n'est appliquée.",
    group: "validation",
  },
  disk_space: {
    label: "Espace disque insuffisant",
    explanation: "Pas assez d'espace libre pour encoder en sécurité.",
    remedy: "Nouvel essai automatique après un délai.",
    group: "environment",
  },
  permission: {
    label: "Permission refusée",
    explanation: "L'application n'a pas les droits d'écriture nécessaires.",
    remedy: "À corriger côté système (PUID/PGID, droits du dossier).",
    group: "environment",
  },
  no_rule: {
    label: "Aucune règle de bitrate",
    explanation: "Pas de règle pour ce codec, cette résolution et ce type HDR.",
    remedy: "Compléter l'échelle de bitrates de la bibliothèque.",
    group: "environment",
  },
  replace_error: {
    label: "Remplacement impossible",
    explanation: "Le fichier validé n'a pas pu remplacer l'original (l'original est intact).",
    remedy: "Vérifier la corbeille et les droits du dossier.",
    group: "environment",
  },
  internal: {
    label: "Erreur interne",
    explanation: "Erreur inattendue de l'application.",
    remedy: "Nouvel essai automatique après un délai.",
    group: "environment",
  },
  unknown: {
    label: "Cause non identifiée",
    explanation: "L'erreur ne correspond à aucune cause connue : voir les lignes d'erreur.",
    remedy: "Replis successifs qui changent réellement la commande : tolérant, pistes strictes, autre API GPU.",
    group: "environment",
  },
};

export function failureMeta(category: string | null | undefined): FailureMeta {
  return FAILURE_CATEGORIES[category ?? "unknown"] ?? FAILURE_CATEGORIES.unknown!;
}

export const FAILURE_GROUP_LABELS: Record<FailureGroup, string> = {
  container: "Conteneur",
  source: "Source",
  hardware: "GPU",
  validation: "Validation",
  environment: "Environnement",
};

export interface StrategyMeta {
  label: string;
  description: string;
}

export const STRATEGIES: Record<string, StrategyMeta> = {
  standard: {
    label: "Standard",
    description: "Commande nominale, pistes adaptées automatiquement au conteneur.",
  },
  tolerant: {
    label: "Tolérant",
    description: "Horodatages régénérés, trames corrompues écartées, erreurs de décodage ignorées.",
  },
  reencode_subs: {
    label: "Sous-titres reconstruits",
    description:
      "Mode tolérant + sous-titres SubRip réencodés sans perte (paquets et horodatages refaits). ASS jamais modifiés.",
  },
  mkvmerge_mux: {
    label: "Assemblage mkvmerge",
    description:
      "Vidéo réencodée par FFmpeg ; audio, sous-titres, polices et chapitres recopiés à l'identique par mkvmerge (minutage exact, même sur des sources désordonnées).",
  },
  safe_streams: {
    label: "Pistes strictes",
    description:
      "Sous-titres reconstruits + liste blanche stricte des pistes (audio exotique → FLAC ; seules les pistes impossibles à écrire sont exclues, jamais un ASS).",
  },
  alt_api: {
    label: "Autre API GPU",
    description: "Tout ce qui précède, sur l'autre API matérielle du GPU (QSV ↔ VAAPI).",
  },
  preflight: {
    label: "Pré-vol",
    description: "Évalué avant encodage, sans utiliser le GPU.",
  },
};

export function strategyMeta(strategy: string | null | undefined): StrategyMeta {
  return STRATEGIES[strategy ?? "standard"] ?? { label: strategy ?? "—", description: "" };
}

export const DECISION_LABELS: Record<string, string> = {
  fallback: "Nouvelle tentative automatique avec une autre méthode",
  retry: "Nouvel essai automatique après un délai",
  final: "Échec définitif : relance manuelle possible",
};
