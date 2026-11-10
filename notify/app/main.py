#!/usr/bin/env python3
"""Notify – Ereignis-Sammelstelle für Home Assistant.

Nimmt Ereignisse über HTTP oder Home-Assistant-Events entgegen, speichert sie
in SQLite, ordnet sie Kategorien zu, überwacht Webdienste, spiegelt alles als
Sensoren nach Home Assistant und liefert eine Weboberfläche (Ingress).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

OPTIONS_FILE = Path(os.environ.get("NOTIFY_OPTIONS", "/data/options.json"))
DB_FILE = Path(os.environ.get("NOTIFY_DB", "/data/notify.db"))
STATIC_DIR = Path(__file__).parent / "static"
PORT = int(os.environ.get("NOTIFY_PORT", "8099"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
CORE_API = "http://supervisor/core/api"
CORE_WS = "ws://supervisor/core/websocket"
VERSION = "1.2.0"

LEVELS = ["debug", "info", "success", "warning", "error", "critical"]
LEVEL_ALIASES = {
    "warn": "warning", "err": "error", "crit": "critical", "fatal": "critical",
    "ok": "success", "notice": "info", "alarm": "critical",
}
LEVEL_ICONS = {
    "debug": "mdi:bug-outline", "info": "mdi:information-outline",
    "success": "mdi:check-circle-outline", "warning": "mdi:alert-outline",
    "error": "mdi:alert-circle-outline", "critical": "mdi:alarm-light-outline",
}
KNOWN_FIELDS = {"title", "message", "text", "level", "severity", "source",
                "key", "tags", "data", "token", "category", "kategorie"}
SORTS = ("new", "severity", "category", "source", "oldest_open")
# Lebenszyklus: neu → gesehen → quittiert   ('read' = quittiert, aus Kompatibilität)
STATUSES = ("new", "seen", "acked")
STATUS_SQL = {
    "new": "(read=0 AND seen_at IS NULL)",
    "seen": "(read=0 AND seen_at IS NOT NULL)",
    "acked": "(read=1)",
}
OUTGOING_EVENTS = {"notify_received", "notify_acknowledged"}
MOBILE_ACTION_EVENT = "mobile_app_notification_action"
MOBILE_ACTION_RE = re.compile(r"NOTIFY_(ACK|ACKNOTE|SEEN)_(\d+)")
NO_CATEGORY = "_none"
PALETTE = ["#2f6fed", "#8a5cf6", "#e0423a", "#1f9d55", "#e08a00", "#0e9fb5",
           "#b01e6c", "#6b7280"]
# Supervisor-/Ingress-Netz und lokale Adressen gelten als vertrauenswürdig
TRUSTED_NETS = [ipaddress.ip_network(n) for n in
                ("172.30.32.0/23", "127.0.0.0/8", "::1/128")]

DEFAULT_CATEGORIES = [
    {"name": "intern", "label": "Intern", "icon": "mdi:home-outline", "color": "#2f6fed"},
    {"name": "extern", "label": "Extern", "icon": "mdi:earth", "color": "#8a5cf6"},
    {"name": "web", "label": "Web-Störungen", "icon": "mdi:web-off", "color": "#e0423a"},
    {"name": "haushalt", "label": "Haushalt", "icon": "mdi:washing-machine", "color": "#1f9d55"},
    {"name": "system", "label": "System", "icon": "mdi:server", "color": "#6b7280"},
]
DEFAULTS = {
    "retention_days": 90,
    "max_events": 5000,
    "api_token": "",
    "event_type": "notify_event",
    "ack_event_type": "notify_ack",
    "seen_event_type": "notify_seen",
    "ack_min_level": "warning",
    "mobile_actions": True,
    "fire_events": True,
    "sensor_events": 10,
    "default_category": "",
    "categories": DEFAULT_CATEGORIES,
    "monitors": [],
    "forward_service": "",
    "forward_min_level": "error",
    "log_level": "info",
}

log = logging.getLogger("notify")


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def load_options() -> dict:
    opts = dict(DEFAULTS)
    try:
        opts.update({k: v for k, v in json.loads(OPTIONS_FILE.read_text()).items()
                     if v is not None})
    except FileNotFoundError:
        pass
    except Exception as err:  # noqa: BLE001
        print(f"Optionen nicht lesbar ({err}), nutze Standardwerte")
    return opts


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 1


def norm_level(value, default="info") -> str:
    level = str(value or default).lower().strip()
    level = LEVEL_ALIASES.get(level, level)
    return level if level in LEVELS else default


def slugify(text: str) -> str:
    text = str(text).lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "x"


def norm_category(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()[:50]


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def short_time(ts: float) -> str:
    dt = datetime.fromtimestamp(ts).astimezone()
    if dt.date() == datetime.now().astimezone().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m. %H:%M")


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def parse_categories(opts: dict) -> list[dict]:
    cats, seen = [], set()
    for i, c in enumerate(opts.get("categories") or []):
        if isinstance(c, str):
            c = {"name": c}
        name = norm_category(c.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        cats.append({
            "name": name,
            "label": str(c.get("label") or name.capitalize()),
            "icon": str(c.get("icon") or "mdi:tag-outline"),
            "color": str(c.get("color") or PALETTE[i % len(PALETTE)]),
            "forward_service": str(c.get("forward_service") or "").strip(),
            "forward_min_level": norm_level(c.get("forward_min_level"), ""),
            "slug": slugify(name),
        })
    return cats


# --------------------------------------------------------------------------
# Eingangsdaten normalisieren
# --------------------------------------------------------------------------
def normalize(payload, default_source: str = "", default_category: str = "") -> dict:
    if not isinstance(payload, dict):
        raise ValueError("JSON-Objekt erwartet")
    title = str(payload.get("title") or "").strip()
    message = str(payload.get("message") or payload.get("text") or "").strip()
    if not title and not message:
        raise ValueError("'title' oder 'message' ist erforderlich")
    if not title:
        title = message.splitlines()[0][:120]
        if title == message:
            message = ""

    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = list(dict.fromkeys(str(t).strip().lstrip("#")[:40] for t in tags
                              if str(t).strip()))[:20]

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {"value": data}
    extras = {k: v for k, v in payload.items() if k not in KNOWN_FIELDS}
    if extras:
        data = {**extras, **data}

    key = payload.get("key")
    category = norm_category(payload.get("category") or payload.get("kategorie")
                             or default_category)
    return {
        "title": title[:300],
        "message": message[:10000],
        "level": norm_level(payload.get("level") or payload.get("severity")),
        "source": str(payload.get("source") or default_source)[:100],
        "category": category,
        "key": str(key)[:200] if key not in (None, "") else None,
        "tags": tags,
        "data": data,
    }


# --------------------------------------------------------------------------
# Speicher (SQLite)
# --------------------------------------------------------------------------
LEVEL_CASE = "CASE level " + " ".join(
    f"WHEN '{lv}' THEN {i}" for i, lv in enumerate(LEVELS)) + " ELSE 1 END"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL    NOT NULL,
                updated  REAL    NOT NULL,
                level    TEXT    NOT NULL,
                title    TEXT    NOT NULL,
                message  TEXT    NOT NULL DEFAULT '',
                source   TEXT    NOT NULL DEFAULT '',
                key      TEXT,
                tags     TEXT    NOT NULL DEFAULT '[]',
                data     TEXT    NOT NULL DEFAULT '{}',
                count    INTEGER NOT NULL DEFAULT 1,
                read     INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # Migration 1.0 → 1.1
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        if "category" not in cols:
            self.db.execute("ALTER TABLE events ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        # Migration 1.1 → 1.2: gesehen / quittiert / weitergeleitet
        for col in ("seen_at REAL", "seen_by TEXT", "acked_at REAL", "acked_by TEXT",
                    "ack_note TEXT", "forwarded TEXT"):
            if col.split()[0] not in cols:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {col}")
        if "acked_at" not in cols:
            # bisher „gelesen“ = jetzt „quittiert“
            self.db.execute("UPDATE events SET acked_at=updated, seen_at=updated WHERE read=1")
        self.db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_events_updated ON events(updated);
            CREATE INDEX IF NOT EXISTS idx_events_key ON events(key, read);
            CREATE INDEX IF NOT EXISTS idx_events_read ON events(read);
            CREATE INDEX IF NOT EXISTS idx_events_category ON events(category, read);
            """
        )
        self.db.commit()

    @staticmethod
    def to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["data"] = json.loads(d["data"] or "{}")
        d["read"] = bool(d["read"])
        d["status"] = "acked" if d["read"] else ("seen" if d.get("seen_at") else "new")
        d["time"] = iso(d["updated"])
        d["created"] = iso(d["ts"])
        d["seen_time"] = iso(d.get("seen_at"))
        d["acked_time"] = iso(d.get("acked_at"))
        return d

    def get(self, event_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return self.to_dict(row) if row else None

    def add(self, ev: dict) -> tuple[dict, bool]:
        """Speichert ein Ereignis. Gleicher 'key' + noch offen → zusammenfassen.

        Eine Wiederholung setzt „gesehen“ zurück – das Ereignis ist wieder neu.
        """
        now = time.time()
        if ev["key"]:
            row = self.db.execute(
                "SELECT id FROM events WHERE key=? AND read=0 ORDER BY updated DESC LIMIT 1",
                (ev["key"],),
            ).fetchone()
            if row:
                self.db.execute(
                    """UPDATE events SET updated=?, level=?, title=?, message=?, source=?,
                       category=?, tags=?, data=?, count=count+1,
                       seen_at=NULL, seen_by=NULL WHERE id=?""",
                    (now, ev["level"], ev["title"], ev["message"], ev["source"],
                     ev["category"], json.dumps(ev["tags"]), json.dumps(ev["data"]),
                     row["id"]),
                )
                self.db.commit()
                return self.get(row["id"]), False
        cur = self.db.execute(
            """INSERT INTO events (ts, updated, level, title, message, source, category,
                                   key, tags, data)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (now, now, ev["level"], ev["title"], ev["message"], ev["source"],
             ev["category"], ev["key"], json.dumps(ev["tags"]), json.dumps(ev["data"])),
        )
        self.db.commit()
        return self.get(cur.lastrowid), True

    def query(self, *, limit=50, offset=0, levels=None, categories=None, tags=None,
              source=None, unread=False, q=None, since=None, sort="new",
              statuses=None,
              category_order: list[str] | None = None) -> tuple[list[dict], int]:
        where, args = [], []
        if statuses:
            conds = [STATUS_SQL[s] for s in statuses if s in STATUS_SQL]
            if conds:
                where.append("(" + " OR ".join(conds) + ")")
        if levels:
            where.append(f"level IN ({','.join('?' * len(levels))})")
            args += levels
        if categories:
            cats = ["" if c == NO_CATEGORY else c for c in categories]
            where.append(f"category IN ({','.join('?' * len(cats))})")
            args += cats
        if tags:
            where.append("EXISTS (SELECT 1 FROM json_each(events.tags) WHERE value IN "
                         f"({','.join('?' * len(tags))}))")
            args += tags
        if source:
            where.append("source=?")
            args.append(source)
        if unread:
            where.append("read=0")
        if q:
            where.append("(title LIKE ? OR message LIKE ? OR source LIKE ? OR category LIKE ?"
                         " OR tags LIKE ? OR data LIKE ? OR ack_note LIKE ? OR acked_by LIKE ?)")
            args += [f"%{q}%"] * 8
        if since:
            where.append("updated>?")
            args.append(since)
        sql_where = f"WHERE {' AND '.join(where)}" if where else ""

        order_args: list = []
        if sort == "severity":
            order = f"read ASC, {LEVEL_CASE} DESC, updated DESC"
        elif sort == "category":
            known = category_order or []
            case = ("CASE category " + " ".join("WHEN ? THEN %d" % i
                                                for i in range(len(known)))
                    + f" WHEN '' THEN {len(known) + 1} ELSE {len(known)} END") if known else \
                "CASE category WHEN '' THEN 1 ELSE 0 END"
            order_args = list(known)
            order = f"{case}, category, updated DESC"
        elif sort == "source":
            order = "source = '' , source COLLATE NOCASE, updated DESC"
        elif sort == "oldest_open":
            order = "read ASC, CASE WHEN read=0 THEN updated END ASC, updated DESC"
        else:
            order = "updated DESC"

        total = self.db.execute(f"SELECT COUNT(*) FROM events {sql_where}", args).fetchone()[0]
        rows = self.db.execute(
            f"SELECT * FROM events {sql_where} ORDER BY {order}, id DESC LIMIT ? OFFSET ?",
            args + order_args + [limit, offset],
        ).fetchall()
        return [self.to_dict(r) for r in rows], total

    def stats(self) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        unread_by_level = {lv: 0 for lv in LEVELS}
        new_by_level = {lv: 0 for lv in LEVELS}
        by_cat: dict[str, dict] = {}
        for r in self.db.execute(
                "SELECT category, level, COUNT(*) c, SUM(seen_at IS NULL) n FROM events "
                "WHERE read=0 GROUP BY category, level"):
            unread_by_level[r["level"]] += r["c"]
            new_by_level[r["level"]] += r["n"] or 0
            cat = by_cat.setdefault(r["category"] or NO_CATEGORY,
                                    {"unread": 0, "new": 0, "highest_level": None})
            cat["unread"] += r["c"]
            cat["new"] += r["n"] or 0
            if (cat["highest_level"] is None
                    or level_rank(r["level"]) > level_rank(cat["highest_level"])):
                cat["highest_level"] = r["level"]
        categories = [r[0] or NO_CATEGORY for r in self.db.execute(
            "SELECT DISTINCT category FROM events ORDER BY category")]
        sources = [r[0] for r in self.db.execute(
            "SELECT DISTINCT source FROM events WHERE source<>'' ORDER BY source COLLATE NOCASE")]
        tags = [{"tag": r[0], "count": r[1]} for r in self.db.execute(
            "SELECT value, COUNT(*) c FROM events, json_each(events.tags) "
            "GROUP BY value ORDER BY c DESC, value LIMIT 30")]
        unread = sum(unread_by_level.values())
        new = sum(new_by_level.values())
        highest = next((lv for lv in reversed(LEVELS) if unread_by_level[lv]), None)
        highest_new = next((lv for lv in reversed(LEVELS) if new_by_level[lv]), None)
        return {"total": total, "unread": unread, "new": new, "seen": unread - new,
                "acked": total - unread,
                "unread_by_level": unread_by_level, "new_by_level": new_by_level,
                "highest_level": highest, "highest_new_level": highest_new,
                "unread_by_category": by_cat,
                "categories": categories, "sources": sources, "tags": tags}

    # ---- Status ändern ---------------------------------------------------
    @staticmethod
    def _where(base: str, ids, key, category) -> tuple[str, list] | None:
        where, args = [base], []
        if ids is not None:
            if not ids:
                return None
            where.append(f"id IN ({','.join('?' * len(ids))})")
            args += ids
        if key:
            where.append("key=?")
            args.append(key)
        if category is not None:
            where.append("category=?")
            args.append("" if category == NO_CATEGORY else norm_category(category))
        return " AND ".join(where), args

    def _update(self, base: str, ids, key, category, set_sql: str, params: list,
                extra: str = "") -> list[int]:
        w = self._where(base + extra, ids, key, category)
        if w is None:
            return []
        sql, args = w
        affected = [r[0] for r in self.db.execute(f"SELECT id FROM events WHERE {sql}", args)]
        if affected:
            self.db.execute(f"UPDATE events SET {set_sql} WHERE {sql}", params + args)
            self.db.commit()
        return affected

    def ack(self, ids=None, *, key=None, category=None, by="", note="") -> list[int]:
        """Offene Ereignisse quittieren. Gibt die betroffenen IDs zurück."""
        now = time.time()
        return self._update(
            "read=0", ids, key, category,
            "read=1, acked_at=?, acked_by=?, ack_note=?, "
            "seen_at=COALESCE(seen_at, ?), seen_by=COALESCE(seen_by, ?)",
            [now, by or None, (note or None), now, by or None])

    def reopen(self, ids=None, *, key=None, category=None) -> list[int]:
        """Quittierung zurücknehmen – „gesehen“ bleibt erhalten."""
        return self._update("read=1", ids, key, category,
                            "read=0, acked_at=NULL, acked_by=NULL, ack_note=NULL", [])

    def mark_seen(self, ids=None, *, key=None, category=None, by="",
                  close_below: int = 0) -> tuple[list[int], list[int]]:
        """Neue Ereignisse als gesehen markieren. Ereignisse unterhalb der Stufe
        ``close_below`` gelten damit auch als erledigt (automatisch quittiert)."""
        closed = []
        if close_below > 0:
            closed = self._update(
                "read=0 AND seen_at IS NULL", ids, key, category,
                "read=1, seen_at=?, seen_by=?, acked_at=?, acked_by=?, ack_note=?",
                [time.time(), by or None, time.time(), by or None, "beim Ansehen erledigt"],
                extra=f" AND {LEVEL_CASE} < {int(close_below)}")
        seen = self._update("read=0 AND seen_at IS NULL", ids, key, category,
                            "seen_at=?, seen_by=?", [time.time(), by or None])
        return seen + closed, closed

    def set_forwarded(self, event_id: int, target: str):
        self.db.execute("UPDATE events SET forwarded=? WHERE id=?", (target, event_id))
        self.db.commit()

    def delete(self, ids: list[int] | None = None, read_only: bool = False,
               everything: bool = False, category: str | None = None) -> int:
        if everything:
            cur = self.db.execute("DELETE FROM events")
        elif category is not None:
            sql = "DELETE FROM events WHERE category=?" + (" AND read=1" if read_only else "")
            cur = self.db.execute(sql, ("" if category == NO_CATEGORY
                                        else norm_category(category),))
        elif read_only:
            cur = self.db.execute("DELETE FROM events WHERE read=1")
        elif ids:
            cur = self.db.execute(
                f"DELETE FROM events WHERE id IN ({','.join('?' * len(ids))})", ids)
        else:
            return 0
        self.db.commit()
        return cur.rowcount

    def prune(self, retention_days: int, max_events: int) -> int:
        removed = self.db.execute(
            "DELETE FROM events WHERE updated < ?",
            (time.time() - retention_days * 86400,)).rowcount
        over = self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] - max_events
        if over > 0:
            removed += self.db.execute(
                """DELETE FROM events WHERE id IN (
                     SELECT id FROM events ORDER BY read DESC, updated ASC LIMIT ?)""",
                (over,)).rowcount
        self.db.commit()
        return removed


# --------------------------------------------------------------------------
# Web-Monitor
# --------------------------------------------------------------------------
class Monitor:
    def __init__(self, core: "NotifyCore", cfg: dict):
        self.core = core
        self.name = str(cfg.get("name") or cfg.get("url"))
        self.url = str(cfg["url"]).strip()
        self.slug = slugify(self.name)
        self.key = f"monitor:{self.slug}"
        self.category = norm_category(cfg.get("category") or "web")
        self.interval = max(30, int(cfg.get("interval") or 300))
        self.timeout = max(1, min(int(cfg.get("timeout") or 10), 120))
        self.keyword = str(cfg.get("keyword") or "")
        self.expect_status = int(cfg["expect_status"]) if cfg.get("expect_status") else None
        self.level = norm_level(cfg.get("level"), "error")
        self.failures = max(1, int(cfg.get("failures") or 2))
        self.verify_ssl = cfg.get("verify_ssl", True) is not False
        # Zustand
        self.status: str | None = None          # None = unbekannt, "up", "down"
        self.fail_count = 0
        self.first_fail: float | None = None
        self.down_since: float | None = None
        self.last_check: float | None = None
        self.last_ok: float | None = None
        self.response_ms: int | None = None
        self.http_status: int | None = None
        self.last_error = ""
        self.wake = asyncio.Event()

    def as_dict(self) -> dict:
        return {
            "name": self.name, "slug": self.slug, "url": self.url,
            "category": self.category, "status": self.status or "unknown",
            "response_ms": self.response_ms, "http_status": self.http_status,
            "last_error": self.last_error, "last_check": iso(self.last_check),
            "last_ok": iso(self.last_ok), "down_since": iso(self.down_since),
            "interval": self.interval,
        }

    async def check(self, session: ClientSession) -> tuple[bool, str]:
        t0 = time.monotonic()
        self.http_status = None
        try:
            if self.url.startswith("tcp://"):
                u = urlparse(self.url)
                if not u.hostname or not u.port:
                    return False, "TCP-Adresse braucht Host und Port (tcp://host:port)"
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(u.hostname, u.port), self.timeout)
                writer.close()
                self.response_ms = int((time.monotonic() - t0) * 1000)
                return True, ""
            async with session.get(self.url, timeout=ClientTimeout(total=self.timeout),
                                   ssl=True if self.verify_ssl else False,
                                   allow_redirects=True) as resp:
                self.http_status = resp.status
                body = ""
                if self.keyword:
                    body = (await resp.content.read(512 * 1024)).decode("utf-8", "replace")
                self.response_ms = int((time.monotonic() - t0) * 1000)
                ok = (resp.status == self.expect_status if self.expect_status
                      else resp.status < 400)
                if not ok:
                    return False, f"HTTP {resp.status} {resp.reason or ''}".strip()
                if self.keyword and self.keyword not in body:
                    return False, f"Text „{self.keyword}“ nicht auf der Seite gefunden"
                return True, ""
        except asyncio.TimeoutError:
            err = f"Zeitüberschreitung nach {self.timeout} s"
        except aiohttp.ClientConnectorCertificateError as e:
            err = f"SSL-Zertifikat ungültig: {e.certificate_error}"
        except aiohttp.ClientSSLError as e:
            err = f"SSL-Fehler: {e}"
        except aiohttp.ClientConnectorError as e:
            err = f"Verbindung fehlgeschlagen: {e.os_error or e}"
        except aiohttp.ClientError as e:
            err = f"{type(e).__name__}: {e}"
        except OSError as e:
            err = f"Verbindung fehlgeschlagen: {e}"
        self.response_ms = None
        return False, err

    async def run(self, session: ClientSession):
        await asyncio.sleep(random.uniform(1, 5))  # Start entzerren
        while True:
            try:
                await self.tick(session)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                log.exception("Monitor %s: %s", self.name, err)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval)
            except asyncio.TimeoutError:
                pass

    async def tick(self, session: ClientSession):
        ok, err = await self.check(session)
        now = time.time()
        self.last_check = now
        core = self.core
        base = {"category": self.category, "source": self.name, "tags": ["monitor"]}
        if ok:
            self.last_error = ""
            self.fail_count, self.first_fail = 0, None
            self.last_ok = now
            if self.status == "down":
                downtime = now - (self.down_since or now)
                core.ack(None, key=self.key, by="automatisch",
                         note=f"wieder erreichbar nach {fmt_duration(downtime)}")
                log.info("Monitor %s wieder erreichbar (Ausfall %s)",
                         self.name, fmt_duration(downtime))
                await core.add_event({
                    **base, "title": f"{self.name} wieder erreichbar",
                    "message": f"Ausfall: {fmt_duration(downtime)} "
                               f"(seit {short_time(self.down_since or now)})",
                    "level": "success", "key": f"{self.key}:up",
                    "data": {"url": self.url, "response_ms": self.response_ms,
                             "downtime_s": int(downtime)}}, closed_by="automatisch")
            elif self.status is None:
                # Nach Neustart: noch offene Ausfallmeldung schließen
                core.ack(None, key=self.key, by="automatisch", note="beim Start erreichbar")
            self.status, self.down_since = "up", None
        else:
            self.last_error = err
            self.fail_count += 1
            self.first_fail = self.first_fail or now
            log.debug("Monitor %s: Fehler %s/%s – %s",
                      self.name, self.fail_count, self.failures, err)
            if self.status != "down" and self.fail_count >= self.failures:
                self.status, self.down_since = "down", self.first_fail
                log.warning("Monitor %s nicht erreichbar: %s", self.name, err)
                await core.add_event({
                    **base, "title": f"{self.name} nicht erreichbar", "message": err,
                    "level": self.level, "key": self.key,
                    "data": {"url": self.url, "http_status": self.http_status,
                             "error": err, "since": iso(self.down_since),
                             "checks_failed": self.fail_count}})
        core.ha.changed()


# --------------------------------------------------------------------------
# Home-Assistant-Anbindung
# --------------------------------------------------------------------------
class HomeAssistant:
    def __init__(self, core: "NotifyCore"):
        self.core = core
        self.session: ClientSession | None = None
        self._dirty = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.connected = False
        self._users: dict[str, str] = {}

    @property
    def active(self) -> bool:
        return self.session is not None

    async def user_name(self, user_id: str | None) -> str:
        """HA-Benutzer-ID → Name, über die person.*-Entitäten (Attribut user_id)."""
        if not user_id or not self.active:
            return ""
        if user_id not in self._users:
            try:
                async with self.session.get(f"{CORE_API}/states") as resp:
                    for st in await resp.json():
                        attrs = st.get("attributes") or {}
                        if st["entity_id"].startswith("person.") and attrs.get("user_id"):
                            self._users[attrs["user_id"]] = (attrs.get("friendly_name")
                                                             or st["entity_id"][7:])
            except Exception as err:  # noqa: BLE001
                log.debug("Personen konnten nicht gelesen werden: %s", err)
            self._users.setdefault(user_id, "")
        return self._users[user_id]

    async def call_service(self, target: str, body: dict) -> bool:
        domain, _, service = target.partition(".")
        if not service:
            domain, service = "notify", domain
        try:
            async with self.session.post(f"{CORE_API}/services/{domain}/{service}",
                                         json=body) as resp:
                if resp.status >= 400:
                    log.warning("Dienst %s: HTTP %s %s", target, resp.status, await resp.text())
                    return False
                return True
        except Exception as err:  # noqa: BLE001
            log.warning("Dienst %s fehlgeschlagen: %s", target, err)
            return False

    async def fire(self, event_type: str, data: dict):
        try:
            async with self.session.post(f"{CORE_API}/events/{event_type}", json=data) as resp:
                if resp.status >= 400:
                    log.warning("%s: HTTP %s", event_type, resp.status)
        except Exception as err:  # noqa: BLE001
            log.warning("%s fehlgeschlagen: %s", event_type, err)

    async def start(self):
        if not SUPERVISOR_TOKEN:
            log.warning("Kein SUPERVISOR_TOKEN – Home-Assistant-Anbindung deaktiviert "
                        "(nur Weboberfläche/REST-API aktiv)")
            return
        self.session = ClientSession(
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=ClientTimeout(total=20))
        self._tasks = [asyncio.create_task(self._ws_loop()),
                       asyncio.create_task(self._sensor_loop())]
        self.changed()

    def changed(self):
        self._dirty.set()

    # ---- Sensoren -------------------------------------------------------
    async def _sensor_loop(self):
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass  # periodisch neu setzen, z. B. nach HA-Neustart
            self._dirty.clear()
            try:
                await self.push_sensors()
            except Exception as err:  # noqa: BLE001
                log.debug("Sensoren konnten nicht gesetzt werden: %s", err)
            await asyncio.sleep(1)  # Bündeln bei vielen Ereignissen

    async def _set_state(self, entity_id: str, state, attributes: dict):
        async with self.session.post(f"{CORE_API}/states/{entity_id}",
                                     json={"state": state, "attributes": attributes}) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"{entity_id}: HTTP {resp.status} {await resp.text()}")

    @staticmethod
    def _compact(events: list[dict]) -> list[dict]:
        return [{
            "id": e["id"], "level": e["level"], "title": e["title"],
            "message": e["message"][:300], "source": e["source"],
            "category": e["category"], "count": e["count"], "status": e["status"],
            "seen_by": e.get("seen_by") or "",
            "time": e["time"], "when": short_time(e["updated"]),
            "icon": LEVEL_ICONS[e["level"]],
        } for e in events]

    async def push_sensors(self):
        core = self.core
        store, opts = core.store, core.opts
        stats = store.stats()
        n = int(opts["sensor_events"])
        events, _ = store.query(limit=n, unread=True) if n else ([], 0)
        by_cat = stats["unread_by_category"]
        # Zustand = offene (nicht quittierte) Ereignisse; Entitäts-ID bleibt kompatibel
        await self._set_state("sensor.notify_unread", stats["unread"], {
            "friendly_name": "Notify offen",
            "icon": "mdi:bell-badge" if stats["unread"] else "mdi:bell-outline",
            "state_class": "measurement",
            "total": stats["total"],
            "new": stats["new"], "seen": stats["seen"], "acked": stats["acked"],
            "highest_level": stats["highest_level"] or "none",
            "by_level": stats["unread_by_level"],
            "by_category": {c: v["unread"] for c, v in by_cat.items()},
            "new_by_category": {c: v["new"] for c, v in by_cat.items()},
            "events": self._compact(events),
        })

        # Noch nicht gesehene Ereignisse – z. B. für eine blinkende LED
        await self._set_state("binary_sensor.notify_unseen", "on" if stats["new"] else "off", {
            "friendly_name": "Notify ungesehen",
            "count": stats["new"],
            "highest_level": stats["highest_new_level"] or "none",
            "icon": "mdi:bell-ring" if stats["new"] else "mdi:bell-check-outline",
        })

        # Für bedingte Karten/Automationen: gibt es offene kritische Meldungen?
        crit = stats["unread_by_level"]["critical"]
        await self._set_state("binary_sensor.notify_critical", "on" if crit else "off", {
            "friendly_name": "Notify kritische Meldung",
            "device_class": "problem", "count": crit,
            "icon": "mdi:alarm-light" if crit else "mdi:alarm-light-off-outline",
        })

        # Ein Sensor pro Kategorie
        for cat in core.all_categories(stats):
            if cat["name"] == NO_CATEGORY:
                continue
            info = by_cat.get(cat["name"], {"unread": 0, "new": 0, "highest_level": None})
            cat_events, _ = (store.query(limit=n, unread=True, categories=[cat["name"]])
                             if n else ([], 0))
            await self._set_state(f"sensor.notify_{cat['slug']}", info["unread"], {
                "friendly_name": f"Notify {cat['label']}",
                "icon": cat["icon"], "color": cat["color"], "category": cat["name"],
                "state_class": "measurement", "new": info["new"],
                "highest_level": info["highest_level"] or "none",
                "events": self._compact(cat_events),
            })

        last, _ = store.query(limit=1)
        if last:
            e = last[0]
            await self._set_state("sensor.notify_last", e["title"][:250], {
                "friendly_name": "Notify letztes Ereignis",
                "icon": LEVEL_ICONS[e["level"]],
                "id": e["id"], "level": e["level"], "message": e["message"][:1000],
                "source": e["source"], "category": e["category"], "count": e["count"],
                "read": e["read"], "status": e["status"],
                "seen_by": e.get("seen_by") or "", "acked_by": e.get("acked_by") or "",
                "ack_note": e.get("ack_note") or "",
                "time": e["time"], "when": short_time(e["updated"]),
                "tags": e["tags"],
            })
        else:
            await self._set_state("sensor.notify_last", "–", {
                "friendly_name": "Notify letztes Ereignis", "icon": "mdi:bell-outline"})

        # Ein binary_sensor pro Web-Monitor
        for m in core.monitors:
            state = {"up": "on", "down": "off"}.get(m.status, "unknown")
            await self._set_state(f"binary_sensor.notify_monitor_{m.slug}", state, {
                "friendly_name": f"{m.name} erreichbar",
                "device_class": "connectivity",
                "icon": "mdi:web-check" if m.status == "up" else "mdi:web-off",
                **{k: v for k, v in m.as_dict().items() if k not in ("name", "slug")},
            })

    # ---- Ereignisse an HA ----------------------------------------------
    async def on_new(self, ev: dict, is_new: bool, forward: bool = True):
        opts = self.core.opts
        if opts["fire_events"]:
            await self.fire("notify_received", {**ev, "new": is_new})

        # Weiterleitung: Regel der Kategorie hat Vorrang vor der globalen
        cat = self.core.category_cfg(ev["category"])
        target = (cat or {}).get("forward_service") or opts.get("forward_service") or ""
        min_level = ((cat or {}).get("forward_min_level")
                     or opts.get("forward_min_level") or "error")
        target = target.strip()
        if "." not in target and target and target.lower() != "none":
            target = f"notify.{target}"
        if (forward and target and target.lower() != "none" and is_new
                and level_rank(ev["level"]) >= level_rank(min_level)):
            prefix = f"[{cat['label']}] " if cat else ""
            body = {"title": prefix + ev["title"], "message": ev["message"] or ev["title"]}
            if target.startswith("notify.mobile_app_") and opts.get("mobile_actions", True):
                # Buttons in der Handy-Benachrichtigung; „tag“ erlaubt späteres Entfernen
                body["data"] = {
                    "tag": f"notify-{ev['id']}",
                    "group": f"notify-{ev['category'] or 'allgemein'}",
                    "actions": [
                        {"action": f"NOTIFY_ACK_{ev['id']}", "title": "Quittieren"},
                        {"action": f"NOTIFY_SEEN_{ev['id']}", "title": "Gesehen"},
                        {"action": f"NOTIFY_ACKNOTE_{ev['id']}", "title": "Mit Kommentar",
                         "behavior": "textInput",
                         "textInputButtonTitle": "Quittieren",
                         "textInputPlaceholder": "Was wurde getan?"},
                    ],
                }
            if await self.call_service(target, body):
                self.core.store.set_forwarded(ev["id"], target)

    async def on_acked(self, ids: list[int]):
        """Nach dem Quittieren: HA-Event auslösen, Handy-Benachrichtigung entfernen."""
        opts = self.core.opts
        events = [e for e in (self.core.store.get(i) for i in ids[:200]) if e]
        for e in events:
            if opts["fire_events"]:
                await self.fire("notify_acknowledged", {
                    "id": e["id"], "title": e["title"], "level": e["level"],
                    "category": e["category"], "source": e["source"], "key": e["key"],
                    "by": e.get("acked_by") or "", "note": e.get("ack_note") or "",
                    "time": e["acked_time"]})
            target = e.get("forwarded") or ""
            if target.startswith("notify.mobile_app_"):
                await self.call_service(target, {"message": "clear_notification",
                                                 "data": {"tag": f"notify-{e['id']}"}})

    # ---- Websocket: auf HA-Events hören --------------------------------
    async def _ws_loop(self):
        opts = self.core.opts
        listen = {opts["event_type"]: "new", opts["ack_event_type"]: "ack",
                  opts.get("seen_event_type") or "notify_seen": "seen"}
        if opts.get("mobile_actions", True):
            listen[MOBILE_ACTION_EVENT] = "mobile"
        for loop_event in OUTGOING_EVENTS & set(listen):
            log.error("Event-Typ '%s' wird von Notify selbst ausgelöst (Schleife) – ignoriert",
                      loop_event)
            listen.pop(loop_event)
        while True:
            try:
                async with self.session.ws_connect(CORE_WS, heartbeat=30,
                                                   timeout=ClientTimeout(total=None)) as ws:
                    await ws.receive_json()  # auth_required
                    await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError(f"Authentifizierung fehlgeschlagen: {msg}")
                    for i, event_type in enumerate(listen, start=1):
                        await ws.send_json({"id": i, "type": "subscribe_events",
                                            "event_type": event_type})
                    self.connected = True
                    log.info("Mit Home Assistant verbunden – höre auf %s",
                             ", ".join(listen))
                    self.changed()
                    async for m in ws:
                        if m.type == WSMsgType.TEXT:
                            data = json.loads(m.data)
                            if data.get("type") == "event":
                                await self._handle(listen, data["event"])
                            elif data.get("type") == "result" and not data.get("success"):
                                log.warning("Abo fehlgeschlagen: %s", data)
                        elif m.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                log.warning("Websocket zu Home Assistant getrennt: %s", err)
            self.connected = False
            await asyncio.sleep(10)

    async def _handle(self, listen: dict, event: dict):
        kind = listen.get(event.get("event_type"))
        data = event.get("data") or {}
        if kind == "new":
            try:
                await self.core.add_event(data, default_source="Home Assistant")
            except ValueError as err:
                log.warning("Ungültiges Ereignis aus HA verworfen: %s (%s)", err, data)
            return
        user = await self.user_name((event.get("context") or {}).get("user_id"))
        try:
            if kind == "ack":
                n = self.core.acknowledge(data, by=user or "Home Assistant")
                log.debug("%s Ereignis(se) per HA-Event bearbeitet", n)
            elif kind == "seen":
                self.core.seen_request(data, by=user or "Home Assistant")
            elif kind == "mobile":
                m = MOBILE_ACTION_RE.fullmatch(str(data.get("action", "")))
                if not m:
                    return  # Aktion gehört nicht zu Notify
                action, event_id = m.group(1), int(m.group(2))
                by = user or "Handy"
                if action == "SEEN":
                    self.core.seen([event_id], by=by)
                else:
                    self.core.ack([event_id], by=by,
                                  note=str(data.get("reply_text") or "")[:500])
        except (TypeError, ValueError) as err:
            log.warning("Ungültige Daten in %s: %s (%s)", event.get("event_type"), err, data)


# --------------------------------------------------------------------------
# Kern
# --------------------------------------------------------------------------
class NotifyCore:
    def __init__(self, opts: dict):
        self.opts = opts
        self.categories = parse_categories(opts)
        self.store = Store(DB_FILE)
        self.ha = HomeAssistant(self)
        self.started = time.time()
        self.monitors: list[Monitor] = []
        seen = set()
        for cfg in opts.get("monitors") or []:
            try:
                m = Monitor(self, cfg)
            except (KeyError, ValueError, TypeError) as err:
                log.error("Monitor-Eintrag ungültig (%s): %s", err, cfg)
                continue
            if m.slug in seen:
                log.error("Monitor-Name doppelt: %s – ignoriert", m.name)
                continue
            seen.add(m.slug)
            self.monitors.append(m)
        self._monitor_session: ClientSession | None = None

    def category_cfg(self, name: str) -> dict | None:
        return next((c for c in self.categories if c["name"] == name), None)

    def all_categories(self, stats: dict | None = None) -> list[dict]:
        """Konfigurierte Kategorien + solche, die nur in Ereignissen vorkommen."""
        stats = stats or self.store.stats()
        result = list(self.categories)
        known = {c["name"] for c in result}
        for i, name in enumerate(stats["categories"]):
            if name not in known:
                result.append({
                    "name": name,
                    "label": "Ohne Kategorie" if name == NO_CATEGORY else name.capitalize(),
                    "icon": "mdi:tag-outline",
                    "color": "#9aa1ab" if name == NO_CATEGORY else PALETTE[(len(result) + i) % len(PALETTE)],
                    "forward_service": "", "forward_min_level": "",
                    "slug": slugify(name), "configured": False})
        return result

    async def add_event(self, payload, default_source: str = "",
                        closed_by: str | None = None) -> tuple[dict, bool]:
        ev = normalize(payload, default_source, self.opts.get("default_category") or "")
        row, is_new = self.store.add(ev)
        if closed_by:
            self.store.ack([row["id"]], by=closed_by)
            row = self.store.get(row["id"])
        log.info("%s [%s%s] %s%s", "Neu" if is_new else f"Wiederholt ×{row['count']}",
                 row["level"], f"/{row['category']}" if row["category"] else "",
                 row["title"], f" ({row['source']})" if row["source"] else "")
        self.ha.changed()
        if self.ha.active:
            asyncio.create_task(self.ha.on_new(row, is_new, forward=not closed_by))
        return row, is_new

    # ---- Gesehen / Quittieren -------------------------------------------
    def ack(self, ids=None, *, key=None, category=None, by="", note="") -> int:
        affected = self.store.ack(ids, key=key, category=category, by=by, note=note)
        self._after_ack(affected, by, note)
        return len(affected)

    def seen(self, ids=None, *, key=None, category=None, by="") -> int:
        close_below = level_rank(norm_level(self.opts.get("ack_min_level"), "warning"))
        seen, closed = self.store.mark_seen(ids, key=key, category=category, by=by,
                                            close_below=close_below)
        if seen:
            log.debug("%s Ereignis(se) gesehen von %s (%s davon damit erledigt)",
                      len(seen), by or "?", len(closed))
            self.ha.changed()
        if closed and self.ha.active:
            asyncio.create_task(self.ha.on_acked(closed))
        return len(seen)

    def reopen(self, ids=None, *, key=None, category=None, by="") -> int:
        affected = self.store.reopen(ids, key=key, category=category)
        if affected:
            log.info("%s Ereignis(se) wieder geöffnet von %s", len(affected), by or "?")
            self.ha.changed()
        return len(affected)

    def _after_ack(self, affected: list[int], by: str, note: str):
        if not affected:
            return
        log.info("%s Ereignis(se) quittiert von %s%s", len(affected), by or "?",
                 f" – „{note}“" if note else "")
        self.ha.changed()
        if self.ha.active:
            asyncio.create_task(self.ha.on_acked(affected))

    @staticmethod
    def _selector(data: dict) -> tuple[list[int] | None, str | None, str | None]:
        ids = data.get("ids", data.get("id"))
        if ids in (None, "", "all"):
            ids = None
        elif not isinstance(ids, list):
            ids = [ids]
        if ids is not None:
            ids = [int(i) for i in ids]
        key = str(data["key"]) if data.get("key") else None
        category = data.get("category")
        category = None if category in (None, "") else str(category)
        return ids, key, category

    def seen_request(self, data: dict, by: str = "") -> int:
        ids, key, category = self._selector(data)
        return self.seen(ids, key=key, category=category,
                         by=str(data.get("by") or by)[:60])

    def acknowledge(self, data: dict, by: str = "") -> int:
        """Gemeinsame Logik für REST und HA-Event: ids / key / category / alle.

        ``read: false`` oder ``reopen: true`` öffnet wieder, ``delete: true`` löscht.
        """
        ids, key, category = self._selector(data)
        by = str(data.get("by") or by)[:60]
        if data.get("delete"):
            if key:
                ids = [r[0] for r in self.store.db.execute(
                    "SELECT id FROM events WHERE key=?", (key,))]
                n = self.store.delete(ids=ids)
            else:
                n = self.store.delete(ids=ids, category=category,
                                      read_only=bool(data.get("read_only")),
                                      everything=ids is None and category is None
                                      and not data.get("read_only"))
            self.ha.changed()
        elif data.get("read") is False or data.get("reopen"):
            n = self.reopen(ids, key=key, category=category, by=by)
        else:
            n = self.ack(ids, key=key, category=category, by=by,
                         note=str(data.get("note") or "")[:500])
        return n

    async def start_monitors(self):
        if not self.monitors:
            return
        self._monitor_session = ClientSession(
            headers={"User-Agent": f"HA-Notify-Monitor/{VERSION}"})
        for m in self.monitors:
            asyncio.create_task(m.run(self._monitor_session))
        log.info("%s Web-Monitor(e) aktiv: %s", len(self.monitors),
                 ", ".join(f"{m.name} ({m.interval}s)" for m in self.monitors))

    async def prune_loop(self):
        while True:
            try:
                n = self.store.prune(int(self.opts["retention_days"]),
                                     int(self.opts["max_events"]))
                if n:
                    log.info("%s alte Ereignisse entfernt", n)
                    self.ha.changed()
            except Exception as err:  # noqa: BLE001
                log.warning("Aufräumen fehlgeschlagen: %s", err)
            await asyncio.sleep(3600)

    # ---- Zugriffskontrolle --------------------------------------------
    @staticmethod
    def trusted(request: web.Request) -> bool:
        try:
            return any(ipaddress.ip_address(request.remote or "") in net
                       for net in TRUSTED_NETS)
        except ValueError:
            return False

    def who(self, request: web.Request, body: dict | None = None) -> str:
        """Wer handelt? Über Ingress liefert HA den angemeldeten Benutzer mit."""
        if body and body.get("by"):
            return str(body["by"])[:60]
        if self.trusted(request):
            return (request.headers.get("X-Remote-User-Display-Name")
                    or request.headers.get("X-Remote-User-Name")
                    or "Weboberfläche")
        return "API"

    def authorized(self, request: web.Request) -> bool:
        if self.trusted(request):
            return True
        token = (self.opts.get("api_token") or "").strip()
        if not token:
            return False
        given = request.headers.get("Authorization", "")
        given = given[7:] if given.lower().startswith("bearer ") else ""
        given = given or request.headers.get("X-Api-Key") or request.query.get("token", "")
        return given == token


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def build_app(core: NotifyCore) -> web.Application:
    routes = web.RouteTableDef()

    def bad(msg: str):
        return web.HTTPBadRequest(text=json.dumps({"error": msg}),
                                  content_type="application/json")

    async def read_body(request: web.Request) -> dict:
        if request.content_type == "application/json":
            try:
                body = await request.json()
            except json.JSONDecodeError as err:
                raise bad(f"Ungültiges JSON: {err}")
            return body if isinstance(body, dict) else {"_invalid": body}
        if request.can_read_body:
            post = await request.post()
            if post:
                return dict(post)
            text = (await request.text()).strip()
            return {"message": text} if text else {}
        return {}

    def csv(value: str | None) -> list[str]:
        return [v for v in (value or "").split(",") if v]

    @routes.get("/")
    async def index(_):
        return web.FileResponse(STATIC_DIR / "index.html",
                                headers={"Cache-Control": "no-store"})

    @routes.get("/api/health")
    async def health(_):
        return web.json_response({"ok": True, "version": VERSION,
                                  "ha_connected": core.ha.connected,
                                  "uptime": int(time.time() - core.started)})

    @routes.get("/api/config")
    async def config(_):
        return web.json_response({
            "version": VERSION, "levels": LEVELS, "sorts": SORTS,
            "categories": [{k: c[k] for k in ("name", "label", "icon", "color", "slug")}
                           | {"configured": c.get("configured", True)}
                           for c in core.all_categories()],
            "default_category": core.opts.get("default_category") or "",
            "monitors": len(core.monitors),
            "ack_min_level": norm_level(core.opts.get("ack_min_level"), "warning"),
        })

    @routes.get("/api/whoami")
    async def whoami(request: web.Request):
        return web.json_response({"name": core.who(request)})

    @routes.get("/api/events")
    async def list_events(request: web.Request):
        q = request.query
        sort = q.get("sort", "new")
        if sort not in SORTS:
            raise bad(f"sort muss eins von {', '.join(SORTS)} sein")
        try:
            limit = max(1, min(int(q.get("limit", 50)), 500))
            offset = max(0, int(q.get("offset", 0)))
            since = float(q["since"]) if q.get("since") else None
        except ValueError:
            raise bad("Ungültige Parameter")
        events, total = core.store.query(
            limit=limit, offset=offset,
            levels=[lv for lv in csv(q.get("level")) if lv in LEVELS],
            categories=[norm_category(c) if c != NO_CATEGORY else c
                        for c in csv(q.get("category"))],
            tags=csv(q.get("tag")),
            source=q.get("source") or None,
            unread=q.get("unread") in ("1", "true"),
            statuses=[s for s in csv(q.get("status")) if s in STATUSES],
            q=q.get("q") or None, since=since, sort=sort,
            category_order=[c["name"] for c in core.categories])
        return web.json_response({"events": events, "total": total,
                                  "stats": core.store.stats()})

    @routes.get("/api/stats")
    async def stats(_):
        return web.json_response(core.store.stats())

    @routes.get("/api/monitors")
    async def monitors(_):
        return web.json_response([m.as_dict() for m in core.monitors])

    @routes.post("/api/monitors/{slug}/check")
    async def monitor_check(request: web.Request):
        m = next((m for m in core.monitors if m.slug == request.match_info["slug"]), None)
        if not m:
            raise web.HTTPNotFound(text='{"error":"Monitor nicht gefunden"}',
                                   content_type="application/json")
        m.wake.set()
        await asyncio.sleep(0)
        return web.json_response({"ok": True})

    @routes.get("/api/events/{id:\\d+}")
    async def get_event(request: web.Request):
        ev = core.store.get(int(request.match_info["id"]))
        if not ev:
            raise web.HTTPNotFound(text='{"error":"Nicht gefunden"}',
                                   content_type="application/json")
        return web.json_response(ev)

    async def create(payload: dict):
        try:
            row, is_new = await core.add_event(payload)
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        return web.json_response({"ok": True, "new": is_new, "event": row},
                                 status=201 if is_new else 200)

    @routes.post("/api/events")
    @routes.post("/api/event")
    @routes.post("/api/notify")
    async def post_event(request: web.Request):
        return await create(await read_body(request))

    @routes.get("/api/notify")
    async def get_notify(request: web.Request):
        """Einfacher Weg für Geräte, die nur GET können: /api/notify?title=..&message=.."""
        return await create({k: v for k, v in request.query.items() if k != "token"})

    # ---- Gesehen / Quittieren / Wieder öffnen --------------------------
    @routes.post("/api/events/ack")
    @routes.post("/api/events/read")          # Kompatibilität 1.x
    async def ack_many(request: web.Request):
        body = await read_body(request)
        try:
            n = core.acknowledge({**body, "delete": False}, by=core.who(request, body))
        except (TypeError, ValueError):
            raise bad("Ungültige ids")
        return web.json_response({"ok": True, "changed": n})

    @routes.post("/api/events/seen")
    async def seen_many(request: web.Request):
        body = await read_body(request)
        try:
            n = core.seen_request(body, by=core.who(request, body))
        except (TypeError, ValueError):
            raise bad("Ungültige ids")
        return web.json_response({"ok": True, "changed": n})

    @routes.post("/api/events/{id:\\d+}/{action:ack|read|seen|reopen}")
    async def act_one(request: web.Request):
        body = await read_body(request)
        event_id = int(request.match_info["id"])
        action = request.match_info["action"]
        by = core.who(request, body)
        if action == "read":                  # Kompatibilität 1.x
            action = "reopen" if body.get("read") is False else "ack"
        if action == "ack":
            n = core.ack([event_id], by=by, note=str(body.get("note") or "")[:500])
        elif action == "seen":
            n = core.seen([event_id], by=by)
        else:
            n = core.reopen([event_id], by=by)
        return web.json_response({"ok": True, "changed": n,
                                  "event": core.store.get(event_id)})

    @routes.delete("/api/events/{id:\\d+}")
    async def delete_one(request: web.Request):
        n = core.store.delete(ids=[int(request.match_info["id"])])
        core.ha.changed()
        return web.json_response({"ok": True, "deleted": n})

    @routes.post("/api/events/delete")
    async def delete_many(request: web.Request):
        body = await read_body(request)
        ids = body.get("ids", body.get("id"))
        try:
            ids = None if ids is None else [int(i) for i in (ids if isinstance(ids, list) else [ids])]
        except (TypeError, ValueError):
            raise bad("Ungültige ids")
        category = body.get("category")
        n = core.store.delete(ids=ids, read_only=bool(body.get("read_only")),
                              everything=bool(body.get("all")),
                              category=None if category in (None, "") else str(category))
        core.ha.changed()
        return web.json_response({"ok": True, "deleted": n})

    @routes.get("/api/export")
    async def export(_):
        events, _ = core.store.query(limit=10_000_000)
        return web.json_response(
            events, headers={"Content-Disposition":
                             f'attachment; filename="notify-{datetime.now():%Y%m%d-%H%M}.json"'})

    @web.middleware
    async def auth_mw(request: web.Request, handler):
        if request.path != "/api/health" and not core.authorized(request):
            log.debug("Abgewiesen: %s %s von %s", request.method, request.path, request.remote)
            return web.json_response({"error": "Nicht autorisiert"}, status=401)
        return await handler(request)

    app = web.Application(middlewares=[auth_mw], client_max_size=1024 * 1024)
    app.add_routes(routes)
    return app


async def main():
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, str(opts["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    core = NotifyCore(opts)
    runner = web.AppRunner(build_app(core), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, port=PORT).start()
    log.info("Notify %s läuft auf Port %s (%s Ereignisse, Kategorien: %s)",
             VERSION, PORT, core.store.stats()["total"],
             ", ".join(c["name"] for c in core.categories) or "–")
    if not (opts.get("api_token") or "").strip():
        log.info("Kein api_token gesetzt – Zugriffe von außerhalb von HA werden abgewiesen")

    await core.ha.start()
    await core.start_monitors()
    asyncio.create_task(core.prune_loop())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
