# Changelog

## 0.4.2

- **Bugfix: cd_time-Kontinuität nach Neustart unvollständig.** Die in 0.1.0
  eingeführte Sollwert-Kontinuität (Seeding von `ES.GetMode.ongrid_power`
  beim Start) aktualisierte bisher nur den INTERNEN Regler-Zustand, sendete
  aber nichts ans Geraet: die geraeteseitige `cd_time` lief unbeeinflusst
  weiter (moeglicherweise kurz vor Ablauf), und `sensor.passive_cd_time_remaining`
  blieb uninitialisiert. Laeuft das Geraet beim Neustart bereits im
  Passive-Mode, sendet die Bridge jetzt sofort ein `ES.SetMode`-Kommando
  mit dem geseedeten Wert erneut - setzt die geraeteseitige `cd_time`
  frisch und initialisiert den lokalen Countdown. Laeuft das Geraet in
  einem anderen Modus (z.B. Auto), wird bewusst NICHTS gesendet - kein
  erzwungener Moduswechsel.

## 0.4.1

- **Entfernt: Shelly-Eingangsentprellung** (`shelly_debounce_time_s`, `number.shelly_debounce_time_s`,
  Modul `input_averager.py`). Nicht mehr benötigt - die Entprellung erfolgt
  jetzt stattdessen auf HA-Automatisierungsebene (siehe DOCS.md fuer ein
  Beispiel mit zeitbasiertem Trigger statt `power.changed`).
- **Keepalive-Schwelle vereinfacht:** Der Passive-Regler sendet ein
  unveraendertes Keepalive-Kommando jetzt nach der Haelfte der
  geraeteseitigen `cd_time` (statt der bisherigen 20%-Marge-Logik kurz vor
  Ablauf). Deutlich konservativer/haeufiger, mehr Sicherheitsabstand gegen
  Verbindungsaussetzer.
- **Kritischer Fix: Idle-bei-niedrigem-SOC sendet jetzt kontinuierlich
  power~0 statt komplett zu schweigen.** Das bisherige Verhalten (gar
  nichts mehr senden, `cd_time` natuerlich ablaufen lassen) wurde als
  riskant gemeldet: dabei verliert das Geraet offenbar die
  Passive-Verbindung/den Modus komplett, statt sauber in einen definierten
  Zustand zu gehen. Ein neuer, von Shelly-Nachrichten unabhaengiger
  Hintergrund-Task sendet waehrend der Idle-Phase jetzt stattdessen
  kontinuierlich (alle `cd_time`/2) ein `Passive`-Kommando mit ~0W - der
  Modus/die Verbindung bleibt so aktiv erhalten, nur ohne Lade-/
  Entladeleistung.
- **`message_settings.max_retry=0` loest keinen Communication-Fail/Watchdog
  mehr aus.** Bisher fuehrte auch bei `max_retry=0` (nur 1 Versuch, keine
  Wiederholung) ein einzelner fehlender Response weiterhin zur vollen
  Eskalation (Communication-Fail-Status + Watchdog-Ausloeser) - das war
  inkonsistent mit der Erwartung, dass `0` bedeutet "nicht als
  Verbindungsproblem werten". Ein fehlgeschlagenes Kommando scheitert bei
  `max_retry=0` jetzt nur noch fuer sich selbst (Exception an den
  Aufrufer), ohne den globalen Verbindungsstatus zu beeinflussen. Betrifft
  ausschliesslich den laufenden Betrieb (`message_settings.max_retry`) -
  die Init-Sequenz eskaliert unabhaengig von `init_max_retries`
  weiterhin immer bei Ausschoepfung aller Versuche.

## 0.3.0

