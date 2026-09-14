"""Abandon anticipé d'un encodage sans gain : projection CONFIRMÉE (pur).

Historique : la 0.9 abandonnait dès 15 % si `taille écrite ÷ avancement`
dépassait le seuil de gain. Constaté en production : 23 639 fichiers jugés
« sans gain » ainsi, dont des épisodes qui gagnent 15 à 58 % une fois
encodés en entier — cause non reproduite (même commande, mêmes pilotes : la
projection tombe aujourd'hui à ±3 %). Une décision sur un seul point de
mesure est trop fragile pour condamner un fichier.

Règles actuelles :
- première alerte à 15 % seulement si la projection simple dépasse le seuil ;
- confirmation au moins 10 points plus loin (≥ 25 %) par la PENTE entre les
  deux mesures (octets par point d'avancement) : un surplus fixe écrit au
  début (en-tête, polices, paquets en avance) ne fausse plus la projection ;
- abandon uniquement si les deux projections, simple et par pente,
  dépassent encore le seuil ; sinon l'alerte est levée et l'encodage continue ;
- les chiffres de la décision sont conservés dans le job (audit).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FIRST_CHECK_PERCENT = 15.0
CONFIRM_GAP_PERCENT = 10.0
# Marge anti-bruit VBR sur le seuil de gain de la bibliothèque
THRESHOLD_MARGIN = 1.05


@dataclass(frozen=True, slots=True)
class AbortDecision:
    percent: float
    written_bytes: int
    projected_bytes: int
    slope_projected_bytes: int
    threshold_bytes: int
    alert_percent: float
    alert_written_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "percent": round(self.percent, 1),
            "written_bytes": self.written_bytes,
            "projected_bytes": self.projected_bytes,
            "slope_projected_bytes": self.slope_projected_bytes,
            "threshold_bytes": self.threshold_bytes,
            "alert_percent": round(self.alert_percent, 1),
            "alert_written_bytes": self.alert_written_bytes,
        }

    def describe(self) -> str:
        gib = 1024**3
        return (
            f"taille projetée {self.slope_projected_bytes / gib:.2f} Gio > seuil "
            f"{self.threshold_bytes / gib:.2f} Gio, confirmée entre "
            f"{self.alert_percent:.0f} % et {self.percent:.0f} %"
        )


class EarlyAbortMonitor:
    """Suit la progression d'un encodage et décide d'un abandon confirmé."""

    def __init__(self, *, src_size_bytes: int, min_gain_percent: float) -> None:
        self.threshold_bytes = int(
            src_size_bytes * (1.0 - min_gain_percent / 100.0) * THRESHOLD_MARGIN
        )
        self._alert: tuple[float, int] | None = None

    def update(self, percent: float | None, written_bytes: int | None) -> AbortDecision | None:
        """Nouvelle mesure ; renvoie la décision d'abandon quand elle est confirmée."""
        if (
            self.threshold_bytes <= 0
            or percent is None
            or written_bytes is None
            or percent < FIRST_CHECK_PERCENT
            or percent >= 100.0
        ):
            return None
        projected = written_bytes / (percent / 100.0)
        if self._alert is None:
            if projected > self.threshold_bytes:
                self._alert = (percent, written_bytes)
            return None
        alert_percent, alert_written = self._alert
        if percent - alert_percent < CONFIRM_GAP_PERCENT:
            return None
        slope = (written_bytes - alert_written) / (percent - alert_percent)
        slope_projected = written_bytes + slope * (100.0 - percent)
        if projected > self.threshold_bytes and slope_projected > self.threshold_bytes:
            return AbortDecision(
                percent=percent,
                written_bytes=written_bytes,
                projected_bytes=int(projected),
                slope_projected_bytes=int(slope_projected),
                threshold_bytes=self.threshold_bytes,
                alert_percent=alert_percent,
                alert_written_bytes=alert_written,
            )
        # Alerte non confirmée : l'encodage continue, nouvelle alerte possible
        self._alert = (percent, written_bytes) if projected > self.threshold_bytes else None
        return None
