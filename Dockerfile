# =============================================================================
# Étape 1 — build du frontend (React/Vite)
# =============================================================================
FROM node:22-bookworm-slim AS webbuild
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# =============================================================================
# Étape 2 — image d'exécution : Debian 12 + Python 3.12 + jellyfin-ffmpeg
#            + drivers Intel (QSV/VAAPI) + Vulkan ANV (libplacebo/DV P5)
#            + dovi_tool + mkvtoolnix (mkvpropedit)
# =============================================================================
FROM python:3.12-slim-bookworm

ARG DOVI_TOOL_VERSION=2.1.2

# Dépôt jellyfin (jellyfin-ffmpeg) + composants non-free Debian
# (intel-media-va-driver-non-free requis pour l'encodage sur Arc).
# L'image python:*-slim-bookworm utilise le format deb822
# (/etc/apt/sources.list.d/debian.sources) : on active non-free et
# non-free-firmware en éditant Components, pas en ajoutant une 2e source.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian bookworm main" \
        > /etc/apt/sources.list.d/jellyfin.list \
    && if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
         sed -i 's/^Components: main.*/Components: main non-free non-free-firmware/' \
             /etc/apt/sources.list.d/debian.sources ; \
       else \
         echo "deb http://deb.debian.org/debian bookworm non-free non-free-firmware" \
             > /etc/apt/sources.list.d/non-free.list ; \
       fi \
    && apt-get update

# Paquets médias. Les runtimes QSV (libvpl2/libmfx-gen1.2/libmfx1) varient
# selon la version de Debian : installation tolérante — jellyfin-ffmpeg
# embarque de toute façon ses propres bibliothèques oneVPL dans
# /usr/lib/jellyfin-ffmpeg/lib, seuls le driver iHD et Vulkan sont requis.
RUN apt-get update && apt-get install -y --no-install-recommends \
        jellyfin-ffmpeg7 \
        intel-media-va-driver-non-free \
        mesa-vulkan-drivers \
        vainfo \
        mkvtoolnix \
    && for pkg in libvpl2 libmfx-gen1.2 libmfx1 intel-opencl-icd; do \
         apt-get install -y --no-install-recommends "$pkg" \
           || echo "paquet optionnel $pkg indisponible : ignoré (runtime fourni par jellyfin-ffmpeg)" ; \
       done \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# dovi_tool (extraction RPU / export L6, §5.3.1)
# L'archive de release contient un unique membre « ./dovi_tool » : extraction
# sans nom de membre (un `tar -x … dovi_tool` échouerait sur le préfixe ./)
RUN curl -fsSL -o /tmp/dovi_tool.tar.gz \
        "https://github.com/quietvoid/dovi_tool/releases/download/${DOVI_TOOL_VERSION}/dovi_tool-${DOVI_TOOL_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    && tar -xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin \
    && chmod +x /usr/local/bin/dovi_tool \
    && test -x /usr/local/bin/dovi_tool \
    && rm /tmp/dovi_tool.tar.gz

WORKDIR /srv/app

# Dépendances Python d'abord (cache docker), puis le code
COPY app/pyproject.toml ./pyproject.toml
COPY app/transcoder ./transcoder
COPY app/alembic ./alembic
COPY app/alembic.ini ./alembic.ini
COPY app/tests ./tests
RUN pip install --no-cache-dir ".[dev]"

# Build frontend servi en statique par FastAPI
COPY --from=webbuild /build/dist /srv/web/dist
ENV WEB_DIST_DIR=/srv/web/dist

# L'app tourne en PUID:PGID via `user:` du compose ; /data est un bind-mount
ENV HOME=/tmp
EXPOSE 8080

CMD ["python", "-m", "transcoder.main"]
