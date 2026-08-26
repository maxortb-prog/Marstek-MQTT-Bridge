"""
input_averager.py

Gleitender Mittelwert (Moving Average) zur Entprellung eines rohen,
extern gemessenen Leistungssignals (z.B. von einem Shelly), BEVOR es in
die eigentliche Regellogik (passive_controller.py) eingespeist wird.

Bewusst getrennt vom Passive-Regler: der Regler selbst hat schon eine
eigene Traegheit (Totzone, Slew-Rate, Mindeständerung) fuer den
SOLLWERT, aber keine Mittelung des ROHEN MESSWERTS. Ohne Entprellung
wuerde ein einzelner Ausreisser im Eingangssignal direkt (wenn auch
gedaempft) in die Regelung einfliessen. Dieses Modul mittelt zuerst
ueber ein Zeitfenster, dessen Laenge zur Laufzeit aenderbar ist (z.B.
per HA-Number-Entity).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional, Tuple


class InputAverager:
    def __init__(self, window_s: float = 0.0):
        """window_s: Laenge des Mittelungsfensters in Sekunden.
        0 = keine Mittelung (jeder neue Wert wird unveraendert durchgereicht)."""
        self._window_s = max(0.0, window_s)
        self._samples: Deque[Tuple[float, float]] = deque()  # (timestamp, value)

    @property
    def window_s(self) -> float:
        return self._window_s

    def set_window(self, window_s: float) -> None:
        """Aendert die Fensterlaenge zur Laufzeit (z.B. per HA-Number-Entity
        oder beim erneuten Wechsel in den Passive-Mode). Bereits gesammelte
        Samples bleiben erhalten, werden aber beim naechsten add() sofort
        gegen das neue Fenster gefiltert."""
        self._window_s = max(0.0, window_s)

    def reset(self) -> None:
        """Verwirft alle bisherigen Samples (z.B. beim Wechsel in den
        Passive-Mode, um mit einer frischen Mittelung zu starten)."""
        self._samples.clear()

    def add(self, value: float, *, now: Optional[float] = None) -> float:
        """Fuegt einen neuen Rohwert hinzu und gibt den aktuellen
        (entprellten) Mittelwert ueber das konfigurierte Zeitfenster zurueck.
        Bei window_s == 0 wird der Rohwert unveraendert zurueckgegeben."""
        if self._window_s <= 0:
            return value

        now = now if now is not None else time.monotonic()
        self._samples.append((now, value))

        cutoff = now - self._window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if not self._samples:
            return value
        return sum(v for _, v in self._samples) / len(self._samples)

    def sample_count(self) -> int:
        return len(self._samples)
