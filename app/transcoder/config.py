"""Configuration d'infrastructure, lue depuis l'environnement (.env).

Ne contient QUE ce qui est nécessaire avant que la base soit joignable
(connexion DB, chemins de montage, valeurs par défaut du seed).
Toute la configuration d'exploitation vit dans la table app_settings,
modifiable à chaud via l'UI (module runtime, livré plus tard).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Base de données -------------------------------------------------
    postgres_user: str = "transcoder"
    postgres_password: str = "change_me"
    postgres_db: str = "transcoder"
    postgres_host: str = "db"
    postgres_port: int = 5432

    # --- Réseau -----------------------------------------------------------
    http_port: int = 8080

    # --- Chemins ----------------------------------------------------------
    media_root: str = "/srv/storage"
    sandbox_root: str = "/srv/storage/_transcoder_sandbox"
    data_dir: str = "/data"

    # --- GPU --------------------------------------------------------------
    dri_device: str = "/dev/dri"
    render_gid: int = 993

    # --- Sécurité d'exploitation (valeurs initiales seedées en DB) --------
    dry_run: bool = True
    trash_retention_days: int = 14
    max_concurrent_jobs: int = 1
    puid: int = 1000
    pgid: int = 1000

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """URL pour les outils synchrones (alembic CLI hors app)."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
