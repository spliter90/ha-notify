# Notify

Sammelstelle für Ereignisse in Home Assistant. Notify nimmt Meldungen aus
Automationen, Skripten oder von beliebigen Geräten (HTTP) entgegen, ordnet sie
Kategorien zu, überwacht Webdienste, speichert alles dauerhaft (SQLite unter
`/data`), zeigt es als Sensoren im Dashboard an und bringt eine eigene
Weboberfläche in der Seitenleiste mit.

## Ordnung: Kategorie → Quelle → Tags

| Ebene | Feld | Wofür | Beispiel |
|---|---|---|---|
| 1 | `category` | Grobe Einteilung, in den Optionen definiert, mit Farbe, Icon, eigenem Sensor und eigener Weiterleitung | `intern`, `extern`, `web` |
| 2 | `source` | Wer meldet – frei, wird automatisch zum Filter | `FlipperPass`, `Heizung`, `NAS` |
| 3 | `tags` | Beliebige Stichworte, als Chips filterbar | `ssl`, `theta`, `backup` |
| – | `level` | Schwere: `debug`, `info`, `success`, `warning`, `error`, `critical` | |

In der Weboberfläche lässt sich nach allem filtern (mehrere Werte je Zeile =
„oder“, zwischen den Zeilen = „und“) und sortieren: **Neueste zuerst**,
**nach Schwere**, **nach Kategorie** bzw. **nach Quelle** (jeweils gruppiert) und
**älteste offene zuerst**. Die Suche durchsucht auch die Zusatzdaten (`data`).

## Gesehen und Quittieren

Jedes Ereignis durchläuft drei Stufen:

| Status | Bedeutung | Wie |
|---|---|---|
| **Neu** | Noch von niemandem wahrgenommen | – |
| **Gesehen** | Zur Kenntnis genommen, aber noch offen | automatisch, sobald die Karte 1,5 s sichtbar ist (abschaltbar im ⋮-Menü), beim Aufklappen, per Handy-Button „Gesehen“ oder `notify_seen` |
| **Quittiert** | Erledigt – mit Name, Uhrzeit und optionalem Kommentar | ✓-Button, Kommentarfeld in der aufgeklappten Karte, Handy-Button „Quittieren“ / „Mit Kommentar“, `notify_ack` |

- **Nicht alles muss quittiert werden:** Unterhalb von `ack_min_level`
  (Standard `warning`) gilt eine Meldung schon mit „gesehen“ als erledigt.
  „Waschmaschine fertig“ verschwindet also beim Ansehen, „Pelletvorrat
  niedrig“ bleibt offen, bis jemand quittiert. Mit `ack_min_level: debug`
  muss alles quittiert werden.
- **Wer?** In der Weboberfläche wird der angemeldete HA-Benutzer erkannt, am
  Handy und bei HA-Events der Benutzer bzw. die Person (`person.*`). Über die
  REST-API kann `by` mitgegeben werden.
- **Wiederholung:** Meldet sich ein Ereignis mit gleichem `key` erneut, zählt
  es hoch (×2) und wird wieder **neu** – auch wenn es schon gesehen war.
- **Rückgängig / wieder öffnen:** Nach dem Quittieren erscheint kurz
  „Rückgängig“; quittierte Karten haben einen ↺-Button.
- **Offen** heißt: neu oder gesehen, aber nicht quittiert. Darauf zählen
  `sensor.notify_unread` und die Kategorie-Sensoren.

### Am Handy

Wird an `notify.mobile_app_…` weitergeleitet (`forward_service`), bekommt die
Benachrichtigung drei Buttons: **Quittieren**, **Gesehen** und **Mit
Kommentar** (Texteingabe). Wird das Ereignis irgendwo quittiert – am Handy, in
der Weboberfläche, per Automation oder weil der Web-Monitor den Dienst wieder
erreicht –, verschwindet die Benachrichtigung automatisch vom Handy.
Abschaltbar mit `mobile_actions: false`.

## Ereignis-Felder

