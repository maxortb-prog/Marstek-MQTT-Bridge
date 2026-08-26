import logging

from passive_controller import PassiveController, PassiveControllerConfig


def test_controllogic_logger_emits_debug_when_enabled(caplog):
    """Regressionstest: die 'errechnete Leistungsaenderung' wird auf dem
    dedizierten 'marstek.control_logic'-Logger mit category=CONTROLLOGIC
    geloggt, unabhaengig vom Logger 'marstek.passive_controller'."""
    ctrl_logger = logging.getLogger("marstek.control_logic")
    ctrl_logger.setLevel(logging.DEBUG)

    cfg = PassiveControllerConfig(deadzone_w=10, min_setpoint_change_w=10, max_step_w=2000,
                                   min_send_interval_s=0)
    ctrl = PassiveController(cfg)

    with caplog.at_level(logging.DEBUG, logger="marstek.control_logic"):
        ctrl.update(300, now=0.0)
        ctrl.update(500, now=1.0)

    messages = [r.message for r in caplog.records if r.name == "marstek.control_logic"]
    assert any("Leistungsaenderung" in m for m in messages)
    assert any("Initialwert" in m for m in messages)

    categories = [getattr(r, "category", None) for r in caplog.records if r.name == "marstek.control_logic"]
    assert all(c == "CONTROLLOGIC" for c in categories)


def test_controllogic_logger_silent_when_disabled(caplog):
    """Wird der Logger auf WARNING gesetzt (Standard, wenn die Option
    deaktiviert ist), duerfen keine Debug-Zeilen zur Leistungsaenderung
    auftauchen."""
    ctrl_logger = logging.getLogger("marstek.control_logic")
    ctrl_logger.setLevel(logging.WARNING)

    cfg = PassiveControllerConfig(deadzone_w=10, min_setpoint_change_w=10, max_step_w=2000,
                                   min_send_interval_s=0)
    ctrl = PassiveController(cfg)

    with caplog.at_level(logging.DEBUG):
        ctrl.update(300, now=0.0)
        ctrl.update(500, now=1.0)

    messages = [r.message for r in caplog.records if r.name == "marstek.control_logic"]
    assert not any("Leistungsaenderung" in m for m in messages)

    # Logger-Level zuruecksetzen, um andere Tests nicht zu beeinflussen
    ctrl_logger.setLevel(logging.NOTSET)
