"""Surface de l'API : tous les endpoints du plan validé existent (sans DB).

Le schéma OpenAPI est généré sans démarrer le lifespan : c'est un test de
structure (chemins + méthodes), les comportements DB sont couverts par
l'e2e docker (module 7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from transcoder.main import create_app


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    return create_app().openapi()


EXPECTED_ENDPOINTS: list[tuple[str, str]] = [
    ("get", "/api/v1/libraries"),
    ("post", "/api/v1/libraries"),
    ("patch", "/api/v1/libraries/{library_id}"),
    ("delete", "/api/v1/libraries/{library_id}"),
    ("get", "/api/v1/libraries/{library_id}/bitrate-rules"),
    ("put", "/api/v1/libraries/{library_id}/bitrate-rules"),
    ("post", "/api/v1/libraries/{library_id}/scan"),
    ("get", "/api/v1/libraries/{library_id}/codec-change-preview"),
    ("get", "/api/v1/gpus"),
    ("post", "/api/v1/gpus"),
    ("patch", "/api/v1/gpus/{gpu_id}"),
    ("delete", "/api/v1/gpus/{gpu_id}"),
    ("post", "/api/v1/gpus/{gpu_id}/test"),
    ("get", "/api/v1/gpus/detect"),
    ("get", "/api/v1/queue"),
    ("post", "/api/v1/queue/pause"),
    ("post", "/api/v1/queue/resume"),
    ("post", "/api/v1/queue/reorder"),
    ("post", "/api/v1/queue/recover"),
    ("post", "/api/v1/files/requeue-dry-run"),
    ("get", "/api/v1/jobs"),
    ("get", "/api/v1/jobs/{job_id}"),
    ("get", "/api/v1/jobs/{job_id}/log"),
    ("post", "/api/v1/jobs/{job_id}/cancel"),
    ("post", "/api/v1/jobs/{job_id}/retry"),
    ("get", "/api/v1/files"),
    ("post", "/api/v1/files/{file_id}/force"),
    ("post", "/api/v1/files/{file_id}/ignore"),
    ("post", "/api/v1/files/{file_id}/reset"),
    ("get", "/api/v1/stats/summary"),
    ("get", "/api/v1/stats/savings-over-time"),
    ("get", "/api/v1/stats/codecs"),
    ("get", "/api/v1/stats/gain-by-bucket"),
    ("get", "/api/v1/stats/throughput"),
    ("get", "/api/v1/stats/top-remaining"),
    ("get", "/api/v1/stats/failures"),
    ("get", "/api/v1/stats/progress-by-library"),
    ("get", "/api/v1/settings"),
    ("patch", "/api/v1/settings"),
    ("get", "/api/v1/config/export"),
    ("post", "/api/v1/config/import"),
    ("get", "/api/v1/health"),
    ("get", "/api/v1/capabilities"),
]


@pytest.mark.parametrize(("method", "path"), EXPECTED_ENDPOINTS)
def test_endpoint_exists(openapi: dict[str, Any], method: str, path: str) -> None:
    assert path in openapi["paths"], f"chemin absent : {path}"
    assert method in openapi["paths"][path], f"méthode {method} absente sur {path}"


def test_websocket_routes_exist() -> None:
    # FastAPI encapsule les routers inclus (lazy) : on vérifie le router source
    from transcoder.api.routers.ws import router as ws_router

    ws_paths = {
        r.path for r in ws_router.routes if r.__class__.__name__ == "APIWebSocketRoute"
    }
    assert "/ws/queue" in ws_paths
    assert "/ws/jobs/{job_id}/log" in ws_paths


def test_pause_resume_accept_library_id(openapi: dict[str, Any]) -> None:
    params = openapi["paths"]["/api/v1/queue/pause"]["post"].get("parameters", [])
    assert any(p["name"] == "library_id" for p in params)


def test_spa_fallback_serves_index(tmp_path: "Path") -> None:
    """Les routes SPA (/queue, /stats…) doivent servir index.html, pas un 404."""
    from pathlib import Path as _P

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from transcoder.main import SpaStaticFiles

    dist = _P(tmp_path) / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    (dist / "app.js").write_text("js", encoding="utf-8")

    app = Starlette()
    app.mount("/", SpaStaticFiles(directory=str(dist), html=True))
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/app.js").text == "js"
    deep = client.get("/queue")
    assert deep.status_code == 200
    assert "SPA" in deep.text