| Feld       | Pflicht | Beschreibung |
|------------|---------|--------------|
| `title`    | ja*     | Kurze Überschrift |
| `message`  | ja*     | Ausführlicher Text (mehrzeilig möglich) |
| `level`    | nein    | Standard `info` |
| `category` | nein    | Standard aus `default_category`. Unbekannte Kategorien werden trotzdem angenommen |
| `source`   | nein    | Herkunft |
| `key`      | nein    | Gleicher Key + noch offen → kein neuer Eintrag, sondern Zähler ×2, ×3 … (und wieder „neu“) |
| `tags`     | nein    | Liste oder kommagetrennter Text |
| `data`     | nein    | Beliebige Zusatzdaten (JSON), sichtbar unter „Details“, durchsuchbar |

\* mindestens eins von beiden. Unbekannte Felder landen automatisch in `data`.

## Kategorien

Standardmäßig vorhanden: `intern`, `extern`, `web` (Web-Störungen), `haushalt`,
`system`. In der Add-on-Konfiguration (YAML-Ansicht) frei änderbar:

```yaml
default_category: intern
categories:
  - name: web
    label: Web-Störungen
    icon: mdi:web-off
    color: "#e0423a"
    forward_service: notify.mobile_app_pixel   # nur für diese Kategorie
    forward_min_level: warning
  - name: intern
    label: Intern
    icon: mdi:home-outline
    forward_service: none                      # nie weiterleiten
  - name: pinball
    label: Flipper
    icon: mdi:gamepad-variant
    color: "#0e9fb5"
```

Jede Kategorie bekommt einen eigenen Sensor `sensor.notify_<name>` (z. B.
`sensor.notify_web`) mit Anzahl offener Meldungen, `highest_level`,
`color`, `icon` und den letzten Ereignissen als Attribut.

Weiterleitung: Hat eine Kategorie `forward_service`, gilt deren Regel,
sonst die globale (`forward_service` / `forward_min_level`). Weitergeleitete
Titel bekommen das Kategorie-Label vorangestellt, z. B. „[Web-Störungen] …“.

## Web-Monitor

Notify prüft Webseiten und Dienste selbst und meldet Ausfälle:

```yaml
monitors:
  - name: FlipperPass
    url: https://flipperpass.de
    keyword: FlipperPass        # Text muss auf der Seite stehen (optional)
    interval: 120               # Sekunden (min. 30, Standard 300)
    failures: 2                 # erst nach 2 Fehlschlägen in Folge melden
  - name: Tower OCR
    url: http://tower:8000/health
    expect_status: 200
    category: intern
    level: warning
  - name: Router WAN2
    url: tcp://192.168.88.1:443  # reiner TCP-Port-Check
    category: intern
```

| Feld | Standard | Bedeutung |
|---|---|---|
| `url` | – | `http(s)://…` oder `tcp://host:port` |
| `category` | `web` | Kategorie der Meldungen |
| `interval` | 300 | Prüfabstand in Sekunden |
| `timeout` | 10 | Sekunden bis „Zeitüberschreitung“ |
| `failures` | 2 | Fehlschläge in Folge bis zur Meldung (gegen Fehlalarme) |
| `keyword` | – | Text, der in der Antwort vorkommen muss |
| `expect_status` | – | Genauer HTTP-Status; sonst gilt alles unter 400 als OK |
| `level` | `error` | Stufe der Ausfallmeldung |
| `verify_ssl` | `true` | `false` für selbstsignierte Zertifikate |

Ablauf: Bei Ausfall entsteht eine Meldung „*Name* nicht erreichbar“ mit Grund
(HTTP-Status, Zeitüberschreitung, SSL-Fehler, Text fehlt) und Details. Ist der
Dienst wieder da, wird die Ausfallmeldung automatisch quittiert (von
„automatisch“, Kommentar „wieder erreichbar nach 12 min“) und eine bereits
erledigte Meldung „wieder erreichbar“ abgelegt.

Pro Monitor gibt es `binary_sensor.notify_monitor_<name>` (Geräteklasse
*connectivity*, `on` = erreichbar) mit `response_ms`, `last_error`,
`down_since` usw. In der Weboberfläche stehen die Monitore oben; ein Klick
prüft sofort.

