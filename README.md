# Notify – Home-Assistant-Add-on

Ereignisse entgegennehmen, aufbewahren und anzeigen – im Dashboard und in einer
eigenen Weboberfläche (Seitenleiste).

- Eingang über HA-Events (`notify_event`), Notify-Dienst (`rest`), REST-API oder einfaches GET
- Kategorien (intern, extern, Web-Störungen … frei definierbar) mit eigenem Sensor und eigener Weiterleitung
- Eingebauter Web-Monitor (HTTP/TCP) für eigene Dienste wie FlipperPass – meldet Ausfall und Erholung
- Speicherung in SQLite, automatische Aufbewahrung (Tage / Anzahl)
- Gleiche Meldungen werden über `key` zusammengefasst (×2, ×3 …)
- Sensoren fürs Dashboard: gesamt, je Kategorie, je Web-Monitor, letztes Ereignis, kritisch ja/nein
- Neu → gesehen → quittiert, mit Name, Uhrzeit und Kommentar – in der Weboberfläche, per HA-Event oder direkt aus der Handy-Benachrichtigung
- Weboberfläche: Suche, Filter nach Kategorie/Stufe/Quelle/Tags, 5 Sortierungen, Export, Hell/Dunkel

## Installation

1. Dieses Repository auf GitHub hochladen (z. B. `spliter90/ha-notify`).
2. In Home Assistant: *Einstellungen → Add-ons → Add-on Store → ⋮ → Repositories*
   → `https://github.com/spliter90/ha-notify` hinzufügen.
3. „Notify“ installieren, starten, „In Seitenleiste anzeigen“ aktivieren.

Alternativ lokal: Ordner `notify/` nach `/addons/notify` kopieren (Samba/SSH),
dann im Add-on Store „Nach Updates suchen“ → unter *Lokale Add-ons*.

Dokumentation: [notify/DOCS.md](notify/DOCS.md) · Beispiele: [examples/](examples/)

## Änderungen und Updates

1. Code ändern, in `notify/config.yaml` **und** `notify/app/main.py` die
   Version erhöhen (z. B. `1.2.1`), Eintrag in `notify/CHANGELOG.md`.
2. `git commit` und `git push`.
3. GitHub prüft automatisch (Reiter *Actions*, Workflow **CI**):
   Add-on-Konfiguration, Python-Code mit API-Kurztest und Docker-Build für
   amd64 und aarch64. Grüner Haken = sicher zu installieren.
4. In Home Assistant: *Add-on Store → ⋮ → Nach Updates suchen* → Update.

Ohne Versionssprung bietet Home Assistant kein Update an.
