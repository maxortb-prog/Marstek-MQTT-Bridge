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
    assert any("trotz Hold-off" in rec.message for rec in caplog.records)


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


def test_discharge_cap_restricts_output_below_max_output_w():
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_output_w=-1500, max_output_w=800, min_send_interval_s=0)
    ctrl = PassiveController(cfg)
    ctrl.set_discharge_cap(200)  # enger als max_output_w=800

    cmd = ctrl.update(700, now=0.0)  # Anfrage 700W, aber Deckel bei 200W
    assert cmd is not None
    assert cmd["power"] == 200, "Deckel muss die Ausgabe auf 200W begrenzen, obwohl max_output_w=800 erlauben wuerde"


def test_discharge_cap_cannot_exceed_configured_max_output_w():
    """Der Deckel darf die konfigurierte Obergrenze nur verschaerfen, nie lockern."""
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_output_w=-1500, max_output_w=800, min_send_interval_s=0)
    ctrl = PassiveController(cfg)
    ctrl.set_discharge_cap(5000)  # Deckel hoeher als max_output_w -> darf nichts bewirken

    cmd = ctrl.update(2000, now=0.0)
    assert cmd["power"] == 800, "max_output_w muss weiterhin die harte Obergrenze bleiben"


def test_discharge_cap_does_not_affect_charging_direction():
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_output_w=-1500, max_output_w=800, min_send_interval_s=0)
    ctrl = PassiveController(cfg)
    ctrl.set_discharge_cap(100)  # soll nur die Entlade-/Einspeiserichtung betreffen

    cmd = ctrl.update(-1200, now=0.0)  # Laden, deutlich unterhalb des Entlade-Deckels
    assert cmd["power"] == -1200, "Lade-Richtung darf vom Entlade-Deckel nicht betroffen sein"


def test_discharge_cap_reset_to_none_restores_max_output_w():
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_output_w=-1500, max_output_w=800, min_send_interval_s=0)
    ctrl = PassiveController(cfg)
    ctrl.set_discharge_cap(100)
    ctrl.set_discharge_cap(None)  # z.B. SOC wieder hoch -> Deckel aufheben

    cmd = ctrl.update(700, now=0.0)
    assert cmd["power"] == 700


def test_cd_time_override_used_instead_of_config_default():
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_send_interval_s=0, default_cd_time_s=60, max_cd_time_s=3600)
    ctrl = PassiveController(cfg)
    ctrl.set_cd_time(120)

    cmd = ctrl.update(300, now=0.0)
    assert cmd["cd_time"] == 120


def test_cd_time_override_still_capped_by_max_cd_time_s():
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_send_interval_s=0, default_cd_time_s=60, max_cd_time_s=300)
    ctrl = PassiveController(cfg)
    ctrl.set_cd_time(9999)  # unsinnig hoch, muss trotzdem gekappt werden

    cmd = ctrl.update(300, now=0.0)
    assert cmd["cd_time"] == 300


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


def test_keepalive_resends_unchanged_setpoint_before_cd_time_expires():
    """Kernanforderung: bleibt der Sollwert innerhalb der Totzone (kein
    echtes Update noetig), muss trotzdem kurz vor Ablauf von cd_time
    erneut gesendet werden, sonst verliert das Geraet seinen Passive-
    Sollwert."""
    cfg = PassiveControllerConfig(deadzone_w=40, min_setpoint_change_w=50, max_step_w=125,
                                   min_send_interval_s=0, default_cd_time_s=60, max_cd_time_s=3600)
    ctrl = PassiveController(cfg)

    # Initiale Sendung bei t=0
    cmd0 = ctrl.update(300, now=0.0)
    assert cmd0 is not None and cmd0["power"] == 300

    # t=10s: Wert bleibt praktisch gleich (innerhalb Totzone) -> kein Senden
    assert ctrl.update(305, now=10.0) is None

    # t=45s: cd_time=60s, Margin=max(5, 60*0.2)=12s -> Keepalive-Schwelle bei 60-12=48s
    # noch nicht erreicht -> weiterhin kein Senden
    assert ctrl.update(305, now=45.0) is None

    # t=50s: Schwelle (48s) ueberschritten -> Keepalive muss trotz Totzone senden
    cmd = ctrl.update(305, now=50.0)
    assert cmd is not None, "Keepalive haette senden muessen, um cd_time zu resetten"
    assert cmd["power"] == 300, "Keepalive sendet den unveraenderten Sollwert, nicht den neuen Rohwert"
    assert cmd["cd_time"] == 60


def test_keepalive_does_not_trigger_before_margin():
    cfg = PassiveControllerConfig(deadzone_w=40, min_setpoint_change_w=50, max_step_w=125,
                                   min_send_interval_s=0, default_cd_time_s=100, max_cd_time_s=3600)
    ctrl = PassiveController(cfg)
    ctrl.update(300, now=0.0)
    # Margin = max(5, 100*0.2) = 20 -> Schwelle bei 80s
    assert ctrl.update(305, now=79.0) is None
    cmd = ctrl.update(305, now=81.0)
    assert cmd is not None


