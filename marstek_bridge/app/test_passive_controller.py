"""
Tests + Demo fuer passive_controller.py

Simuliert einen Shelly-Eingang (alle 5s ein neuer Rohwert) ueber virtuelle
Zeit (kein echtes sleep noetig -> schnell und deterministisch).
"""

import logging

from passive_controller import PassiveController, PassiveControllerConfig

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")


def run_series(cfg: PassiveControllerConfig, series, step_s=5.0):
    """series: Liste von roh berechneten Sollwerten. Gibt Liste der (t, gesendetes_kommando_oder_None) zurueck."""
    ctrl = PassiveController(cfg)
    results = []
    t = 0.0
    for raw in series:
        cmd = ctrl.update(raw, now=t)
        results.append((t, raw, cmd))
        t += step_s
    return results, ctrl


def print_results(title, results):
    print(f"\n=== {title} ===")
    for t, raw, cmd in results:
        sent = f"-> SEND {cmd}" if cmd else "   (kein Senden)"
        print(f"t={t:6.1f}s raw={raw:8.1f}W {sent}")


def test_deadzone_ignores_noise():
    cfg = PassiveControllerConfig(deadzone_w=40, min_setpoint_change_w=50, max_step_w=125, min_send_interval_s=0)
    # Rauschen um 0 herum, alles < Totzone
    series = [0, 10, -15, 20, -5, 12, -20]
    results, ctrl = run_series(cfg, series)
    print_results("Totzone unterdrueckt Rauschen um 0W", results)
    sent = [c for _, _, c in results if c]
    assert len(sent) == 1, "nur die Erstinitialisierung darf senden"


def test_slow_trend_ramps_up_gradually():
    cfg = PassiveControllerConfig(deadzone_w=30, min_setpoint_change_w=50, max_step_w=100, min_send_interval_s=0)
    # Sprunghafter Zielwert 0 -> 500W, muss ueber mehrere Zyklen rangefahren werden
    series = [0] + [500] * 8
    results, ctrl = run_series(cfg, series)
    print_results("Sprung auf 500W wird durch Slew-Rate abgeflacht", results)
    sent_powers = [c["power"] for _, _, c in results if c]
    assert sent_powers[0] == 1  # Start bei 0 -> vermieden, auf 1W gesetzt
    assert max(sent_powers) <= 500
    assert sent_powers == sorted(sent_powers), "Sollwert darf sich nur monoton annaehern (kein Ueberschwingen)"
    assert sent_powers[-1] == 500 or ctrl.state.committed_setpoint_w == 500


def test_min_send_interval_holds_off():
    cfg = PassiveControllerConfig(deadzone_w=10, min_setpoint_change_w=20, max_step_w=200, min_send_interval_s=30)
    # alle 5s ein neuer, deutlich unterschiedlicher Wert -> Aenderung waere jedes mal noetig,
    # aber Hold-off (30s) soll das Senden auf max. jede 6. Messung begrenzen
    series = [0, 200, 210, 220, 230, 240, 250, 260]
    results, ctrl = run_series(cfg, series)
    print_results("Hold-off (30s) begrenzt Sendehaeufigkeit", results)
    send_times = [t for t, _, c in results if c]
    for a, b in zip(send_times, send_times[1:]):
        assert b - a >= 30, "zwei Sendungen duerfen (ohne Safety-Trigger) nicht < 30s auseinanderliegen"


def test_safety_clamp_overrides_holdoff_with_warning(caplog):
    cfg = PassiveControllerConfig(deadzone_w=10, min_setpoint_change_w=20, max_step_w=2000, min_send_interval_s=30,
                                   min_output_w=-1500, max_output_w=800)
    series = [0, 5000]  # zweiter Wert weit ueber max_output_w -> Sicherheitsgrenze
    with caplog.at_level(logging.WARNING):
        results, ctrl = run_series(cfg, series, step_s=5.0)
    print_results("Sicherheitsgrenze erzwingt Senden trotz Hold-off", results)
    assert results[1][2] is not None, "Sicherheitsgrenze muss trotz Hold-off senden"
    assert results[1][2]["power"] == 800
    assert any("Sicherheitsgrenze" in rec.message for rec in caplog.records)


def test_min_setpoint_change_blocks_tiny_updates_even_without_holdoff():
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=50, max_step_w=125, min_send_interval_s=0)
    series = [0, 60, 70, 80]  # nach dem ersten Schritt (0->60, gesendet) sind 70/80 zu klein ggue. 60
    results, ctrl = run_series(cfg, series)
    print_results("Mindeständerung verhindert Mini-Nachjustierungen", results)
    sent = [c for _, _, c in results if c]
    assert len(sent) == 2  # initial (0->1W) + Sprung auf 60W; 70/80 zu klein ggue. 60


def test_config_validation():
    import pytest
    with pytest.raises(ValueError):
        PassiveControllerConfig(min_send_interval_s=90).validate()
    with pytest.raises(ValueError):
        PassiveControllerConfig(min_output_w=100, max_output_w=50).validate()


if __name__ == "__main__":
    # Demo-Modus ohne pytest: einfach direkt ausfuehren und Log/Ausgabe anschauen
    test_deadzone_ignores_noise()
    test_slow_trend_ramps_up_gradually()
    test_min_send_interval_holds_off()

    class _FakeCapLog:
        class _Rec:
            def __init__(self, message):
                self.message = message
        def __init__(self):
            self.records = []
        def at_level(self, *_a, **_k):
            import contextlib
            return contextlib.nullcontext()

    # safety-clamp Test manuell ohne pytest-caplog-Fixture
    import io
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("marstek.passive_controller").addHandler(handler)
    cfg = PassiveControllerConfig(deadzone_w=10, min_setpoint_change_w=20, max_step_w=2000, min_send_interval_s=30)
    results, ctrl = run_series(cfg, [0, 5000])
    print_results("Sicherheitsgrenze erzwingt Senden trotz Hold-off", results)
    assert "Sicherheitsgrenze" in stream.getvalue()

    test_min_setpoint_change_blocks_tiny_updates_even_without_holdoff()

    print("\nAlle Demo-Checks OK.")
