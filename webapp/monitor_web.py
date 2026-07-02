#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_SERVER = "192.168.2.200"
DEFAULT_NETWORK = "TM"
DEFAULT_CHANNEL = "HZ"

SCORE = {"missing": -1, "red": 0, "yellow": 1, "green": 2}
STATUS_FROM_SCORE = {value: key for key, value in SCORE.items()}


@dataclass(frozen=True)
class Config:
    seedlink_server: str
    network: str
    channel: str
    poll_seconds: int
    slot_minutes: int
    ok_lag_minutes: float
    warn_lag_minutes: float
    timezone_mode: str
    db_path: Path
    shell_command: str
    mock_file: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Web dashboard for SeedLink TM/HZ monitoring.")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address. Use 0.0.0.0 for LAN access.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--server", default=os.environ.get("SLINK_SERVER", DEFAULT_SERVER))
    parser.add_argument("--network", default=os.environ.get("SLINK_NETWORK", DEFAULT_NETWORK))
    parser.add_argument("--channel", default=os.environ.get("SLINK_CHANNEL", DEFAULT_CHANNEL))
    parser.add_argument("--poll-seconds", type=int, default=int(os.environ.get("SLINK_POLL_SECONDS", "60")))
    parser.add_argument("--slot-minutes", type=int, default=int(os.environ.get("SLINK_SLOT_MINUTES", "15")))
    parser.add_argument("--ok-lag-minutes", type=float, default=float(os.environ.get("SLINK_OK_LAG_MINUTES", "15")))
    parser.add_argument("--warn-lag-minutes", type=float, default=float(os.environ.get("SLINK_WARN_LAG_MINUTES", "30")))
    parser.add_argument("--time-zone", choices=("utc", "local"), default=os.environ.get("SLINK_TIME_ZONE", "utc"))
    parser.add_argument("--db", default=os.environ.get("SLINK_DB", str(ROOT / "data" / "monitor.db")))
    parser.add_argument("--shell-command", default=os.environ.get("SLINK_SHELL_COMMAND", ""))
    parser.add_argument("--mock-file", default=os.environ.get("SLINK_MOCK_FILE", ""))
    parser.add_argument("--no-poll", action="store_true", help="Start API without background polling.")
    return parser.parse_args()


def now_for_mode(mode: str) -> datetime:
    if mode == "utc":
        return datetime.now(timezone.utc)
    return datetime.now().astimezone()


def parse_datetime_token(text: str, mode: str) -> Optional[datetime]:
    text = text.replace("T", " ").replace("Z", "+00:00")
    patterns = [
        r"\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2})?",
        r"\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?:[+-]\d{2}:?\d{2})?",
    ]
    found: List[datetime] = []
    for pattern in patterns:
        for match in re.findall(pattern, text):
            candidate = match.replace("/", "-")
            if re.search(r"[+-]\d{4}$", candidate):
                candidate = candidate[:-5] + candidate[-5:-2] + ":" + candidate[-2:]
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                if mode == "utc":
                    parsed = parsed.replace(tzinfo=timezone.utc)
                else:
                    parsed = parsed.astimezone()
            found.append(parsed)
    return max(found) if found else None


def find_station(line: str, network_filter: str) -> Optional[str]:
    upper = line.upper()
    parts = re.split(r"[\s._,-]+", upper)
    for idx, part in enumerate(parts[:-1]):
        if part == network_filter.upper() and re.fullmatch(r"[A-Z0-9]{3,6}", parts[idx + 1]):
            return parts[idx + 1]
    return None


def latest_by_station(
    lines: Iterable[str], mode: str, network_filter: str
) -> Tuple[Dict[str, datetime], Dict[str, int]]:
    latest: Dict[str, datetime] = {}
    counts: Dict[str, int] = {}
    for line in lines:
        station = find_station(line, network_filter)
        if not station:
            continue
        counts[station] = counts.get(station, 0) + 1
        dt = parse_datetime_token(line, mode)
        if dt and (station not in latest or dt > latest[station]):
            latest[station] = dt
    return latest, counts