def test_keepalive_bypasses_holdoff():
    """Keepalive muss auch dann senden, wenn der normale Mindestabstand
    (min_send_interval_s) noch nicht erreicht ist - sonst koennte cd_time
    trotzdem ablaufen."""
    cfg = PassiveControllerConfig(deadzone_w=40, min_setpoint_change_w=50, max_step_w=125,
                                   min_send_interval_s=55, default_cd_time_s=60, max_cd_time_s=3600)
    ctrl = PassiveController(cfg)
    ctrl.update(300, now=0.0)
    # Keepalive-Schwelle bei 48s liegt VOR dem Hold-off-Ende bei 55s
    cmd = ctrl.update(305, now=50.0)
    assert cmd is not None, "Keepalive haette trotz Hold-off senden muessen"


def test_no_keepalive_when_real_update_already_happening():
    """Wenn ohnehin ein echtes Update ansteht, ist keine gesonderte
    Keepalive-Logik noetig - der normale Sendepfad greift bereits."""
    cfg = PassiveControllerConfig(deadzone_w=10, min_setpoint_change_w=10, max_step_w=2000,
                                   min_send_interval_s=0, default_cd_time_s=60, max_cd_time_s=3600)
    ctrl = PassiveController(cfg)
    ctrl.update(300, now=0.0)
    cmd = ctrl.update(600, now=5.0)  # deutliche Aenderung, weit vor cd_time-Ablauf
    assert cmd is not None
    assert cmd["power"] == 600


def test_discharge_cap_does_not_force_immediate_resend_when_unchanged():
    """Kernanforderung: haengt der Sollwert dauerhaft am dynamischen
    Entlade-Deckel (z.B. SOC-Automatisierung), darf das NICHT bei jedem
    Zyklus ein erzwungenes Sofort-Senden ausloesen, solange sich der
    tatsaechlich gesendete Wert nicht aendert. Totzone/Hold-off muessen
    normal greifen."""
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_output_w=-1500, max_output_w=800, min_send_interval_s=30)
    ctrl = PassiveController(cfg)
    ctrl.set_discharge_cap(200)  # aktiv reduzierter SOC-Deckel

    # Initiale Sendung: Rohwert 300 wird auf den Deckel (200) geklemmt
    cmd0 = ctrl.update(300, now=0.0)
    assert cmd0 is not None and cmd0["power"] == 200

    # Mehrere Folgezyklen mit unterschiedlichen Rohwerten, die aber alle
    # ueber dem Deckel liegen -> geklemmter Wert bleibt immer 200 ->
    # darf NICHT jedes Mal senden (kein Sicherheitsereignis mehr)
    assert ctrl.update(254, now=5.0) is None
    assert ctrl.update(275, now=10.0) is None
    assert ctrl.update(271, now=15.0) is None
    assert ctrl.update(262, now=20.0) is None


def test_hard_output_limit_still_forces_immediate_send():
    """Regressionstest: die ECHTEN harten Grenzen (min_output_w/max_output_w)
    muessen weiterhin sofort senden (Sicherheitsverhalten bleibt erhalten),
    nur der dynamische SOC-Deckel wurde davon ausgenommen."""
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_output_w=-1500, max_output_w=800, min_send_interval_s=30)
    ctrl = PassiveController(cfg)
    ctrl.update(800, now=0.0)  # committed = 800 (am Limit)

    # kurz danach (< min_send_interval_s) erneut ein Wert, der ueber die
    # harte max_output_w-Grenze hinausschiesst -> muss trotz Hold-off senden
    cmd = ctrl.update(5000, now=5.0)
    assert cmd is not None, "Harte Sicherheitsgrenze haette trotz Hold-off senden muessen"
    assert cmd["power"] == 800


def test_discharge_cap_keepalive_still_works_while_capped():
    """Auch waehrend der Sollwert dauerhaft am Deckel haengt, muss das
    Keepalive kurz vor Ablauf von cd_time weiterhin senden."""
    cfg = PassiveControllerConfig(deadzone_w=5, min_setpoint_change_w=5, max_step_w=2000,
                                   min_output_w=-1500, max_output_w=800, min_send_interval_s=0,
                                   default_cd_time_s=60, max_cd_time_s=3600)
    ctrl = PassiveController(cfg)
    ctrl.set_discharge_cap(200)

    ctrl.update(300, now=0.0)  # -> gesendet: 200W
    assert ctrl.update(275, now=10.0) is None  # weiterhin am Deckel, kein Senden

    # kurz vor Ablauf von cd_time (Schwelle bei 60-max(5,12)=48s) -> Keepalive muss senden
    cmd = ctrl.update(271, now=50.0)
    assert cmd is not None
    assert cmd["power"] == 200
