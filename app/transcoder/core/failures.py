"""Classification des échecs : catégorie exploitable + extrait de log utile.

Avant : un échec se résumait à « code de sortie 218 » — inexploitable.
Ici chaque échec (encodage FFmpeg, validation, préparation) est ramené à
une catégorie stable (stockée en base, traduite dans l'UI, pilote la
stratégie de repli) et à quelques lignes d'erreur FFmpeg débarrassées du
bruit (libva info, avertissements de mapping…).

Tout est pur et testé sans binaire.
"""

from __future__ import annotations

import re
import signal
from dataclasses import dataclass
from enum import Enum


class FailureCategory(str, Enum):
    # --- Conteneur de sortie : pistes non muxables -------------------------
    MUX_SUBTITLE = "mux_subtitle"
    MUX_AUDIO = "mux_audio"
    MUX_DATA = "mux_data"
    MUX_OTHER = "mux_other"
    # --- Source -------------------------------------------------------------
    SOURCE_CORRUPT = "source_corrupt"
    TIMESTAMPS = "timestamps"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_BUSY = "source_busy"
    # --- Encodeur / matériel --------------------------------------------------
    HW_ENCODER = "hw_encoder"
    HW_DEVICE = "hw_device"
    ENCODER_CRASH = "encoder_crash"
    INTERRUPTED = "interrupted"
    # --- Validation de la sortie -----------------------------------------------
    OUTPUT_MISSING = "output_missing"
    CODEC_MISMATCH = "codec_mismatch"
    DURATION_MISMATCH = "duration_mismatch"
    OUTPUT_CORRUPT = "output_corrupt"
    VALIDATOR_CRASH = "validator_crash"
    STREAM_MISMATCH = "stream_mismatch"
    SUBTITLE_TIMING = "subtitle_timing"
    COLORIMETRY = "colorimetry"
    DV_COLORS = "dv_colors"
    # --- Environnement / configuration --------------------------------------------
    DISK_SPACE = "disk_space"
    PERMISSION = "permission"
    NO_RULE = "no_rule"
    REPLACE_ERROR = "replace_error"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Failure:
    category: FailureCategory
    # Résumé court lisible (stocké dans error_message / last_error)
    message: str
    # Lignes d'erreur significatives (stockées dans failure_detail)
    detail: str | None = None


def parse_category(value: str | None) -> FailureCategory | None:
    if not value:
        return None
    try:
        return FailureCategory(value)
    except ValueError:
        return FailureCategory.UNKNOWN


# ---------------------------------------------------------------------------
# Codes de sortie FFmpeg
# ---------------------------------------------------------------------------
# FFmpeg renvoie une AVERROR négative tronquée sur 8 bits : 256 + errno
# négatif. 218 = -38 (ENOSYS), 234 = -22 (EINVAL)… — d'où ces codes opaques.
_AVERROR_EXIT_CODES: dict[int, str] = {
    1: "erreur générique",
    69: "taux d'erreurs de décodage dépassé",
    183: "données invalides (AVERROR_INVALIDDATA)",
    218: "fonction non implémentée (ENOSYS)",
    234: "argument invalide (EINVAL)",
    228: "plus d'espace disque (ENOSPC)",
    243: "permission refusée (EACCES)",
    244: "mémoire insuffisante (ENOMEM)",
    247: "descripteur invalide (EBADF)",
    251: "erreur d'entrée/sortie (EIO)",
    254: "fichier introuvable (ENOENT)",
    255: "interrompu par un signal",
}


def describe_exit_code(code: int | None) -> str:
    """Code de sortie → texte humain (« 234 — argument invalide (EINVAL) »)."""
    if code is None:
        return "code de sortie inconnu"
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"processus tué par {name}"
    label = _AVERROR_EXIT_CODES.get(code)
    return f"code de sortie {code}" + (f" — {label}" if label else "")


_CRASH_SIGNALS = {
    signal.SIGSEGV,
    signal.SIGABRT,
    signal.SIGBUS,
    signal.SIGILL,
    signal.SIGFPE,
}