def floor_slot(dt: datetime, slot_minutes: int) -> datetime:
    minute = (dt.minute // slot_minutes) * slot_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def status_for_age(age_minutes: Optional[float], ok_minutes: float, warn_minutes: float) -> str:
    if age_minutes is None:
        return "red"
    if age_minutes <= ok_minutes:
        return "green"
    if age_minutes <= warn_minutes:
        return "yellow"
    return "red"


def best_status(old_status: str, new_status: str) -> str:
    return STATUS_FROM_SCORE[max(SCORE.get(old_status, -1), SCORE.get(new_status, -1))]


def build_slots(current: datetime, slot_minutes: int, hours: int) -> List[datetime]:
    count = max(1, int((hours * 60) / slot_minutes) + 1)
    base = floor_slot(current, slot_minutes)
    return [base - timedelta(minutes=slot_minutes * offset) for offset in range(count - 1, -1, -1)]


class MonitorStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS poll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poll_time TEXT NOT NULL,
                    server TEXT NOT NULL,
                    network TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS stations (
                    station TEXT PRIMARY KEY,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    latest_packet_time TEXT,
                    age_minutes REAL
                );
                CREATE TABLE IF NOT EXISTS station_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poll_id INTEGER NOT NULL,
                    poll_time TEXT NOT NULL,
                    slot_start TEXT NOT NULL,
                    station TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latest_packet_time TEXT,
                    age_minutes REAL,
                    raw_line_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(poll_id) REFERENCES poll_runs(id)
                );
                CREATE TABLE IF NOT EXISTS slot_summaries (
                    slot_start TEXT NOT NULL,
                    station TEXT NOT NULL,
                    slot_status TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    green_count INTEGER NOT NULL DEFAULT 0,
                    yellow_count INTEGER NOT NULL DEFAULT 0,
                    red_count INTEGER NOT NULL DEFAULT 0,
                    missing_count INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    latest_packet_time TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(slot_start, station)
                );
                CREATE INDEX IF NOT EXISTS idx_station_status_poll_time ON station_status(poll_time);
                CREATE INDEX IF NOT EXISTS idx_station_status_station_time ON station_status(station, poll_time);
                CREATE INDEX IF NOT EXISTS idx_slot_summaries_slot ON slot_summaries(slot_start);
                """
            )

    def insert_poll_run(self, conn: sqlite3.Connection, current: datetime, config: Config, success: bool, error: str = "") -> int:
        cur = conn.execute(
            """
            INSERT INTO poll_runs (poll_time, server, network, channel, success, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (current.isoformat(), config.seedlink_server, config.network, config.channel, 1 if success else 0, error),
        )
        return int(cur.lastrowid)

    def known_stations(self, conn: sqlite3.Connection) -> List[str]:
        rows = conn.execute("SELECT station FROM stations ORDER BY station").fetchall()
        return [str(row["station"]) for row in rows]

    def record_success(
        self,
        current: datetime,
        config: Config,
        latest: Dict[str, datetime],
        raw_counts: Dict[str, int],
    ) -> None:
        slot_start = floor_slot(current, config.slot_minutes).isoformat()
        with self.lock, self.connect() as conn:
            poll_id = self.insert_poll_run(conn, current, config, True)
            known = set(self.known_stations(conn))
            stations = sorted(known | set(latest))

            for station in stations:
                latest_dt = latest.get(station)
                age = None
                if latest_dt:
                    raw_age = (current - latest_dt.astimezone(current.tzinfo)).total_seconds() / 60.0
                    age = max(0.0, raw_age)
                status = status_for_age(age, config.ok_lag_minutes, config.warn_lag_minutes)
                latest_iso = latest_dt.isoformat() if latest_dt else None
                raw_count = raw_counts.get(station, 0)

                conn.execute(
                    """
                    INSERT INTO station_status
                    (poll_id, poll_time, slot_start, station, status, latest_packet_time, age_minutes, raw_line_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (poll_id, current.isoformat(), slot_start, station, status, latest_iso, age, raw_count),
                )

                existing = conn.execute("SELECT station FROM stations WHERE station = ?", (station,)).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE stations
                        SET last_seen = ?, last_status = ?, latest_packet_time = ?, age_minutes = ?
                        WHERE station = ?
                        """,
                        (current.isoformat(), status, latest_iso, age, station),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO stations
                        (station, first_seen, last_seen, last_status, latest_packet_time, age_minutes)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (station, current.isoformat(), current.isoformat(), status, latest_iso, age),
                    )

                summary = conn.execute(
                    """
                    SELECT * FROM slot_summaries WHERE slot_start = ? AND station = ?
                    """,
                    (slot_start, station),
                ).fetchone()
                if summary:
                    green = int(summary["green_count"]) + (1 if status == "green" else 0)
                    yellow = int(summary["yellow_count"]) + (1 if status == "yellow" else 0)
                    red = int(summary["red_count"]) + (1 if status == "red" else 0)
                    missing = int(summary["missing_count"]) + (1 if status == "missing" else 0)
                    total = int(summary["total_count"]) + 1
                    slot_status = best_status(str(summary["slot_status"]), status)
                    conn.execute(
                        """
                        UPDATE slot_summaries
                        SET slot_status = ?, last_status = ?, green_count = ?, yellow_count = ?,
                            red_count = ?, missing_count = ?, total_count = ?, latest_packet_time = ?,
                            updated_at = ?
                        WHERE slot_start = ? AND station = ?
                        """,
                        (
                            slot_status,
                            status,
                            green,
                            yellow,
                            red,
                            missing,
                            total,
                            latest_iso or summary["latest_packet_time"],
                            current.isoformat(),
                            slot_start,
                            station,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO slot_summaries
                        (slot_start, station, slot_status, last_status, green_count, yellow_count,
                         red_count, missing_count, total_count, latest_packet_time, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            slot_start,
                            station,
                            status,
                            status,
                            1 if status == "green" else 0,
                            1 if status == "yellow" else 0,
                            1 if status == "red" else 0,
                            1 if status == "missing" else 0,
                            1,
                            latest_iso,
                            current.isoformat(),
                        ),
                    )

    def record_error(self, current: datetime, config: Config, error: str) -> None:
        with self.lock, self.connect() as conn:
            self.insert_poll_run(conn, current, config, False, error)

    def latest_poll(self) -> Optional[dict]:
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def latest_stations(self) -> List[dict]:
        with self.lock, self.connect() as conn:
            rows = conn.execute("SELECT * FROM stations ORDER BY station").fetchall()
            return [dict(row) for row in rows]

    def slot_dashboard(self, config: Config, hours: int) -> dict:
        current = now_for_mode(config.timezone_mode)
        slot_times = build_slots(current, config.slot_minutes, hours)
        slot_keys = [slot.isoformat() for slot in slot_times]

        with self.lock, self.connect() as conn:
            stations = [dict(row) for row in conn.execute("SELECT * FROM stations ORDER BY station").fetchall()]
            summaries = conn.execute(
                """
                SELECT * FROM slot_summaries
                WHERE slot_start >= ? AND slot_start <= ?
                ORDER BY station, slot_start
                """,
                (slot_keys[0], slot_keys[-1]),
            ).fetchall()

        summary_map = {(row["station"], row["slot_start"]): dict(row) for row in summaries}
        rows = []
        for station in stations:
            cells = []
            for slot in slot_keys:
                summary = summary_map.get((station["station"], slot))
                if summary:
                    total = max(int(summary["total_count"]), 1)
                    summary["green_pct"] = round((int(summary["green_count"]) / total) * 100, 2)
                    summary["healthy_pct"] = round(((int(summary["green_count"]) + int(summary["yellow_count"])) / total) * 100, 2)
                cells.append(summary or {"slot_start": slot, "station": station["station"], "slot_status": "missing"})
            rows.append({**station, "cells": cells})

        counts = {"green": 0, "yellow": 0, "red": 0, "missing": 0}
        for station in stations:
            counts[station.get("last_status") or "missing"] = counts.get(station.get("last_status") or "missing", 0) + 1

        return {
            "generated_at": current.isoformat(),
            "config": public_config(config),
            "summary": counts,
            "slots": [{"start": key, "label": datetime.fromisoformat(key).strftime("%H:%M")} for key in slot_keys],
            "rows": rows,
            "last_poll": self.latest_poll(),
        }

    def station_history(self, station: str, hours: int) -> dict:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self.lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM station_status
                WHERE station = ? AND poll_time >= ?
                ORDER BY poll_time DESC
                LIMIT 500
                """,
                (station.upper(), since.isoformat()),
            ).fetchall()
        return {"station": station.upper(), "records": [dict(row) for row in rows]}

    def export_rows(self, hours: int) -> List[dict]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self.lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT poll_time, slot_start, station, status, latest_packet_time, age_minutes, raw_line_count
                FROM station_status
                WHERE poll_time >= ?
                ORDER BY poll_time, station
                """,
                (since.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]


class MonitorService:
    def __init__(self, config: Config, store: MonitorStore) -> None:
        self.config = config
        self.store = store
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run_loop, name="slink-monitor-poller", daemon=True)
        self.thread.start()

    def run_loop(self) -> None:
        while not self.stop_event.is_set():
            self.poll_once()
            self.stop_event.wait(max(5, self.config.poll_seconds))

    def run_query(self) -> List[str]:
        if self.config.mock_file:
            return Path(self.config.mock_file).read_text(encoding="utf-8").splitlines()
        if self.config.shell_command:
            completed = subprocess.run(
                self.config.shell_command,
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            completed = subprocess.run(
                ["slinktool", "-Q", self.config.seedlink_server],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "slinktool returned a non-zero code")
        return completed.stdout.splitlines()

    def poll_once(self) -> None:
        current = now_for_mode(self.config.timezone_mode)
        try:
            lines = self.run_query()
            network = self.config.network.upper()
            channel = self.config.channel.upper()
            filtered = [line for line in lines if network in line.upper() and channel in line.upper() and line.strip()]
            latest, counts = latest_by_station(filtered, self.config.timezone_mode, self.config.network)
            self.store.record_success(current, self.config, latest, counts)
        except Exception as exc:
            self.store.record_error(current, self.config, str(exc))

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)


