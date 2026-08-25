# Marstek Add-ons für Home Assistant

Dieses Repository enthält das **Marstek MQTT Bridge**-Add-on: eine Brücke
zwischen einem Marstek Energiespeicher (lokale UDP-API) und Home Assistant
per MQTT-Discovery, inklusive eines trägen Self-Consumption-Reglers für den
Passive-Mode.

## Installation

1. In Home Assistant: **Einstellungen -> Add-ons -> Add-on-Store -> ⋮ ->
   Repositories** -> diese Repository-URL hinzufügen:
   `https://github.com/maxortb-prog/Marstek-MQTT-Bridge`
2. "Marstek MQTT Bridge" installieren.
3. Konfiguration prüfen/anpassen (siehe [DOCS.md](marstek_bridge/DOCS.md)).
4. Add-on starten. Unter **Info** empfehlenswert: den Schalter **Watchdog**
   aktivieren, damit Home Assistant das Add-on bei einem internen
   Kommunikationsausfall automatisch neu startet.

## Enthaltene Add-ons

| Add-on | Beschreibung |
|---|---|
| [marstek_bridge](marstek_bridge/DOCS.md) | UDP-zu-MQTT Bruecke fuer Marstek Energiespeicher |

## Hinweis zum Docker-Build

Das `Dockerfile` in `marstek_bridge/` wurde **nicht** in dieser Umgebung
gebaut oder getestet (kein Zugriff auf `ghcr.io` in der Entwicklungs-Sandbox).
Die reine Python-Logik (Config-Mapping, UDP-Client, MQTT-Bridge, Passive-
Regler, Verdrahtung) ist umfassend gegen einen echten lokalen MQTT-Broker
und einen simulierten Marstek-UDP-Server getestet (siehe `app/test_*.py`),
aber der eigentliche Add-on-Build (Dockerfile, `run.sh` mit bashio,
Supervisor-Service-Discovery) sollte vor dem produktiven Einsatz einmal
gegen eine echte Home-Assistant-Supervisor-Instanz getestet werden.