def is_crash_exit(code: int | None) -> bool:
    """Le processus a planté (segfault, abort…) plutôt qu'échoué proprement."""
    if code is None or code >= 0:
        return False
    try:
        return signal.Signals(-code) in _CRASH_SIGNALS
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Extraction des lignes utiles d'un log FFmpeg
# ---------------------------------------------------------------------------
_NOISE = (
    re.compile(r"^libva info:"),
    re.compile(r"Multiple -codec/-c/-acodec/-vcodec/-scodec/-dcodec options"),
    re.compile(r"^\s*Last message repeated"),
    re.compile(r"^x26[45] \[info\]"),
    re.compile(r"Starting new cluster due to timestamp"),
    re.compile(r"Consider increasing the value for the 'analyzeduration'"),
    re.compile(r"^\s*$"),
)

_ERRORISH = re.compile(
    r"error|invalid|not supported|not implemented|failed|cannot|could not|"
    r"unable|corrupt|damaged|missing|no such|denied|out of range|non monoton|"
    r"unknown|mismatch|abort|fatal|exceed|illegal|truncat",
    re.IGNORECASE,
)


def key_error_lines(log: str | None, *, max_lines: int = 12, max_chars: int = 1800) -> str | None:
    """Lignes significatives d'un log FFmpeg (erreurs d'abord, sans bruit)."""
    if not log:
        return None
    lines = [ln.rstrip() for ln in log.splitlines()]
    meaningful = [ln for ln in lines if not any(p.search(ln) for p in _NOISE)]
    errors = [ln for ln in meaningful if _ERRORISH.search(ln)]
    chosen = errors[-max_lines:] if errors else meaningful[-max_lines:]
    # Dédoublonnage en conservant l'ordre (adresses mémoire ignorées)
    seen: set[str] = set()
    unique: list[str] = []
    for ln in chosen:
        key = re.sub(r"0x[0-9a-f]+", "", ln)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ln)
    text = "\n".join(unique).strip()
    if not text:
        return None
    return text[-max_chars:]


