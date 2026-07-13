#!/usr/bin/env bash
# ============================================================================
# make_sample_set.sh — construit un échantillon de rodage SANS RISQUE (§9.4)
#
# Copie N fichiers par bibliothèque depuis ${MEDIA_ROOT}/{Films,Séries,Animés}
# vers ${SANDBOX_ROOT}/… en préservant les mtime (cp -p) et en reflink si le
# filesystem le supporte (copie instantanée, pas de double espace).
#
# Usage :
#   ./scripts/make_sample_set.sh [-n N] [--hdr] [--dv] [--4k] [--h264]
#                                [--already-hevc] [--random]
#
# Les options de sélection se cumulent (OR) : --dv --hdr prend les fichiers
# DV OU HDR. Sans option : --random.
# Nécessite ffprobe sur l'hôte (apt install ffmpeg) pour la sélection ciblée ;
# sans ffprobe, seul --random est possible.
# ============================================================================
set -euo pipefail

ENV_FILE="$(dirname "$0")/../.env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a
MEDIA_ROOT="${MEDIA_ROOT:-/srv/storage}"
SANDBOX_ROOT="${SANDBOX_ROOT:-/srv/storage/_transcoder_sandbox}"
LIBS=("Films" "Séries" "Animés")

N=5
WANT_HDR=0 WANT_DV=0 WANT_4K=0 WANT_H264=0 WANT_HEVC=0 WANT_RANDOM=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n) N="$2"; shift 2 ;;
        --hdr) WANT_HDR=1; shift ;;
        --dv) WANT_DV=1; shift ;;
        --4k) WANT_4K=1; shift ;;
        --h264) WANT_H264=1; shift ;;
        --already-hevc) WANT_HEVC=1; shift ;;
        --random) WANT_RANDOM=1; shift ;;
        *) echo "option inconnue : $1" >&2; exit 2 ;;
    esac
done
if [[ $((WANT_HDR + WANT_DV + WANT_4K + WANT_H264 + WANT_HEVC)) -eq 0 ]]; then
    WANT_RANDOM=1
fi

FFPROBE=$(command -v ffprobe || true)
if [[ -z "$FFPROBE" && $WANT_RANDOM -eq 0 ]]; then
    echo "ffprobe requis pour la sélection ciblée (apt install ffmpeg)" >&2
    exit 1
fi

# probe <fichier> → "codec height transfer dv" (dv=1 si DOVI record)
probe_file() {
    local f="$1"
    local json
    json=$("$FFPROBE" -v quiet -print_format json -show_streams "$f" 2>/dev/null) || return 1
    python3 - "$json" <<'PYEOF'
import json, sys
data = json.loads(sys.argv[1])
v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
if v is None:
    sys.exit(1)
dv = any("DOVI" in str(sd.get("side_data_type", ""))
         for sd in v.get("side_data_list", []))
print(v.get("codec_name", "?"), v.get("height", 0),
      v.get("color_transfer", "none"), 1 if dv else 0)
PYEOF
}

matches() {
    local codec="$1" height="$2" transfer="$3" dv="$4"
    [[ $WANT_RANDOM -eq 1 ]] && return 0
    [[ $WANT_DV -eq 1 && "$dv" == "1" ]] && return 0
    [[ $WANT_HDR -eq 1 && ( "$transfer" == "smpte2084" || "$transfer" == "arib-std-b67" ) ]] && return 0
    [[ $WANT_4K -eq 1 && "$height" -ge 1800 ]] && return 0
    [[ $WANT_H264 -eq 1 && "$codec" == "h264" ]] && return 0
    [[ $WANT_HEVC -eq 1 && "$codec" == "hevc" ]] && return 0
    return 1
}

# Détection reflink
REFLINK_NOTE=""
if cp --help 2>/dev/null | grep -q reflink; then
    CP=(cp --reflink=auto -p)
    REFLINK_NOTE="(reflink=auto : instantané si le FS le supporte, sinon copie réelle — surveiller l'espace)"
else
    CP=(cp -p)
    REFLINK_NOTE="(cp classique : l'espace de l'échantillon s'ajoute — surveiller l'espace disque)"
fi

echo "Échantillon : $N fichier(s)/bibliothèque → $SANDBOX_ROOT $REFLINK_NOTE"
TOTAL=0; TOTAL_BYTES=0
declare -a SUMMARY=()

for lib in "${LIBS[@]}"; do
    SRC="$MEDIA_ROOT/$lib"
    DST="$SANDBOX_ROOT/$lib"
    [[ -d "$SRC" ]] || { echo "  $SRC absent, ignoré"; continue; }
    mkdir -p "$DST"
    COUNT=0
    # Parcours mélangé pour éviter de toujours prendre les mêmes
    while IFS= read -r f; do
        [[ $COUNT -ge $N ]] && break
        codec="?"; height=0; transfer="none"; dv=0
        if [[ -n "$FFPROBE" ]]; then
            read -r codec height transfer dv <<<"$(probe_file "$f" || echo '? 0 none 0')"
        fi
        matches "$codec" "$height" "$transfer" "$dv" || continue
        base=$(basename "$f")
        "${CP[@]}" "$f" "$DST/$base"
        size=$(stat -c %s "$DST/$base" 2>/dev/null || stat -f %z "$DST/$base")
        COUNT=$((COUNT + 1)); TOTAL=$((TOTAL + 1)); TOTAL_BYTES=$((TOTAL_BYTES + size))
        hdrtag="SDR"
        [[ "$transfer" == "smpte2084" ]] && hdrtag="HDR10/PQ"
        [[ "$transfer" == "arib-std-b67" ]] && hdrtag="HLG"
        [[ "$dv" == "1" ]] && hdrtag="DolbyVision"
        SUMMARY+=("$lib | $base | $codec | ${height}p | $hdrtag | $((size / 1024 / 1024)) Mio")
    done < <(find "$SRC" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m2ts' -o -iname '*.ts' -o -iname '*.avi' \) ! -path '*/.transcoder_tmp/*' | sort -R)
    echo "  $lib : $COUNT fichier(s) copiés"
done

echo
echo "== Récapitulatif =="
printf '%s\n' "${SUMMARY[@]:-aucun fichier ne correspond aux critères}"
echo
echo "Total : $TOTAL fichier(s), $((TOTAL_BYTES / 1024 / 1024 / 1024)) Gio dans $SANDBOX_ROOT"
echo "→ créer dans l'UI 3 bibliothèques de test pointant sur $SANDBOX_ROOT/{Films,Séries,Animés}"
