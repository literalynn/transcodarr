#!/usr/bin/env bash
# ============================================================================
# check_gpu.sh — full diagnostic of the Intel GPU stack
#
# Usage:
#   ./scripts/check_gpu.sh                # diagnostic on the host
#   ./scripts/check_gpu.sh --container    # same diagnostic INSIDE the container
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
    ok "kernel $KVER (>= 6.2 required for Intel Arc)"
else
    ko "kernel $KVER too old: >= 6.2 required for Intel Arc (e.g. Debian backports)"
fi

echo "== DRM nodes =="
if ls /dev/dri/renderD* >/dev/null 2>&1; then
    for node in /dev/dri/renderD*; do
        ok "$node ($(stat -c '%G gid=%g' "$node"))"
    done
else
    ko "no /dev/dri/renderD* — is the i915/xe driver loaded? firmware-misc-nonfree installed?"
fi

echo "== render group =="
if RGID=$(getent group render | cut -d: -f3) && [[ -n "$RGID" ]]; then
    ok "render group: GID $RGID (put it in .env → RENDER_GID)"
else
    if [[ -e /dev/dri/renderD128 ]]; then
        info "no 'render' group in this context — node GID: $(stat -c '%g' /dev/dri/renderD128)"
    else
        ko "render group not found"
    fi
fi
if id | grep -qE "render|$(stat -c '%g' /dev/dri/renderD128 2>/dev/null || echo NOPE)"; then
    ok "current user can access the node's group"
else
    info "current user is not in the render group (normal on the host if not configured)"
fi

echo "== VAAPI (vainfo) =="
VAINFO=$(command -v vainfo || echo /usr/bin/vainfo)
if [[ -x "$VAINFO" ]]; then
    OUT=$("$VAINFO" --display drm --device /dev/dri/renderD128 2>&1)
    if echo "$OUT" | grep -q "VAProfileHEVCMain10.*Enc\|VAProfileHEVCMain10.*VAEntrypointEncSlice"; then
        ok "HEVC 10-bit encoder exposed by the driver"
    else
        ko "no HEVC Main10 encoder in vainfo (intel-media-va-driver-non-free?)"
    fi
    if echo "$OUT" | grep -q "VAProfileAV1.*Enc\|VAProfileAV1.*VAEntrypointEncSlice"; then
        ok "AV1 encoder exposed by the driver"
    else
        ko "no AV1 encoder in vainfo (requires Arc or newer + non-free driver)"
    fi
else
    ko "vainfo not found (apt install vainfo)"
fi

echo "== FFmpeg =="
FFMPEG="/usr/lib/jellyfin-ffmpeg/ffmpeg"
[[ -x "$FFMPEG" ]] || FFMPEG=$(command -v ffmpeg || true)
if [[ -n "$FFMPEG" && -x "$FFMPEG" ]]; then
    ok "binary: $FFMPEG ($($FFMPEG -version 2>/dev/null | head -1 | awk '{print $3}'))"
    for enc in hevc_qsv av1_qsv hevc_vaapi av1_vaapi; do
        if "$FFMPEG" -hide_banner -encoders 2>/dev/null | grep -q " $enc "; then
            ok "encoder $enc compiled in"
        else
            ko "encoder $enc missing from the binary"
        fi
    done
    if "$FFMPEG" -hide_banner -h filter=libplacebo 2>/dev/null | grep -q apply_dolbyvision; then
        ok "libplacebo with apply_dolbyvision (DV profile 5 supported)"
    else
        ko "libplacebo without apply_dolbyvision: DV profile 5 files will be skipped_policy"
    fi
    echo "== 5 s test pattern encode (real) =="
    for enc in hevc_qsv av1_qsv; do
        if "$FFMPEG" -hide_banner -nostdin -v error \
            -init_hw_device qsv=hw:/dev/dri/renderD128 \
            -f lavfi -i testsrc2=duration=5:size=1920x1080:rate=25 \
            -pix_fmt nv12 -c:v "$enc" -b:v 3000k -f null - 2>/dev/null; then
            ok "5 s test pattern encoded with $enc"
        else
            ko "$enc encode failed (permissions on /dev/dri? group_add?)"
        fi
    done
else
    ko "ffmpeg not found"
fi

echo "== dovi_tool / mkvpropedit =="
command -v dovi_tool >/dev/null && ok "dovi_tool $(dovi_tool --version 2>/dev/null | awk '{print $2}')" || info "dovi_tool missing (normal on the host, required in the container)"
command -v mkvpropedit >/dev/null && ok "mkvpropedit present" || info "mkvpropedit missing (normal on the host, required in the container)"

echo
if [[ $FAIL -eq 0 ]]; then
    echo "Diagnostic: all good. ✅"
else
    echo "Diagnostic: at least one blocking issue. ❌"
fi
exit $FAIL