- **Neue Funktion: Idle bei niedrigem SOC.** Die automatische Passive-
  Regelung pausiert jetzt komplett, sobald der Batterie-SOC unter eine
  konfigurierbare Schwelle (`idle_soc_threshold`, Standard 5%) faellt: es
  wird kein Kommando mehr gesendet (weder Update noch Keepalive), die
  geraeteseitige `cd_time` laeuft dadurch natuerlich ab und das Geraet
  faellt in seinen eigenen Idle-/Sicherheitszustand zurueck. Die SOC-
  Quelle ist dabei bewusst flexibel: es wird immer der jeweils zuletzt
  aktualisierte Wert aus `Bat.GetStatus.soc` ODER `ES.GetStatus.bat_soc`
  verwendet, da beide unterschiedliche Poll-Intervalle haben koennen.
  Eine Hysterese (`idle_soc_resume_margin`, Standard 3%) verhindert
  schnelles Pausieren/Wiederanspringen direkt an der Schwelle - die
  Regelung startet erst wieder, wenn der SOC `idle_soc_threshold +
  idle_soc_resume_margin` erreicht. Neue HA-Entities:
  `number.idle_soc_threshold` (live aenderbar) und
  `binary_sensor.passive_idle_low_soc` (Statusanzeige, eignet sich fuer
  Benachrichtigungs-Automatisierungen).

## 0.2.3

- **Bugfix: Init-Sequenz faelschlich vom Laufzeit-Pacing-Gate ausgebremst.**
  Init-Kommandos liefen intern unter derselben Kategorie wie normale
  Laufzeit-Status-Abfragen und wurden dadurch zusaetzlich zur eigenen
  Init-Pause (`init.inter_command_delay_s`) auch noch vom
  `message_settings.min_inter_message_delay_s`-Pacing-Gate ausgebremst -
  der jeweils groessere Wert gewann unbeabsichtigt. In der Praxis
  beobachtet und gemeldet: bei `inter_command_delay_s=2.0` und
  `min_inter_message_delay_s=5.0` betrug der tatsaechliche Abstand
  zwischen Init-Kommandos durchgaengig 5s statt der konfigurierten 2s.
  Init-Kommandos umgehen das Laufzeit-Pacing-Gate jetzt vollstaendig -
  nur die eigene, dedizierte Init-Pause gilt noch. Laufzeit-Pacing und
  Control-Preemption bleiben fuer den normalen Poll-/Control-Betrieb
  unveraendert.

## 0.2.2

- **Config-Umgliederung + neue Default-Werte** in `config.yaml`: Feldreihenfolge
  in "Scanrate for Statuscalls" angepasst (bat_status_interval_s,
  es_status_interval_s, pv_enabled, pv_status_interval_s, es_mode_enabled,
  es_mode_interval_s), sowie die vorbelegten Default-Werte fuer neue
  Installationen ueberarbeitet (u.a. schnellere Poll-/Regel-Intervalle,
  engere Totzone). Betrifft nur die Vorbelegung in der Add-on-UI - bestehende
  Installationen mit bereits gesetzten Werten sind davon nicht betroffen.
  Reine Konfigurationsaenderung, keine Code-/Verhaltensaenderung.

## 0.2.1

- **Bugfix: Docker-Build schlug fehl** (`ghcr.io/home-assistant/base:3.19: not found`).
  Das in 0.2.0 gesetzte Tag `3.19` existiert fuer das neue, vereinheitlichte
  Multi-Arch-Base-Image nicht mehr - die aktuellen Images werden nach
  Alpine-Version getaggt (aktuell unterstuetzt: 3.22/3.23/3.24). Auf
  Empfehlung der offiziellen HA-Entwickler-Doku auf `ghcr.io/home-assistant/base:latest`
  umgestellt.

## 0.2.0

- **Neue Option `es_mode_enabled`** (Gruppe "Scanrate for Statuscalls",
  Standard: an): schaltet nur das periodische `ES.GetMode`-Polling ein/aus.
  Die Init-Sequenz fragt `ES.GetMode` weiterhin immer genau einmal ab,
  unabhaengig von dieser Option.
