#!/usr/bin/env bash
# ============================================================================
# check_gpu.sh — diagnostic complet de la chaîne GPU Intel (§9.5.1)
#
# Usage :
#   ./scripts/check_gpu.sh                # diagnostic sur l'hôte
#   ./scripts/check_gpu.sh --container    # même diagnostic DANS le conteneur
# ============================================================================
set -uo pipefail

if [[ "${1:-}" == "--container" ]]; then
    exec docker compose exec app bash -c "$(sed '1,/^# __IN_CONTAINER__$/d' "$0")"
fi

# __IN_CONTAINER__
FAIL=0
ok()   { printf '  \033[32m✔\033[0m %s\n' "$1"; }
ko()   { printf '  \033[31m✘\033[0m %s\n' "$1"; FAIL=1; }
info() { printf '  \033[2m…\033[0m %s\n' "$1"; }

echo "== Kernel =="
KVER=$(uname -r)
KMAJ=${KVER%%.*}; KMIN=$(echo "$KVER" | cut -d. -f2)
if [[ "$KMAJ" -gt 6 || ( "$KMAJ" -eq 6 && "$KMIN" -ge 2 ) ]]; then
    ok "kernel $KVER (>= 6.2 requis pour l'Arc A770)"
else
    ko "kernel $KVER trop ancien : >= 6.2 requis pour l'Arc (backports Debian)"
fi

echo "== Nodes DRM =="
if ls /dev/dri/renderD* >/dev/null 2>&1; then
    for node in /dev/dri/renderD*; do
        ok "$node ($(stat -c '%G gid=%g' "$node"))"
    done
else
    ko "aucun /dev/dri/renderD* — driver i915 chargé ? firmware-misc-nonfree installé ?"
fi

echo "== Groupe render =="
if RGID=$(getent group render | cut -d: -f3) && [[ -n "$RGID" ]]; then
    ok "groupe render : GID $RGID (à mettre dans .env → RENDER_GID)"
else
    if [[ -e /dev/dri/renderD128 ]]; then
        info "pas de groupe 'render' dans ce contexte — GID du node : $(stat -c '%g' /dev/dri/renderD128)"
    else
        ko "groupe render introuvable"
    fi
fi
if id | grep -qE "render|$(stat -c '%g' /dev/dri/renderD128 2>/dev/null || echo NOPE)"; then
    ok "l'utilisateur courant a accès au groupe du node"
else
    info "utilisateur courant hors groupe render (normal sur l'hôte si non configuré)"
fi

echo "== VAAPI (vainfo) =="
VAINFO=$(command -v vainfo || echo /usr/bin/vainfo)
if [[ -x "$VAINFO" ]]; then
    OUT=$("$VAINFO" --display drm --device /dev/dri/renderD128 2>&1)
    if echo "$OUT" | grep -q "VAProfileHEVCMain10.*Enc\|VAProfileHEVCMain10.*VAEntrypointEncSlice"; then
        ok "encodeur HEVC 10-bit exposé par le driver"
    else
        ko "pas d'encodeur HEVC Main10 dans vainfo (intel-media-va-driver-non-free ?)"
    fi
    if echo "$OUT" | grep -q "VAProfileAV1.*Enc\|VAProfileAV1.*VAEntrypointEncSlice"; then
        ok "encodeur AV1 exposé par le driver"
    else
        ko "pas d'encodeur AV1 dans vainfo (Arc requis + driver non-free)"
    fi
else
    ko "vainfo introuvable (apt install vainfo)"
fi

echo "== FFmpeg =="
FFMPEG="/usr/lib/jellyfin-ffmpeg/ffmpeg"
[[ -x "$FFMPEG" ]] || FFMPEG=$(command -v ffmpeg || true)
if [[ -n "$FFMPEG" && -x "$FFMPEG" ]]; then
    ok "binaire : $FFMPEG ($($FFMPEG -version 2>/dev/null | head -1 | awk '{print $3}'))"
    for enc in hevc_qsv av1_qsv hevc_vaapi av1_vaapi; do
        if "$FFMPEG" -hide_banner -encoders 2>/dev/null | grep -q " $enc "; then
            ok "encodeur $enc compilé"
        else
            ko "encodeur $enc absent du binaire"
        fi
    done
    if "$FFMPEG" -hide_banner -h filter=libplacebo 2>/dev/null | grep -q apply_dolbyvision; then
        ok "libplacebo avec apply_dolbyvision (DV P5 possible)"
    else
        ko "libplacebo sans apply_dolbyvision : les DV P5 seront skipped_policy"
    fi
    echo "== Encodage de mire 5 s (test réel) =="
    for enc in hevc_qsv av1_qsv; do
        if "$FFMPEG" -hide_banner -nostdin -v error \
            -init_hw_device qsv=hw:/dev/dri/renderD128 \
            -f lavfi -i testsrc2=duration=5:size=1920x1080:rate=25 \
            -pix_fmt nv12 -c:v "$enc" -b:v 3000k -f null - 2>/dev/null; then
            ok "mire 5 s encodée en $enc"
        else
            ko "échec d'encodage $enc (droits sur /dev/dri ? group_add ?)"
        fi
    done
else
    ko "ffmpeg introuvable"
fi

echo "== dovi_tool / mkvpropedit =="
command -v dovi_tool >/dev/null && ok "dovi_tool $(dovi_tool --version 2>/dev/null | awk '{print $2}')" || info "dovi_tool absent (hôte : normal, requis dans le conteneur)"
command -v mkvpropedit >/dev/null && ok "mkvpropedit présent" || info "mkvpropedit absent (hôte : normal, requis dans le conteneur)"

echo
if [[ $FAIL -eq 0 ]]; then
    echo "Diagnostic : tout est bon. ✅"
else
    echo "Diagnostic : au moins un point bloquant. ❌"
fi
exit $FAIL
