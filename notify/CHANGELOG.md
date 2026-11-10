# Changelog

## 1.2.0
- Drei Stufen statt gelesen/ungelesen: **neu → gesehen → quittiert**
- Quittieren mit Name, Uhrzeit und optionalem Kommentar; wieder öffnen, Rückgängig
- Automatisch „gesehen“, sobald eine Karte in der Weboberfläche sichtbar ist (abschaltbar)
- `ack_min_level`: unterhalb dieser Stufe reicht „gesehen“ (Standard: Warnung)
- Wiederholte Meldungen (gleicher `key`) werden wieder „neu“
- Handy: Buttons „Quittieren“, „Gesehen“, „Mit Kommentar“; Benachrichtigung
  verschwindet nach dem Quittieren
- HA: Event `notify_seen` (eingehend), `notify_acknowledged` (ausgehend),
  `binary_sensor.notify_unseen`, Zähler `new`/`seen`/`acked`
- Benutzer wird erkannt (Ingress, `person.*`)
- Status-Filter, Suche auch in Kommentaren
- Bestehende Datenbanken werden übernommen: bisher „gelesen“ = „quittiert“

## 1.1.0
- Kategorien (frei definierbar, Standard: intern, extern, web, haushalt, system)
  mit Farbe, Icon, eigenem Sensor `sensor.notify_<kategorie>` und eigener Weiterleitung
- Web-Monitor: HTTP(S)- und TCP-Prüfungen, Ausfall- und Erholungsmeldungen,
  `binary_sensor.notify_monitor_<name>`
- Sortierung: neueste, nach Schwere, Kategorie, Quelle, älteste offene (gruppiert)
- Filter nach Tags, Suche auch in Zusatzdaten
- Quittieren/Löschen per Kategorie (REST und `notify_ack`)
- `binary_sensor.notify_critical` für bedingte Karten
- Weboberfläche: Kategorie-/Tag-Chips, Monitor-Leiste, einklappbare Filter am Handy

## 1.0.0
- Erste Version: Ereignisse per HA-Event, REST und GET, SQLite-Speicher,
  Sensoren, Quittieren per Event, Weiterleitung, Weboberfläche (Ingress)