def public_config(config: Config) -> dict:
    return {
        "server": config.seedlink_server,
        "network": config.network,
        "channel": config.channel,
        "poll_seconds": config.poll_seconds,
        "slot_minutes": config.slot_minutes,
        "ok_lag_minutes": config.ok_lag_minutes,
        "warn_lag_minutes": config.warn_lag_minutes,
        "timezone_mode": config.timezone_mode,
    }


def parse_int(values: Dict[str, List[str]], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(values.get(key, [str(default)])[0])
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


class AppHandler(BaseHTTPRequestHandler):
    store: MonitorStore
    service: MonitorService
    config: Config

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path

        if path == "/api/health":
            last_poll = self.store.latest_poll()
            self.send_json({"ok": bool(last_poll and last_poll["success"]), "last_poll": last_poll, "config": public_config(self.config)})
            return
        if path == "/api/status/latest":
            self.send_json({"stations": self.store.latest_stations(), "last_poll": self.store.latest_poll(), "config": public_config(self.config)})
            return
        if path == "/api/status/slots":
            hours = parse_int(params, "hours", 8, 1, 168)
            self.send_json(self.store.slot_dashboard(self.config, hours))
            return
        if path == "/api/stations":
            self.send_json({"stations": self.store.latest_stations()})
            return
        if path.startswith("/api/stations/") and path.endswith("/history"):
            station = unquote(path.split("/")[3])
            hours = parse_int(params, "hours", 24, 1, 720)
            self.send_json(self.store.station_history(station, hours))
            return
        if path == "/api/export.csv":
            hours = parse_int(params, "hours", 24, 1, 720)
            rows = self.store.export_rows(hours)
            output = []
            fieldnames = ["poll_time", "slot_start", "station", "status", "latest_packet_time", "age_minutes", "raw_line_count"]
            output.append(",".join(fieldnames))
            for row in rows:
                output.append(",".join(csv_escape(row.get(field, "")) for field in fieldnames))
            self.send_text("\n".join(output) + "\n", "text/csv; charset=utf-8")
            return

        self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/poll-now":
            self.service.poll_once()
            self.send_json({"ok": True, "last_poll": self.store.latest_poll()})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def serve_static(self, path: str) -> None:
        if path == "/":
            file_path = STATIC_DIR / "index.html"
        else:
            relative = path.lstrip("/")
            file_path = (STATIC_DIR / relative).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if file_path.suffix in {".html", ".css", ".js"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def csv_escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in [",", '"', "\n", "\r"]):
        return '"' + text.replace('"', '""') + '"'
    return text


def make_config(args: argparse.Namespace) -> Config:
    return Config(
        seedlink_server=args.server,
        network=args.network,
        channel=args.channel,
        poll_seconds=max(5, args.poll_seconds),
        slot_minutes=max(1, args.slot_minutes),
        ok_lag_minutes=args.ok_lag_minutes,
        warn_lag_minutes=args.warn_lag_minutes,
        timezone_mode=args.time_zone,
        db_path=Path(args.db).expanduser(),
        shell_command=args.shell_command,
        mock_file=args.mock_file,
    )


def main() -> int:
    args = parse_args()
    config = make_config(args)
    store = MonitorStore(config.db_path)
    service = MonitorService(config, store)
    AppHandler.store = store
    AppHandler.service = service
    AppHandler.config = config

    if not args.no_poll:
        service.poll_once()
        service.start()

    httpd = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Serving TM/HZ monitor at http://{args.host}:{args.port}")
    print(f"Database: {config.db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping monitor web app...")
    finally:
        service.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