- **`build.yaml`-Migration** (Reaktion auf Supervisor-Deprecation-Warnung):
  `build.yaml` entfernt, Inhalt direkt ins `Dockerfile` verschoben (`FROM
  ghcr.io/home-assistant/base:3.19` statt Multi-Arch-`build_from`-Mapping,
  `LABEL`-Anweisungen statt separater `labels`-Sektion). `arch:`-Liste in
  `config.yaml` bewusst unveraendert gelassen (32-Bit-Architekturen bleiben
  vorerst unterstuetzt). Die verwandte `addon_config`→`app_config`-
  Map-Type-Warnung wurde bewusst NICHT geaendert, da die aktuelle offizielle
  HA-Doku `addon_config` weiterhin als gueltig dokumentiert und kein
  `app_config`-Ersatzwert dokumentiert ist (vermutlich Teil eines noch
  offenen HA-internen Migrations-Issues).
- **PV.GetStatus erstmals implementiert.** Neue Geraete-Gruppe "Marstek PV"
  mit 4 Kanaelen (pv1-pv4: Power/Voltage/Current als Sensor, State als
  binary_sensor). Neue Optionen `pv_enabled` (Standard: aus, da laut API-Doku
  nur Venus D/Venus A unterstuetzt werden, Venus C/E nicht) und
  `pv_status_interval_s`. Im Unterschied zu `es_mode_enabled` betrifft
  `pv_enabled` SOWOHL die Init-Sequenz ALS AUCH das periodische Polling -
  bei deaktiviertem PV wird `PV.GetStatus` nirgends aufgerufen. Zusaetzlich:
  wird PV deaktiviert, nachdem es zuvor aktiv war, werden die betroffenen
  Entities aktiv aus Home Assistant entfernt (leere retained
  Discovery-Nachricht) statt nur verwaist stehen zu bleiben. Neue Methode
  `HAMqttBridge.remove_entity_discovery()` fuer diesen Zweck.
- **Bugfix: Sollwert-Kontinuitaet des Passive-Reglers ueber einen Neustart
  hinweg.** Bisher startete der interne Sollwert des automatischen Reglers
  nach jedem Add-on-Neustart faelschlich bei 0W, obwohl das Geraet
  tatsaechlich bereits auf einem ganz anderen Wert lief (in der Praxis
  beobachtet und gemeldet: 261W). Das fuehrte beim ersten automatischen
  Update nach dem Neustart zu einem grossen, ungewollten Sprung. Die Bridge
  uebernimmt jetzt den waehrend der Init-Sequenz ohnehin bereits
  abgefragten `ES.GetMode.ongrid_power`-Wert als Startpunkt fuer den
  Regler-Sollwert.
- **Bugfix: SOC-Deckel loeste unnoetige Dauer-Sendungen aus.** Haengt der
  Sollwert dauerhaft am dynamischen Entlade-Deckel (`passive_power`, z.B.
  aktiv per SOC-Automatisierung reduziert), wurde bisher bei JEDEM Zyklus
  ein sofortiges Senden erzwungen (unter Umgehung von Totzone und
  Hold-off) - selbst wenn sich am tatsaechlich gesendeten Wert gar nichts
  aenderte. Ursache: der Deckel wurde intern wie eine echte, seltene
  Sicherheitsgrenze behandelt. In der Praxis beobachtet und gemeldet:
  identische 200W-Kommandos alle ~12-15s ueber Stunden hinweg. Der
  dynamische Deckel loest jetzt kein erzwungenes Sofort-Senden mehr aus -
  Totzone/Hold-off greifen normal, das Keepalive sendet weiterhin kurz vor
  Ablauf von `cd_time`. Die harten Grenzen (`controller_min_output_w`/
  `controller_max_output_w`) loesen weiterhin sofortiges Senden aus
  (echtes Sicherheitsverhalten bleibt unveraendert).

## 0.1.0

