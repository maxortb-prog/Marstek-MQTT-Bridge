"""
passive_controller.py

Traeger Regler-Filter fuer den Marstek Passive-Mode.

Aufgabe dieses Moduls:
    Nimmt einen roh berechneten Leistungs-Sollwert entgegen (z.B. aus einer
    Shelly-Messung abgeleitet, ca. alle 5s) und entscheidet OB und mit
    WELCHEM Wert tatsaechlich ein ES.SetMode(Passive)-Kommando an den
    Marstek gesendet werden soll.

Enthaelt bewusst KEINEN MQTT- oder UDP-Code - reine, deterministische und
gut testbare Regel-Logik. Die eigentliche Sende-Schicht (UDP-Client mit
Queue/Prioritaeten) ruft update() auf und verschickt das zurueckgegebene
dict (falls nicht None) an den Marstek.

Filterkette (in dieser Reihenfolge):
    1. Sicherheits-Clamp auf [min_output_w, max_output_w]
    2. Totzone         -> Rauschen um den aktuellen (internen) Sollwert ignorieren
    3. Slew-Rate       -> Sollwert naehert sich dem Ziel nur in kleinen Schritten
    4. Mindeständerung -> erst senden, wenn genug Differenz zum zuletzt
                          GESENDETEN Wert aufgelaufen ist
    5. Hold-off-Timer  -> zwischen zwei echten Sendungen liegt mindestens
                          min_send_interval_s, AUSSER die Sicherheitsgrenze
                          wurde getroffen (dann wird trotzdem gesendet und
                          eine Warnung mit Zeitstempel geloggt)

Alle Schwellwerte sind ueber PassiveControllerConfig konfigurierbar und
sollen im HA-Addon unter "Optionen -> Controller" einstellbar sein.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, TypedDict

logger = logging.getLogger("marstek.passive_controller")


class PassiveCommand(TypedDict):
    power: int
    cd_time: int


@dataclass
class PassiveControllerConfig:
    """Defaults entsprechen den in der Projektspezifikation genannten Werten."""

    # Optionen -> Controller
    deadzone_w: float = 40.0            # +/- 30-50 W, Rauschunterdrueckung
    min_setpoint_change_w: float = 50.0  # Mindeständerung ggue. letztem Sendewert
    max_step_w: float = 125.0           # max. Leistungsschritt pro Zyklus (100-150 W)
    min_output_w: float = -1500.0       # Ladegrenze (negativ = Laden aus dem Netz)
    max_output_w: float = 800.0         # Einspeisegrenze (positiv = Einspeisen)
    min_send_interval_s: float = 30.0   # 0-60s, 0 = keine Wartezeit zwischen Sendungen

    # Optionen -> Passiv Mode Settings (cd_time, das mit jedem Kommando mitgeht)
    default_cd_time_s: int = 60
    max_cd_time_s: int = 3600

    def validate(self) -> None:
        if not (0.0 <= self.min_send_interval_s <= 60.0):
            raise ValueError("min_send_interval_s muss zwischen 0 und 60 Sekunden liegen")
        if not (0 < self.default_cd_time_s <= self.max_cd_time_s):
            raise ValueError("default_cd_time_s muss zwischen 1 und max_cd_time_s liegen")
        if self.max_cd_time_s > 3600:
            raise ValueError("max_cd_time_s darf laut Geraetevorgabe 3600s nicht ueberschreiten")
        if self.min_output_w >= self.max_output_w:
            raise ValueError("min_output_w muss kleiner als max_output_w sein")
        if self.max_step_w <= 0:
            raise ValueError("max_step_w muss > 0 sein")
        if self.deadzone_w < 0 or self.min_setpoint_change_w < 0:
            raise ValueError("deadzone_w / min_setpoint_change_w duerfen nicht negativ sein")


@dataclass
class PassiveControllerState:
    committed_setpoint_w: Optional[float] = None   # interner, slew-limitierter Zielwert
    last_sent_setpoint_w: Optional[float] = None   # letzter tatsaechlich gesendeter Wert
    last_send_monotonic: Optional[float] = None    # time.monotonic() der letzten Sendung
    update_count: int = 0
    send_count: int = 0


class PassiveController:
    def __init__(self, config: PassiveControllerConfig, state: Optional[PassiveControllerState] = None):
        config.validate()
        self.config = config
        self.state = state or PassiveControllerState()

    def update(self, raw_target_w: float, *, now: Optional[float] = None) -> Optional[PassiveCommand]:
        """
        Bei jeder neuen Messung aufrufen (empfohlen: alle ~5s).

        raw_target_w: roh berechneter Soll-Leistungswert, NICHT vorbegrenzt.
                      (Vorzeichen: positiv = einspeisen, negativ = laden)
        now:          optionaler monotoner Zeitstempel, nur fuer Tests.
                      Default: time.monotonic().

        Returns:
            None                    -> nichts senden, Regler bleibt still.
            {"power": int, "cd_time": int} -> dieses Kommando an den
                                              Marstek (ES.SetMode Passive) senden.
        """
        now = now if now is not None else time.monotonic()
        cfg = self.config
        st = self.state
        st.update_count += 1

        # 1) Sicherheits-Clamp
        clamped = max(cfg.min_output_w, min(cfg.max_output_w, raw_target_w))
        hit_safety_limit = clamped != raw_target_w

        # Erstinitialisierung
        if st.committed_setpoint_w is None:
            st.committed_setpoint_w = clamped
            return self._maybe_send(clamped, now, hit_safety_limit, reason="initial")

        # 2) Totzone (bezogen auf den aktuellen internen Sollwert)
        deviation = clamped - st.committed_setpoint_w
        if abs(deviation) <= cfg.deadzone_w and not hit_safety_limit:
            logger.debug(
                "Totzone: |%.1f W| <= %.1f W -> ignoriert (Sollwert bleibt %.0f W)",
                deviation, cfg.deadzone_w, st.committed_setpoint_w,
            )
            return None

        # 3) Slew-Rate-Begrenzung
        step = max(-cfg.max_step_w, min(cfg.max_step_w, deviation))
        new_committed = st.committed_setpoint_w + step
        new_committed = max(cfg.min_output_w, min(cfg.max_output_w, new_committed))
        st.committed_setpoint_w = new_committed

        # 4) Mindeständerung ggue. dem zuletzt tatsaechlich GESENDETEN Wert
        if st.last_sent_setpoint_w is not None and not hit_safety_limit:
            change_vs_sent = abs(new_committed - st.last_sent_setpoint_w)
            if change_vs_sent < cfg.min_setpoint_change_w:
                logger.debug(
                    "Mindeständerung nicht erreicht: %.1f W < %.1f W -> kein Senden "
                    "(intern bei %.0f W)",
                    change_vs_sent, cfg.min_setpoint_change_w, new_committed,
                )
                return None

        return self._maybe_send(new_committed, now, hit_safety_limit, reason="update")

    def _maybe_send(
        self, power_w: float, now: float, hit_safety_limit: bool, reason: str
    ) -> Optional[PassiveCommand]:
        cfg = self.config
        st = self.state

        elapsed = None if st.last_send_monotonic is None else now - st.last_send_monotonic
        interval_satisfied = (
            cfg.min_send_interval_s == 0
            or st.last_send_monotonic is None
            or elapsed >= cfg.min_send_interval_s
        )

        if not interval_satisfied and not hit_safety_limit:
            logger.debug(
                "Hold-off aktiv: erst %.1fs seit letzter Sendung (Minimum %.1fs) -> warte",
                elapsed, cfg.min_send_interval_s,
            )
            return None

        if not interval_satisfied and hit_safety_limit:
            logger.warning(
                "%s | Sicherheitsgrenze erreicht - Kommando wird trotz Hold-off bereits "
                "nach %.1fs (Minimum %.1fs) gesendet. power=%.0fW",
                time.strftime("%Y-%m-%d %H:%M:%S"), elapsed, cfg.min_send_interval_s, power_w,
            )

        power_int = int(round(power_w))
        # power=0 vermeiden: laut Geraet fuehrt 0 zu max. Ladeleistung (unerwuenscht)
        if power_int == 0:
            power_int = 1 if power_w >= 0 else -1

        st.last_sent_setpoint_w = float(power_int)
        st.last_send_monotonic = now
        st.send_count += 1

        cd_time = min(cfg.default_cd_time_s, cfg.max_cd_time_s)

        logger.info(
            "SEND [%s] power=%dW cd_time=%ds (Update #%d, Sendung #%d)",
            reason, power_int, cd_time, st.update_count, st.send_count,
        )

        return {"power": power_int, "cd_time": cd_time}