# ---------------------------------------------------------------------------
# Motifs d'erreurs FFmpeg → catégorie (ordre = priorité)
# ---------------------------------------------------------------------------
_PATTERNS: tuple[tuple[re.Pattern[str], FailureCategory, str], ...] = (
    (
        re.compile(
            r"Subtitle codec .* is not supported|Unsupported subtitle codec|"
            r"Subtitle encoding currently only possible from text to text or bitmap to bitmap|"
            r"Error (?:while )?(?:de|en)coding subtitle|Subtitle encoding failed|"
            r"\[(?:ass|ssa|srt|subrip|webvtt|mov_text|microdvd|sami|eia_608|cc_dec)"
            r"[^\]]*\][^\n]*(?:error|invalid)",
            re.I,
        ),
        FailureCategory.MUX_SUBTITLE,
        "sous-titres incompatibles avec le conteneur de sortie",
    ),
    (
        re.compile(r"Only audio, video, and subtitles are supported for Matroska", re.I),
        FailureCategory.MUX_DATA,
        "piste de données non supportée par Matroska",
    ),
    (
        re.compile(r"has no mimetype tag and it cannot be deduced", re.I),
        FailureCategory.MUX_DATA,
        "pièce jointe sans type MIME",
    ),
    (
        re.compile(
            r"No wav codec tag found for codec|Could not find tag for codec .* in stream #\d+, "
            r"codec not currently supported in container|codec not currently supported in container",
            re.I,
        ),
        FailureCategory.MUX_AUDIO,
        "codec audio non supporté par le conteneur de sortie",
    ),
    (
        re.compile(r"No space left on device", re.I),
        FailureCategory.DISK_SPACE,
        "plus d'espace disque",
    ),
    (
        re.compile(r"Permission denied", re.I),
        FailureCategory.PERMISSION,
        "permission refusée",
    ),
    (
        re.compile(
            r"Failed to (?:create|initialise|initialize) (?:a )?(?:VAAPI|QSV|VA) |"
            r"Device creation failed|No VA display found|Failed to set value 'qsv=|"
            r"Failed to set value 'vaapi=|Error creating a MFX session|"
            r"Failed to open the DRM device",
            re.I,
        ),
        FailureCategory.HW_DEVICE,
        "initialisation du GPU impossible",
    ),
    (
        re.compile(
            r"non monotonically increasing dts|Can't write packet with unknown timestamp|"
            r"pts \(\d+\) < dts|Invalid timestamps|Timestamps are unset|"
            r"DTS \d+ < \d+ out of order|invalid dts|Non-monotonous DTS",
            re.I,
        ),
        FailureCategory.TIMESTAMPS,
        "horodatages invalides dans la source",
    ),
    (
        re.compile(
            r"\[(?:hevc|av1|h264|vp9)_(?:qsv|vaapi)[^\]]*\][^\n]*"
            r"(?:error|fail|invalid|unsupported|not supported)|"
            r"MFX_ERR|Error while opening encoder|Failed to (?:upload|map|download) frame|"
            r"Failed to (?:end|begin) picture encode|Failed to query surface|"
            r"Error during encoding",
            re.I,
        ),
        FailureCategory.HW_ENCODER,
        "erreur de l'encodeur matériel",
    ),
    (
        re.compile(
            r"corrupt decoded frame|Error while decoding|Invalid data found when processing input|"
            r"error while decoding MB|illegal MB_type|ac-tex damaged|Header missing|"
            r"Decoding error|decode_slice_header error|concealing \d+ .*errors|"
            r"Error submitting packet to decoder|Decode error rate",
            re.I,
        ),
        FailureCategory.SOURCE_CORRUPT,
        "erreurs de décodage : source partiellement corrompue",
    ),
    (
        re.compile(r"No such file or directory", re.I),
        FailureCategory.SOURCE_UNREADABLE,
        "fichier introuvable",
    ),
    (
        re.compile(r"Could not write header|Error initializing output", re.I),
        FailureCategory.MUX_OTHER,
        "impossible d'écrire l'en-tête du fichier de sortie",
    ),
)


def classify_ffmpeg_failure(exit_code: int | None, log: str | None) -> Failure:
    """Échec d'un encodage FFmpeg (code ≠ 0) → catégorie + extrait utile."""
    detail = key_error_lines(log)
    exit_desc = describe_exit_code(exit_code)
    text = log or ""
    for pattern, category, label in _PATTERNS:
        if pattern.search(text):
            return Failure(category, f"{label} ({exit_desc})", detail)
    if exit_code is not None and exit_code < 0:
        if is_crash_exit(exit_code):
            return Failure(FailureCategory.ENCODER_CRASH, f"FFmpeg a planté ({exit_desc})", detail)
        return Failure(FailureCategory.INTERRUPTED, f"encodage interrompu ({exit_desc})", detail)
    if exit_code == 255:
        return Failure(FailureCategory.INTERRUPTED, f"encodage interrompu ({exit_desc})", detail)
    return Failure(FailureCategory.UNKNOWN, f"échec FFmpeg ({exit_desc})", detail)


# Contrôle de validation → catégorie (le contrôle 1 est reclassé via le log)
_CHECK_CATEGORIES: dict[str, FailureCategory] = {
    "2_output_exists": FailureCategory.OUTPUT_MISSING,
    "3_codec": FailureCategory.CODEC_MISMATCH,
    "4_duration": FailureCategory.DURATION_MISMATCH,
    "5_integrity": FailureCategory.OUTPUT_CORRUPT,
    "6_streams": FailureCategory.STREAM_MISMATCH,
    "8_colorimetry": FailureCategory.COLORIMETRY,
    "8b_no_dovi": FailureCategory.COLORIMETRY,
    "9_dv_colors": FailureCategory.DV_COLORS,
}