- **Kritischer Regelungsfix**: Die Berechnung des Zielwerts fuer den
  automatischen Passive-Regler war fehlerhaft und konnte bei einer
  Messstelle, die den TATSAECHLICHEN Netzbezug inkl. der Batterieleistung
  misst, zu einer **Mitkopplung (Aufschaukeln statt Stabilisierung)**
  fuehren: Laden erhoehte den gemessenen Netzbezug, was faelschlich als
  "noch mehr laden noetig" interpretiert wurde. Die direkte Zielwertvorgabe
  (`target = -Messwert`) wurde durch eine **integrierende Regelung**
  ersetzt (`neuer Sollwert = aktueller Sollwert + gemessener Netzbezug`),
  die bei konstanter Last in einem Schritt korrekt konvergiert statt zu
  eskalieren. In der Praxis beobachtet und gemeldet durch einen Nutzer, der
  ein Aufschaukeln bei realer Shelly-Anbindung feststellte.
- DOCS.md um eine explizite Anforderung an die Messstelle ergaenzt: der
  konfigurierte Topic muss den Netzbezug INKLUSIVE der aktuellen
  Batterieleistung liefern (z.B. eine Messung am Netzanschlusspunkt), nicht
  eine reine Haushaltslast ohne Batteriebeitrag.
- Neuer Regressionstest, der genau das beobachtete Szenario nachstellt
  (konstante Last, Regelung muss sich stabilisieren statt zu eskalieren).

## 0.0.12

- **Neuer Button `button.passive_resend`** ("Resend Passive Command"):
  sendet das aktuelle Passive-Kommando erneut, ohne den Modus wechseln zu
  muessen. Loest das Problem, dass HA's `select`-Entity beim erneuten
  Klicken auf den bereits aktiven Wert oft keine neue MQTT-Nachricht
  ausloest. Nutzt denselben Codepfad wie das (Neu-)Auswaehlen von "Passive"
  (Countdown-Reset, Entprellungs-Reset, automatische ControlLogic-Debug-
  Aktivierung).
- `mqtt_ha.py` um Unterstuetzung fuer die HA-Komponente `button` erweitert
  (zustandslose Aktions-Entity, kein `state_topic` im Discovery-Schema,
  `payload_press`).
- **Bugfix**: Der Countdown ("Passive Countdown Remaining") wurde beim
  manuellen Wechsel in den Passive-Mode bisher optimistisch VOR dem
  eigentlichen Senden zurueckgesetzt, auch wenn das Kommando das Geraet nie
  erreicht hat. Wird jetzt korrekt erst nach bestaetigtem `set_result`
  zurueckgesetzt (analog zum automatischen Shelly-Pfad).

## 0.0.11

- **Automatische ControlLogic-Debug-Aktivierung beim Wechsel in Passive**:
  Unabhaengig von der Config-Option `debug_control_logic` schaltet die
  Bridge den ControlLogic-Logger jetzt automatisch auf DEBUG, sobald
  manuell ueber `select.energy_mode` in den Passive-Mode gewechselt wird -
  inkl. sofortiger Diagnosemeldung, ob ueberhaupt ein `shelly_power_topic`
  konfiguriert ist. Damit laesst sich direkt pruefen, ob der Shelly-Eingang
  ankommt, ohne vorher die Konfiguration zu aendern und neu zu starten.
  Beim Verlassen von Passive kehrt der Logger zum konfigurierten
  Grundzustand zurueck.

## 0.0.10

- **Neuer Debug-Kanal "Debugging - ControlLogic"** (`logging_settings.debug_control_logic`,
  Standard: aus), eigene Farbe (grün), separat vom allgemeinen `log_level`
  schaltbar. Loggt: Shelly-Eingang (roh), Shelly-Eingang (entprellt, inkl.
  Fenstergroesse/Sample-Anzahl), errechnete Leistungsaenderung pro Zyklus.
  Bewusst getrennt vom allgemeinen Log-Level, da bei haeufigen Shelly-
  Updates hier sehr viele Zeilen anfallen koennen.

