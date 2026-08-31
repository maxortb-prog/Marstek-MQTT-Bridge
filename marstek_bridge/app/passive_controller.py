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
    1. Sicherheits-Clamp auf [min_output_w, effektive_max_output_w]
       (effektive_max_output_w = min(max_output_w, dynamischer Entlade-Deckel,
       siehe set_discharge_cap() - typischerweise SOC-abhaengig aus HA gesetzt)
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

Der dynamische Entlade-Deckel (set_discharge_cap) und die cd_time
(set_cd_time) sind bewusst NICHT Teil der Config, sondern zur Laufzeit
aenderbar: sie sollen z.B. per HA-Number-Entity live verstellbar sein (etwa
SOC-abhaengig, damit der Akku bei niedrigem Ladezustand nicht mit voller
Leistung weiter entladen wird), ohne dass ein Config-Reload/Neustart noetig
waere. config.max_output_w bleibt dabei immer die harte, aeussere
Sicherheitsgrenze - der Deckel kann sie nur verschaerfen, nie aufheben.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, TypedDict

logger = logging.getLogger("marstek.passive_controller")

# Eigener, separat vom allgemeinen log_level schaltbarer Logger fuer
# Regler-internes Debugging (siehe DOCS.md "Debugging - ControlLogic").
# Erwartet hohe Frequenz -> bewusst getrennt, damit er nicht automatisch
# mit aktiviert wird, nur weil log_level=debug gesetzt ist.
ctrl_logger = logging.getLogger("marstek.control_logic")


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

    # Proportionaler Schritt: schritt = clamp(abweichung * step_gain, -max_step_w, max_step_w).
    # Default 1.0 = rueckwaertskompatibel (identisch zum alten festen Schrittbegrenzer:
    # Abweichungen unterhalb max_step_w werden in EINEM Schritt voll uebernommen).
    # Werte < 1.0 daempfen kleine Abweichungen zusaetzlich, waehrend grosse
    # Abweichungen weiterhin bis max_step_w/Zyklus konvergieren (kein Trade-off
    # zwischen "schnell bei grossen Spruengen" und "ruhig bei kleinem Rauschen"
    # mehr noetig).
    step_gain: float = 1.0

    # Nulldurchgangs-Hysterese: verhindert haeufiges Hin- und Herschalten
    # zwischen Laden und Entladen bei Lasten, die knapp um den Nullpunkt
    # pendeln. Ein Vorzeichenwechsel des Sollwerts wird nur zugelassen, wenn
    # der neue Zielwert diese Schwelle (in Watt, jenseits der Null) erreicht -
    # sonst wird der Zielwert auf 0 "eingefangen" (dort greift wie ueberall
    # der bestehende Slew-Rate-Mechanismus normal weiter). Default 0.0 = aus
    # (jeder Vorzeichenwechsel wird sofort zugelassen, altes Verhalten).
    zero_crossing_hysteresis_w: float = 0.0

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
        if not (0.0 < self.step_gain <= 1.0):
            raise ValueError("step_gain muss zwischen > 0 und <= 1 liegen")
        if self.zero_crossing_hysteresis_w < 0:
            raise ValueError("zero_crossing_hysteresis_w darf nicht negativ sein")
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
        # Zusaetzlicher, zur Laufzeit aenderbarer Deckel fuer die maximale
        # Entlade-/Einspeiseleistung (z.B. SOC-abhaengig aus HA gesteuert).
        # None = kein Zusatz-Deckel, es gilt nur config.max_output_w.
        # Kann die konfigurierte Obergrenze nur VERSCHAERFEN, nie aufheben.
        self._discharge_cap_w: Optional[float] = None
        # Zur Laufzeit aenderbare cd_time (z.B. aus einer HA-Number-Entity).
        # None = es gilt config.default_cd_time_s.
        self._cd_time_override_s: Optional[int] = None

    def set_discharge_cap(self, cap_w: Optional[float]) -> None:
        """Setzt/aendert den dynamischen Entlade-Deckel. Wird typischerweise
        von einer HA-Automatisierung aufgerufen (z.B. SOC-abhaengig), damit
        der Akku bei niedrigem SOC nicht mit voller Leistung weiter entladen
        wird. None setzt den Deckel zurueck (nur noch max_output_w gilt)."""
        self._discharge_cap_w = cap_w

    def set_cd_time(self, cd_time_s: Optional[int]) -> None:
        """Setzt/aendert die cd_time, die mit jedem Passive-Kommando
        mitgesendet wird. None setzt auf config.default_cd_time_s zurueck."""
        self._cd_time_override_s = cd_time_s

    def _effective_max_output_w(self) -> float:
        if self._discharge_cap_w is None:
            return self.config.max_output_w
        return min(self.config.max_output_w, self._discharge_cap_w)

    def _effective_cd_time_s(self) -> int:
        raw = self._cd_time_override_s if self._cd_time_override_s is not None else self.config.default_cd_time_s
        return min(int(raw), self.config.max_cd_time_s)

    def _is_keepalive_due(self, now: float) -> bool:
        """True, wenn seit der letzten Sendung bereits die Haelfte der
        geraeteseitigen cd_time verstrichen ist - dann wird erneut gesendet,
        OBWOHL Totzone/Mindeständerung eigentlich kein Update verlangen
        wuerden. Sonst wuerde das Geraet nach Ablauf von cd_time keinen
        aktiven Passive-Sollwert mehr haben (siehe API-Doku: cd_time ist
        ein Countdown, nach dessen Ablauf ohne neues Kommando das Geraet
        den Passive-Sollwert nicht mehr haelt)."""
        if self.state.last_send_monotonic is None:
            return False
        cd_time = self._effective_cd_time_s()
        return (now - self.state.last_send_monotonic) >= (cd_time / 2)

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
        effective_max = self._effective_max_output_w()

        # 1) Sicherheits-Clamp (inkl. dynamischem Entlade-Deckel)
        clamped = max(cfg.min_output_w, min(effective_max, raw_target_w))

        # hit_safety_limit bezieht sich BEWUSST NUR auf die harten,
        # konfigurierten Grenzen (min_output_w/max_output_w) - NICHT auf den
        # dynamischen Entlade-Deckel (siehe set_discharge_cap). Der Deckel
        # begrenzt den gesendeten Wert zwar genauso, loest aber KEIN
        # erzwungenes Sofort-Senden (unter Umgehung von Totzone/Hold-off)
        # aus. Sonst wuerde ein aktiv reduzierter Deckel (z.B. SOC-abhaengig)
        # bei jedem Zyklus faelschlich als Sicherheitsereignis behandelt und
        # das Geraet unnoetig mit identischen Werten bombardieren, obwohl
        # sich am tatsaechlich gesendeten Wert gar nichts aendert. Ein
        # dauerhaft am Deckel haengender Sollwert faellt stattdessen normal
        # in Totzone/Hold-off und wird nur noch ueber das Keepalive kurz vor
        # Ablauf von cd_time erneut gesendet.
        hard_clamped = max(cfg.min_output_w, min(cfg.max_output_w, raw_target_w))
        hit_safety_limit = hard_clamped != raw_target_w
        keepalive_due = self._is_keepalive_due(now)

        # Erstinitialisierung
        if st.committed_setpoint_w is None:
            ctrl_logger.debug(
                "Errechnete Leistungsaenderung: Initialwert %.1f W (roh=%.1f W)",
                clamped, raw_target_w, extra={"category": "CONTROLLOGIC"},
            )
            st.committed_setpoint_w = clamped
            return self._maybe_send(clamped, now, force=True, reason="initial")

        # 1b) Nulldurchgangs-Hysterese: verhindert haeufiges Umschalten
        # zwischen Laden und Entladen bei Lasten, die knapp um den Nullpunkt
        # pendeln. Ein Vorzeichenwechsel wird nur zugelassen, wenn der neue
        # Zielwert die konfigurierte Schwelle jenseits der Null erreicht -
        # sonst wird der Zielwert auf 0 "eingefangen" und faellt danach ganz
        # normal in Totzone/Slew-Rate/Mindeständerung wie jeder andere Wert.
        if cfg.zero_crossing_hysteresis_w > 0:
            committed = st.committed_setpoint_w
            crosses_to_charge = committed > 0 and clamped < 0
            crosses_to_discharge = committed < 0 and clamped > 0
            if crosses_to_charge and clamped > -cfg.zero_crossing_hysteresis_w:
                ctrl_logger.debug(
                    "Nulldurchgangs-Hysterese: Ziel %.1f W noch nicht jenseits -%.1f W -> "
                    "auf 0 W eingefangen (bleibt bei Entladen-Richtung)",
                    clamped, cfg.zero_crossing_hysteresis_w, extra={"category": "CONTROLLOGIC"},
                )
                clamped = 0.0
            elif crosses_to_discharge and clamped < cfg.zero_crossing_hysteresis_w:
                ctrl_logger.debug(
                    "Nulldurchgangs-Hysterese: Ziel %.1f W noch nicht jenseits +%.1f W -> "
                    "auf 0 W eingefangen (bleibt bei Laden-Richtung)",
                    clamped, cfg.zero_crossing_hysteresis_w, extra={"category": "CONTROLLOGIC"},
                )
                clamped = 0.0

        # 2) Totzone (bezogen auf den aktuellen internen Sollwert)
        deviation = clamped - st.committed_setpoint_w
        ctrl_logger.debug(
            "Errechnete Leistungsaenderung: %.1f W (aktueller Sollwert=%.1f W -> Ziel=%.1f W, roh=%.1f W)",
            deviation, st.committed_setpoint_w, clamped, raw_target_w,
            extra={"category": "CONTROLLOGIC"},
        )
        if abs(deviation) <= cfg.deadzone_w and not hit_safety_limit:
            if keepalive_due:
                logger.info(
                    "Keepalive: cd_time laeuft bald ab - Sollwert %.0fW unveraendert "
                    "erneut gesendet, um den Countdown zurueckzusetzen",
                    st.committed_setpoint_w,
                )
                return self._maybe_send(st.committed_setpoint_w, now, force=True, reason="keepalive")
            logger.debug(
                "Totzone: |%.1f W| <= %.1f W -> ignoriert (Sollwert bleibt %.0f W)",
                deviation, cfg.deadzone_w, st.committed_setpoint_w,
            )
            return None

        # 3) Proportionale Slew-Rate-Begrenzung: Schritt = Abweichung * step_gain,
        # gedeckelt auf max_step_w. Bei step_gain=1.0 (Default) identisch zum
        # alten festen Schrittbegrenzer (rueckwaertskompatibel). Werte < 1.0
        # daempfen kleine Abweichungen zusaetzlich, waehrend grosse
        # Abweichungen weiterhin mit bis zu max_step_w/Zyklus konvergieren.
        proportional_step = deviation * cfg.step_gain
        step = max(-cfg.max_step_w, min(cfg.max_step_w, proportional_step))
        new_committed = st.committed_setpoint_w + step
        new_committed = max(cfg.min_output_w, min(effective_max, new_committed))
        st.committed_setpoint_w = new_committed

        # 4) Mindeständerung ggue. dem zuletzt tatsaechlich GESENDETEN Wert
        if st.last_sent_setpoint_w is not None and not hit_safety_limit:
            change_vs_sent = abs(new_committed - st.last_sent_setpoint_w)
            if change_vs_sent < cfg.min_setpoint_change_w:
                if keepalive_due:
                    logger.info(
                        "Keepalive: cd_time laeuft bald ab - Sollwert %.0fW unveraendert "
                        "erneut gesendet, um den Countdown zurueckzusetzen",
                        new_committed,
                    )
                    return self._maybe_send(new_committed, now, force=True, reason="keepalive")
                logger.debug(
                    "Mindeständerung nicht erreicht: %.1f W < %.1f W -> kein Senden "
                    "(intern bei %.0f W)",
                    change_vs_sent, cfg.min_setpoint_change_w, new_committed,
                )
                return None

        return self._maybe_send(new_committed, now, force=(hit_safety_limit or keepalive_due), reason="update")

    def _maybe_send(
        self, power_w: float, now: float, force: bool, reason: str
    ) -> Optional[PassiveCommand]:
        cfg = self.config
        st = self.state

        elapsed = None if st.last_send_monotonic is None else now - st.last_send_monotonic
        interval_satisfied = (
            cfg.min_send_interval_s == 0
            or st.last_send_monotonic is None
            or elapsed >= cfg.min_send_interval_s
        )

        if not interval_satisfied and not force:
            logger.debug(
                "Hold-off aktiv: erst %.1fs seit letzter Sendung (Minimum %.1fs) -> warte",
                elapsed, cfg.min_send_interval_s,
            )
            return None

        if not interval_satisfied and force:
            logger.warning(
                "%s | [%s] trotz Hold-off bereits nach %.1fs (Minimum %.1fs) gesendet. power=%.0fW",
                time.strftime("%Y-%m-%d %H:%M:%S"), reason, elapsed, cfg.min_send_interval_s, power_w,
            )

        power_int = int(round(power_w))
        # power=0 vermeiden: laut Geraet fuehrt 0 zu max. Ladeleistung (unerwuenscht)
        if power_int == 0:
            power_int = 1 if power_w >= 0 else -1

        st.last_sent_setpoint_w = float(power_int)
        st.last_send_monotonic = now
        st.send_count += 1

        cd_time = self._effective_cd_time_s()

        logger.info(
            "SEND [%s] power=%dW cd_time=%ds (Update #%d, Sendung #%d)",
            reason, power_int, cd_time, st.update_count, st.send_count,
        )

        return {"power": power_int, "cd_time": cd_time}