def classify_validation_failure(
    check_name: str,
    check_detail: str,
    *,
    exit_code: int | None,
    ffmpeg_log: str | None,
    check_meta: dict[str, object] | None = None,
) -> Failure:
    """Contrôle de validation en échec → catégorie."""
    if check_name == "1_exit_code":
        return classify_ffmpeg_failure(exit_code, ffmpeg_log)
    category = _CHECK_CATEGORIES.get(check_name, FailureCategory.UNKNOWN)
    detail: str | None = None
    if check_name == "5_integrity":
        meta = check_meta or {}
        if meta.get("crashed") or meta.get("timeout"):
            category = FailureCategory.VALIDATOR_CRASH
        detail = key_error_lines(str(meta.get("stderr") or "")) or None
    if check_name == "6b_timestamps":
        subtitles = bool((check_meta or {}).get("subtitles"))
        category = FailureCategory.SUBTITLE_TIMING if subtitles else FailureCategory.TIMESTAMPS
    if check_name == "3_codec" and "ffprobe a échoué" in check_detail:
        category = FailureCategory.OUTPUT_CORRUPT
    return Failure(category, f"{check_name} : {check_detail}", detail)


# ---------------------------------------------------------------------------
# Reclassement de l'historique (jobs antérieurs à cette classification)
# ---------------------------------------------------------------------------
_LEGACY_MESSAGE_PATTERNS: tuple[tuple[re.Pattern[str], FailureCategory], ...] = (
    (re.compile(r"^2_output_exists"), FailureCategory.OUTPUT_MISSING),
    (re.compile(r"^3_codec.*ffprobe a échoué"), FailureCategory.OUTPUT_CORRUPT),
    (re.compile(r"^3_codec"), FailureCategory.CODEC_MISMATCH),
    (re.compile(r"^4_duration"), FailureCategory.DURATION_MISMATCH),
    (re.compile(r"^5_integrity : erreurs : $"), FailureCategory.VALIDATOR_CRASH),
    (re.compile(r"^5_integrity"), FailureCategory.OUTPUT_CORRUPT),
    (re.compile(r"^6_streams"), FailureCategory.STREAM_MISMATCH),
    (re.compile(r"^8b?_"), FailureCategory.COLORIMETRY),
    (re.compile(r"^9_dv_colors"), FailureCategory.DV_COLORS),
    (re.compile(r"espace libre insuffisant"), FailureCategory.DISK_SPACE),
    (re.compile(r"sonde (?:ffprobe )?impossible|résolution vidéo indéterminée"), FailureCategory.SOURCE_UNREADABLE),
    (re.compile(r"aucune règle"), FailureCategory.NO_RULE),
    (re.compile(r"remplacement impossible"), FailureCategory.REPLACE_ERROR),
    (re.compile(r"erreur interne|bibliothèque supprimée"), FailureCategory.INTERNAL),
)


def classify_legacy(error_message: str | None, log_tail: str | None, exit_code: int | None) -> Failure:
    """Catégorie d'un échec enregistré avant la classification (backfill)."""
    message = error_message or ""
    if message.startswith("1_exit_code") or (not message and exit_code not in (None, 0)):
        failure = classify_ffmpeg_failure(exit_code, log_tail)
        return Failure(failure.category, message or failure.message, failure.detail)
    if message.startswith("5_integrity"):
        # Les erreurs de décodage des pistes AUDIO copiées (préexistantes dans
        # la source) faisaient échouer l'ancien contrôle d'intégrité
        body = message.partition("erreurs :")[2]
        if body.strip():
            if re.search(r"\[(?:aac|ac3|eac3|mp3\w*|mp2\w*|truehd|dts|opus|flac|vorbis)\b|aist#", body):
                return Failure(FailureCategory.SOURCE_CORRUPT, message, body.strip())
            if "non monoton" in body:
                return Failure(FailureCategory.TIMESTAMPS, message, body.strip())
    for pattern, category in _LEGACY_MESSAGE_PATTERNS:
        if pattern.search(message):
            return Failure(category, message, key_error_lines(log_tail))
    return Failure(FailureCategory.UNKNOWN, message or "échec", key_error_lines(log_tail))