## 0.0.9

- **cd_time-Keepalive**: Der Passive-Regler sendet jetzt automatisch ein
  Kommando mit unveraendertem Sollwert kurz bevor die geraeteseitige
  `cd_time` ablaeuft (Schwelle: 20% Marge, mind. 5s), selbst wenn Totzone/
  Mindeständerung sonst kein Update verlangen wuerden. Ohne dieses
  Verhalten haette das Geraet nach Ablauf von `cd_time` ohne neues
  Kommando seinen Passive-Sollwert verloren. Bypasst dafuer auch den
  Hold-off-Timer (`min_send_interval_s`).
- **Eingangsentprellung fuer `shelly_power_topic`** (neues Modul
  `input_averager.py`): der rohe externe Leistungsmesswert wird jetzt vor
  der Regellogik ueber ein konfigurierbares Zeitfenster gemittelt
  (`shelly_debounce_time_s`, Standard 10s, 0 = aus). Neue Option in der
  Gruppe "Selfconsumption Control Parameters for Passive Mode", neue
  HA-Entity `number.shelly_debounce_time_s` (live aenderbar), wird beim
  manuellen (Re-)Aktivieren des Passive-Mode automatisch zurueckgesetzt.
- **Neuer konfigurierbarer Mindestabstand zwischen Nachrichten**
  (`message_settings.min_inter_message_delay_s`, Standard 2.0s): verhindert,
  dass mehrere unabhaengige Status-Poll-Loops (Bat.GetStatus, ES.GetMode,
  ES.GetStatus) zufaellig praktisch gleichzeitig beim Geraet ankommen
  (in der Praxis beobachtet: 1ms Abstand zwischen aufeinanderfolgenden
  Abfragen). **Control-Kommandos werden davon nie aufgehalten** - sie
  duerfen sich laut Test jederzeit sofort dazwischen einreihen, auch
  waehrend eine Status-Abfrage auf den Mindestabstand wartet.
- **Korrektur einer Fehleinschaetzung**: Ein per Screenshot bestaetigter
  Test zeigt, dass ANSI-Farbcodierung im HA-Add-on-Log-Tab tatsaechlich
  funktioniert. Die vorherige Annahme, ANSI wuerde herausgefiltert,
  basierte auf per Copy-Paste kopiertem Logtext - Copy-Paste aus einer
  farbig gerenderten Weboberflaeche kann grundsaetzlich nie Farbe
  mitkopieren. Die zwischenzeitlich entfernte ANSI-Faerbung ist wieder
  aktiviert.

## 0.0.8

- ANSI-Farbcodes von "hellen" SGR-Codes (90-97) auf **Standard-SGR-Codes
  (30-37)** umgestellt, da manche ANSI->HTML-Konverter (u.a. offenbar
  Teile des HA-Add-on-Log-Viewers) die erweiterten "bright"-Codes nicht
  erkennen und dadurch weder Farbe noch lesbaren Text anzeigen.
  **Hinweis:** Ob der Add-on-Log-Tab in deiner HA-Version ANSI-Codes
  tatsaechlich in Farbe umwandelt oder als rohe Escape-Sequenzen anzeigt,
  konnte ohne echten Supervisor nicht abschliessend verifiziert werden -
  bitte nach dem Update pruefen. Falls weiterhin keine Farbe zu sehen ist,
  sind die reinen Text-Tags `[STATUS]`/`[CONTROL]`/`[INIT]`/`[COMM]` in
  jeder Log-Zeile bereits unabhaengig von ANSI-Unterstuetzung vorhanden.

## 0.0.7

- `log_level` in eine eigene, benannte Gruppe **"Logging"** verschoben -
  gleiche aufklappbare Darstellung wie alle anderen Abschnitte, statt als
  einzelnes, ungruppiertes Feld am Ende.
