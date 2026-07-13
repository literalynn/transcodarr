"""Intégration Jellyfin optionnelle : dates d'ajout fiables (§5.1).

`btime`/`max(mtime, ctime)` approxime la date d'ajout au NAS mais ment
après une copie de fichiers. Si l'utilisateur fournit URL + API key,
le scanner croise les chemins avec `DateCreated` de Jellyfin.
Toute erreur est non bloquante : dict vide + warning.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def parse_items_dates(payload: dict[str, Any]) -> dict[str, datetime.datetime]:
    """Extrait {chemin: DateCreated} d'une réponse /Items (fonction pure)."""
    out: dict[str, datetime.datetime] = {}
    items = payload.get("Items")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        path = item.get("Path")
        raw = item.get("DateCreated")
        if not isinstance(path, str) or not isinstance(raw, str):
            continue
        try:
            # Jellyfin renvoie de l'ISO 8601 avec nanosecondes : on tronque
            trimmed = raw.rstrip("Z")
            if "." in trimmed:
                head, _, frac = trimmed.partition(".")
                trimmed = f"{head}.{frac[:6]}"
            dt = datetime.datetime.fromisoformat(trimmed).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            continue
        out[path] = dt
    return out


async def fetch_dates_created(url: str, api_key: str) -> dict[str, datetime.datetime]:
    """Récupère toutes les DateCreated indexées par chemin. Jamais bloquant."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path,DateCreated",
                    "EnableImages": "false",
                },
                headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            )
            resp.raise_for_status()
            payload: Any = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Intégration Jellyfin indisponible (%s) : fallback btime/mtime", exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    dates = parse_items_dates(payload)
    logger.info("Jellyfin : %d dates d'ajout récupérées", len(dates))
    return dates
