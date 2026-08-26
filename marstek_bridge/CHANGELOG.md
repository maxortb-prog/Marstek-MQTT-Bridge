# Changelog

## 0.0.2

- Erklaerende Kommentare zu `passive_power` in `config.yaml` ergaenzt
  (Options- und Schema-Block): deckelt die maximale Einspeise-/Entladeleistung
  des Reglers, selbst wenn rechnerisch mehr ermittelt wuerde; Laderichtung
  bleibt davon unberuehrt und wird weiterhin nur durch `controller_min_output_w`
  begrenzt.

## 0.0.1

- Erste Version: UDP-Client mit Prioritaets-Queue (Control vor Status),
  Init-Sequenz, HA-MQTT-Discovery fuer alle Marstek-Datenpunkte, traeger
  Passive-Mode-Regler mit optionaler externer Leistungsmessung (z.B. Shelly),
  Communication-Fail/System-Ready-Status fuer Automatisierungen.
- Fix: ungueltiges `watchdog: null` Feld aus config.yaml entfernt (verhinderte
  das Laden des Add-ons durch den Supervisor).
- icon.png/logo.png ergaenzt.
- Farbcodiertes Debug-Logging (Control/Status/Init/Verbindungsstatus) in
  main.py aktiviert.
- Erster Poll-Zyklus nach der Initialisierung wartet jetzt das volle
  konfigurierte Intervall ab, statt sofort erneut abzufragen.
- Default fuer `init_inter_command_delay_s` von 5s auf 10s erhoeht.
- `init_timeout_increment_s` Default von 5s auf 10s erhoeht.
- `passive_power` umgebaut: von einem statischen, wirkungslosen Startwert zu
  einem zur Laufzeit ueber HA (`number.passive_default_power`) aenderbaren
  Deckel fuer die maximale Entlade-/Einspeiseleistung des automatischen
  Passive-Mode-Reglers (z.B. fuer SOC-abhaengige Automatisierungen). Wirkt
  nur auf die Entlade-/Einspeiserichtung, nicht auf das Laden.
  Default von 50 auf 800 (= `controller_max_output_w`) geaendert, damit
  ohne aktive Automatisierung keine zusaetzliche Einschraenkung entsteht.
  `passive_cd_time` (`number.passive_cd_time`) ist aus demselben Grund jetzt
  ebenfalls tatsaechlich live wirksam (vorher nur kosmetisch).
- Config-Optionen neu gegliedert (Allgemein, Abtastrate, Passive-Mode,
  Regelungsparameter, Message Settings, Initphase, DOD, Sonstige, MQTT-
  Override) und `message_max_retry`/`message_timeout_s` in
  `message_settings_max_retry`/`message_settings_timeout_s` umbenannt.
