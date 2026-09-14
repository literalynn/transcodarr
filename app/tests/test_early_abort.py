"""Abandon anticipé confirmé : jamais sur une seule mesure (core/early_abort.py)."""

from __future__ import annotations

from collections.abc import Callable

from transcoder.core.early_abort import EarlyAbortMonitor

MB = 1_000_000


def _run(monitor: EarlyAbortMonitor, size_at: Callable[[float], float]) -> float | None:
    """Rejoue une progression de 0,5 en 0,5 % ; renvoie l'avancement de l'abandon."""
    percent = 0.5
    while percent < 100.0:
        decision = monitor.update(percent, int(size_at(percent)))
        if decision is not None:
            return decision.percent
        percent += 0.5
    return None


def test_real_gain_is_never_aborted() -> None:
    # Mesuré en production sur un épisode : 1 563 → 651 Mo (−58 %)
    monitor = EarlyAbortMonitor(src_size_bytes=1563 * MB, min_gain_percent=5.0)
    assert _run(monitor, lambda p: 651 * MB * p / 100) is None


def test_true_no_gain_is_aborted_after_confirmation() -> None:
    monitor = EarlyAbortMonitor(src_size_bytes=1000 * MB, min_gain_percent=5.0)
    aborted_at = _run(monitor, lambda p: 1200 * MB * p / 100)
    assert aborted_at is not None
    assert 25.0 <= aborted_at < 26.0  # alerte à 15 %, confirmation 10 points plus loin


def test_fixed_overhead_written_early_does_not_abort() -> None:
    # 300 Mo écrits d'emblée (polices, paquets en avance), puis flux normal :
    # la projection simple déborde longtemps, la pente reste juste
    monitor = EarlyAbortMonitor(src_size_bytes=800 * MB, min_gain_percent=5.0)
    assert _run(monitor, lambda p: 300 * MB + 300 * MB * p / 100) is None


def test_transient_spike_is_not_confirmed() -> None:
    monitor = EarlyAbortMonitor(src_size_bytes=1000 * MB, min_gain_percent=5.0)

    def size(p: float) -> float:
        normal = 500 * MB * p / 100
        return normal * 2.5 if 15 <= p < 18 else normal  # début d'épisode très chargé

    assert _run(monitor, size) is None


def test_decision_carries_audit_figures() -> None:
    monitor = EarlyAbortMonitor(src_size_bytes=1000 * MB, min_gain_percent=5.0)
    assert monitor.update(10.0, 150 * MB) is None  # avant 15 % : jamais
    assert monitor.update(15.0, 180 * MB) is None  # alerte
    assert monitor.update(20.0, 240 * MB) is None  # trop proche de l'alerte
    decision = monitor.update(26.0, 312 * MB)
    assert decision is not None
    figures = decision.as_dict()
    assert figures["alert_percent"] == 15.0 and figures["percent"] == 26.0
    assert figures["threshold_bytes"] == int(1000 * MB * 0.95 * 1.05)
    assert figures["slope_projected_bytes"] > figures["threshold_bytes"]
    assert "confirmée entre 15 % et 26 %" in decision.describe()