- `main.py` entsprechend angepasst (liest `log_level` jetzt aus der
  `logging_settings`-Gruppe).

## 0.0.6

- Letzte verbleibende ungruppierte Basis-Felder (`device_ip`,
  `device_udp_port`, `device_ble_mac`, `device_type`,
  `mqtt_discovery_prefix`, `mqtt_base_topic`, `mqtt_suggested_area`) in
  eine echte, benannte Gruppe **"Marstek Device"** verschoben - gleiche
  Darstellung wie alle anderen Abschnitte. Damit sind jetzt saemtliche
  Optionen ausser `log_level` und `mqtt_port` gruppiert.
- `addon_options.py` entsprechend angepasst (liest Geraete-/Verbindungswerte
  jetzt aus der `marstek_device`-Gruppe).

## 0.0.5

- `log_level` an das Ende der Konfiguration verschoben (war vorher unter
  "Allgemein" ganz oben).
- MQTT-Override-Felder (`mqtt_host`, `mqtt_username`, `mqtt_password`) aus
  dem generischen "Nicht verwendete optionale Konfigurationsoptionen"-
  Bereich in eine echte, benannte Gruppe **"MQTT Settings"** verschoben
  (gleiche Darstellung wie die anderen Abschnitte).
  `mqtt_port` bleibt bewusst ausserhalb dieser Gruppe ohne Default-Wert,
  da ein vorbelegter Port die "leer lassen = automatische Erkennung via
  Mosquitto-Service-Discovery"-Logik verhindern wuerde. Dadurch kann sich
  `mqtt_port` in der UI ggf. weiterhin anders verhalten als die drei
  anderen Felder der Gruppe - ohne echten Supervisor nicht verifizierbar.
- `addon_options.py` entsprechend angepasst (liest `mqtt_host`/
  `mqtt_username`/`mqtt_password` jetzt aus der `mqtt_settings`-Gruppe,
  `mqtt_port` weiterhin top-level).

## 0.0.4

- **Strukturelle Änderung**: Add-on-Optionen von flachen, prefix-benannten
  Schlüsseln (`message_settings_max_retry`, `controller_deadzone_w`, ...)
  auf echte verschachtelte Gruppen umgestellt (`message_settings.max_retry`,
  `selfconsumption_control.deadzone_w`, ...). Seit HA Supervisor 2025.10
  werden diese Gruppen als eigene Unterformulare mit Titel in der
  Add-on-Konfigurations-UI angezeigt: "Scanrate for Statuscalls", "Passiv
  Mode Settings", "Selfconsumption Control Parameters for Passive Mode",
  "Message Settings", "Init Settings", "DOD (Depth of Discharge)",
  "Additional Settings".
- `addon_options.py` entsprechend umgebaut (liest jetzt verschachtelte
  Gruppen statt flacher Praefix-Schluessel), inkl. Fallback auf Defaults
  falls eine ganze Gruppe im `options.json` fehlt.
- `translations/en.yaml` um Gruppennamen/-beschreibungen sowie
  Feldbeschreibungen fuer alle Optionen ergaenzt.
- Voraussetzung ergaenzt: HA Supervisor >= 2025.10 fuer korrekte Anzeige
  der gruppierten Abschnitte.

## 0.0.3

- Gliedernde Überschriften in `config.yaml` ergänzt (sowohl im `options:`-
  als auch im `schema:`-Block), damit die Struktur beim Lesen der Datei
  leichter erkennbar ist: "Scanrate for Statuscalls", "Passiv Mode
  Settings", "Selfconsumption Control Parameters for Passive Mode",
  "Message Settings", "Init Settings", "DOD (Depth of Discharge)",
  "Additional Settings". Rein kosmetisch (YAML-Kommentare) - keine
  funktionale Änderung, HA zeigt diese Überschriften nicht in der
  Add-on-Konfigurations-UI an, nur beim Lesen der Rohdatei.

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