Hinweis: Die Prüfung läuft aus dem Add-on-Container. Hostnamen müssen dort
auflösbar sein (im Zweifel IP-Adresse verwenden).

## Ereignisse senden

### 1. Aus Automationen und Skripten (empfohlen)

```yaml
actions:
  - event: notify_event
    event_data:
      title: Waschmaschine fertig
      message: "Programm beendet um {{ now().strftime('%H:%M') }}"
      level: success
      category: haushalt
      source: Waschküche
      key: waschmaschine
```

### 2. Als Notify-Dienst (`notify.notify_center`)

In `configuration.yaml` (Hostname steht oben auf der Info-Seite des Add-ons,
z. B. `a0d7b954-notify`, bei lokaler Installation `local-notify`):

```yaml
notify:
  - platform: rest
    name: notify_center
    resource: http://a0d7b954-notify:8099/api/notify
    method: POST_JSON
    title_param_name: title
    message_param_name: message
```

Meldungen landen dann in `default_category`.

### 3. Von außerhalb (ESP32, Skripte, Synology, Tower …)

Port `8099` im Add-on unter *Netzwerk* freigeben **und** `api_token` setzen.
Ohne Token werden Zugriffe von außerhalb von Home Assistant abgewiesen.

```bash
curl -X POST http://homeassistant.local:8099/api/events \
  -H "Authorization: Bearer DEIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"OCR-Job fehlgeschlagen","level":"error","category":"web","source":"FlipperPass","tags":["ocr"]}'

# Geräte, die nur GET können:
curl "http://homeassistant.local:8099/api/notify?token=DEIN_TOKEN&title=Hallo&category=extern"
```

## Dashboard

Sensoren:

- `sensor.notify_unread` – alle **offenen** (nicht quittierten); Attribute
  `new`, `seen`, `acked`, `highest_level`, `by_level`, `by_category`,
  `new_by_category`, `events` (jeweils mit `status`)
- `binary_sensor.notify_unseen` – `on`, solange etwas noch nicht gesehen wurde
  (z. B. für eine blinkende LED); Attribute `count`, `highest_level`
- `binary_sensor.notify_critical` – `on` bei offenen kritischen Meldungen
- `sensor.notify_<kategorie>` – offene je Kategorie, z. B. `sensor.notify_web`,
  Attribut `new`
- `sensor.notify_last` – letztes Ereignis inkl. `status`, `seen_by`,
  `acked_by`, `ack_note`
- `binary_sensor.notify_monitor_<name>` – je Web-Monitor

Fertige Karten (Mushroom, Markdown, Kategorie-Kacheln, Monitor-Status) stehen
in `examples/dashboard.yaml`, Skripte in `examples/scripts.yaml`.

## Gesehen / Quittieren aus Home Assistant

```yaml
- event: notify_ack                              # alle offenen quittieren
- event: notify_ack
  event_data: { category: web, note: "Server neu gestartet" }
- event: notify_ack
  event_data: { key: haustuer, by: Automation }  # bestimmte Meldung
- event: notify_ack
  event_data: { ids: [1, 2, 3] }
- event: notify_ack
  event_data: { id: 42, reopen: true }           # wieder öffnen
- event: notify_ack
  event_data: { delete: true, category: web, read_only: true }  # quittierte Web-Meldungen löschen

- event: notify_seen                             # alle als gesehen
- event: notify_seen
  event_data: { category: haushalt }
```

Ohne `by` wird der auslösende HA-Benutzer eingetragen, sonst „Home Assistant“.

## Auf neue Ereignisse reagieren

Bei jedem Ereignis feuert Notify `notify_received`, beim Quittieren
`notify_acknowledged` mit `id`, `title`, `level`, `category`, `key`, `by` und
`note` (beides abschaltbar über `fire_events`). Beispiel – rot blinken, bis
eine Web-Störung quittiert ist:

```yaml
- alias: Web-Störung → Licht an
  triggers:
    - trigger: event
      event_type: notify_received
      event_data: { category: web, level: error }
  actions:
    - action: light.turn_on
      target: { entity_id: light.buero }
      data: { color_name: red, flash: long }

- alias: Web-Störung quittiert → Licht aus
  triggers:
    - trigger: event
      event_type: notify_acknowledged
      event_data: { category: web }
  actions:
    - action: light.turn_off
      target: { entity_id: light.buero }
```

Oder nach Stufe:

```yaml
triggers:
  - trigger: event
    event_type: notify_received
    event_data: { category: web, level: error }
actions:
  - action: light.turn_on
    target: { entity_id: light.buero }
    data: { color_name: red, flash: long }
```

## Optionen

| Option | Standard | Beschreibung |
|---|---|---|
| `retention_days` | 90 | Ältere Ereignisse werden stündlich gelöscht |
| `max_events` | 5000 | Obergrenze; quittierte und älteste fliegen zuerst |
| `api_token` | – | Pflicht für Zugriff über Port 8099 |
| `default_category` | – | Kategorie, wenn keine angegeben ist |
| `categories` | 5 Standard-Kategorien | siehe oben |
| `monitors` | – | siehe oben |
| `event_type` | `notify_event` | HA-Event für neue Ereignisse |
| `ack_event_type` | `notify_ack` | HA-Event zum Quittieren |
| `seen_event_type` | `notify_seen` | HA-Event für „gesehen“ |
| `ack_min_level` | `warning` | Ab dieser Stufe muss quittiert werden; darunter reicht „gesehen“ |
| `mobile_actions` | `true` | Buttons in Handy-Benachrichtigungen, Entfernen nach Quittieren |
| `fire_events` | `true` | `notify_received` und `notify_acknowledged` auslösen |
| `sensor_events` | 10 | Ereignisse im Sensor-Attribut `events` (max. 25) |
| `forward_service` | – | Globaler Notify-Dienst für Weiterleitung |
| `forward_min_level` | `error` | Ab dieser Stufe weiterleiten |

## REST-API

| Methode | Pfad | Zweck |
|---|---|---|
| `POST` | `/api/events` (auch `/api/event`, `/api/notify`) | Ereignis anlegen (JSON, Formular oder Klartext) |
| `GET`  | `/api/notify?title=…&message=…&category=…` | Ereignis anlegen per GET |
| `GET`  | `/api/events?…` | Liste + Statistik. Parameter: `limit`, `offset`, `status` (`new`, `seen`, `acked`), `level`, `category` (`_none` = ohne), `tag`, `source`, `unread=1` (= offen), `q`, `sort` (`new`, `severity`, `category`, `source`, `oldest_open`) – Listen kommagetrennt |
| `GET`  | `/api/events/{id}` | Einzelnes Ereignis |
| `POST` | `/api/events/{id}/seen` | als gesehen markieren |
| `POST` | `/api/events/{id}/ack` | quittieren, optional `{"note":"…","by":"…"}` |
| `POST` | `/api/events/{id}/reopen` | Quittierung zurücknehmen |
| `POST` | `/api/events/seen` | `{"ids":[…]}`, `{"category":"web"}`, `{"key":"…"}` oder `{}` = alle |
| `POST` | `/api/events/ack` | wie oben, plus `note`, `by`; `{"reopen":true}` öffnet wieder |
| `DELETE` | `/api/events/{id}` | löschen |
| `POST` | `/api/events/delete` | `{"ids":[…]}`, `{"category":"web"}`, `{"read_only":true}` (= quittierte) oder `{"all":true}` |
| `GET`  | `/api/monitors` | Status aller Web-Monitore |
| `POST` | `/api/monitors/{name}/check` | Sofort prüfen |
| `GET`  | `/api/config`, `/api/stats`, `/api/whoami`, `/api/export`, `/api/health` | Kategorien, Statistik, erkannter Benutzer, JSON-Export, Status |

Die Pfade `/api/events/read` und `/api/events/{id}/read` aus 1.x funktionieren
weiter (= quittieren).
