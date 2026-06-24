#!/usr/bin/env python3
"""
SeedLink station monitor for streams matching TM and HZ.

Default query is equivalent to:
    slinktool -Q 192.168.2.200 | grep TM | grep HZ

The screen keeps a rolling history of 15-minute slots. By default it shows every
station found in streams matching TM and HZ. Pass --stations only when you want
to limit the display to selected stations.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


DEFAULT_SERVER = "192.168.2.200"
DEFAULT_NETWORK_FILTER = "TM"
DEFAULT_CHANNEL_FILTER = "HZ"
DEFAULT_STATIONS: List[str] = []
DOT = "\u25cf"

SCORE = {"missing": -1, "red": 0, "yellow": 1, "green": 2}
STATUS_FROM_SCORE = {v: k for k, v in SCORE.items()}

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[90m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
}


def default_state_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "slink_hz_monitor" / "state.json"
    return Path.home() / ".local" / "state" / "slink_hz_monitor" / "state.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor SeedLink stations in 15-minute slots."
    )
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--network", default=DEFAULT_NETWORK_FILTER)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL_FILTER)
    parser.add_argument(
        "--stations",
        default=",".join(DEFAULT_STATIONS),
        help="Optional comma-separated station list. Empty means auto-detect all matching stations.",
    )
    parser.add_argument(
        "--slot-minutes",
        type=int,
        default=15,
        help="Slot size in minutes.",
    )
    parser.add_argument(
        "--slots",
        type=int,
        default=8,
        help="Number of recent slots to display.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=60,
        help="How often to query slinktool.",
    )
    parser.add_argument(
        "--ok-lag-minutes",
        type=float,
        default=15,
        help="Green when latest packet age is <= this many minutes.",
    )
    parser.add_argument(
        "--warn-lag-minutes",
        type=float,
        default=30,
        help="Yellow when latest packet age is <= this many minutes.",
    )
    parser.add_argument(
        "--time-zone",
        choices=("utc", "local"),
        default="utc",
        help="Treat slinktool timestamps as UTC or local time when no TZ is printed.",
    )
    parser.add_argument(
        "--label-format",
        default="%H:%M",
        help="Column label format, for example %%H or %%H:%%M.",
    )
    parser.add_argument(
        "--state",
        default=str(default_state_path()),
        help="JSON file used to keep 15-minute history.",
    )
    parser.add_argument(
        "--log-file",
        default="",
        help="Optional CSV file to append poll results continuously.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Query once, update the current slot, print, and exit.",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Use ASCII status marks instead of colored dots.",
    )
    parser.add_argument(
        "--shell-command",
        default="",
        help=(
            "Optional command to run instead of direct slinktool call, e.g. "
            "\"slinktool -Q 192.168.2.200 | grep TM | grep HZ\"."
        ),
    )
    return parser.parse_args()


def now_for_mode(mode: str) -> datetime:
    if mode == "utc":
        return datetime.now(timezone.utc)
    return datetime.now().astimezone()


def normalize_station_list(stations: str) -> List[str]:
    return [item.strip().upper() for item in stations.split(",") if item.strip()]


def run_query(args: argparse.Namespace) -> List[str]:
    if args.shell_command:
        completed = subprocess.run(
            args.shell_command,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    else:
        completed = subprocess.run(
            ["slinktool", "-Q", args.server],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    if completed.returncode != 0:
        message = completed.stderr.strip() or "slinktool returned a non-zero code"
        raise RuntimeError(message)

    lines = completed.stdout.splitlines()
    network = args.network.upper()
    channel = args.channel.upper()
    return [
        line
        for line in lines
        if network in line.upper() and channel in line.upper() and line.strip()
    ]


def compile_station_patterns(stations: Iterable[str]) -> Dict[str, re.Pattern[str]]:
    return {
        station: re.compile(rf"(?<![A-Z0-9]){re.escape(station)}(?![A-Z0-9])", re.I)
        for station in stations
    }


def find_station(
    line: str,
    stations: List[str],
    station_patterns: Dict[str, re.Pattern[str]],
    network_filter: str,
) -> Optional[str]:
    if stations:
        for station, pattern in station_patterns.items():
            if pattern.search(line):
                return station
        return None

    upper = line.upper()
    parts = re.split(r"[\s._,-]+", upper)
    for idx, part in enumerate(parts[:-1]):
        if part == network_filter.upper() and re.fullmatch(r"[A-Z0-9]{3,6}", parts[idx + 1]):
            return parts[idx + 1]
    return None


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

    if not found:
        return None
    return max(found)


def latest_by_station(
    lines: Iterable[str], station_filter: List[str], mode: str, network_filter: str
) -> Dict[str, datetime]:
    latest: Dict[str, datetime] = {}
    station_patterns = compile_station_patterns(station_filter)
    for line in lines:
        station = find_station(line, station_filter, station_patterns, network_filter)
        if not station:
            continue
        dt = parse_datetime_token(line, mode)
        if dt and (station not in latest or dt > latest[station]):
            latest[station] = dt
    return latest


def floor_slot(dt: datetime, slot_minutes: int) -> datetime:
    minute = (dt.minute // slot_minutes) * slot_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def slot_key(dt: datetime) -> str:
    return dt.isoformat()


def load_state(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_state(path: Path, state: Dict[str, Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def merge_status(old: Optional[str], new: str) -> str:
    if old is None:
        return new
    return STATUS_FROM_SCORE[max(SCORE.get(old, -1), SCORE[new])]


def status_for_age(age_minutes: Optional[float], ok_minutes: float, warn_minutes: float) -> str:
    if age_minutes is None or age_minutes < -1:
        return "red"
    if age_minutes <= ok_minutes:
        return "green"
    if age_minutes <= warn_minutes:
        return "yellow"
    return "red"


def station_age_minutes(
    latest: Dict[str, datetime], station: str, current: datetime
) -> Optional[float]:
    last_dt = latest.get(station)
    if last_dt is None:
        return None
    return (current - last_dt.astimezone(current.tzinfo)).total_seconds() / 60.0


def update_current_slot(
    state: Dict[str, Dict[str, str]],
    stations: List[str],
    latest: Dict[str, datetime],
    current: datetime,
    slot_minutes: int,
    ok_minutes: float,
    warn_minutes: float,
) -> None:
    current_slot = slot_key(floor_slot(current, slot_minutes))
    for station in stations:
        age = station_age_minutes(latest, station, current)
        new_status = status_for_age(age, ok_minutes, warn_minutes)
        station_state = state.setdefault(station, {})
        station_state[current_slot] = merge_status(station_state.get(current_slot), new_status)


def append_poll_log(
    log_file: str,
    stations: List[str],
    latest: Dict[str, datetime],
    current: datetime,
    args: argparse.Namespace,
    error: str = "",
) -> None:
    if not log_file:
        return

    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    slot_start = floor_slot(current, args.slot_minutes).isoformat()
    fieldnames = [
        "poll_time",
        "slot_start",
        "server",
        "network",
        "channel",
        "station",
        "status",
        "latest_packet_time",
        "age_minutes",
        "error",
    ]

    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        if error:
            writer.writerow(
                {
                    "poll_time": current.isoformat(),
                    "slot_start": slot_start,
                    "server": args.server,
                    "network": args.network,
                    "channel": args.channel,
                    "station": "",
                    "status": "error",
                    "latest_packet_time": "",
                    "age_minutes": "",
                    "error": error,
                }
            )
            return

        for station in stations:
            latest_dt = latest.get(station)
            age = station_age_minutes(latest, station, current)
            status = status_for_age(age, args.ok_lag_minutes, args.warn_lag_minutes)
            writer.writerow(
                {
                    "poll_time": current.isoformat(),
                    "slot_start": slot_start,
                    "server": args.server,
                    "network": args.network,
                    "channel": args.channel,
                    "station": station,
                    "status": status,
                    "latest_packet_time": latest_dt.isoformat() if latest_dt else "",
                    "age_minutes": f"{age:.2f}" if age is not None else "",
                    "error": "",
                }
            )


def trim_state(
    state: Dict[str, Dict[str, str]], current: datetime, slot_minutes: int, keep_slots: int
) -> None:
    earliest = floor_slot(current, slot_minutes) - timedelta(minutes=slot_minutes * keep_slots * 2)
    for station in list(state):
        station_state = state[station]
        for key in list(station_state):
            try:
                key_dt = datetime.fromisoformat(key)
            except ValueError:
                del station_state[key]
                continue
            if key_dt < earliest:
                del station_state[key]


def status_mark(status: str, ascii_mode: bool) -> str:
    if ascii_mode:
        return {"green": "OK", "yellow": "!!", "red": "XX", "missing": ".."}.get(status, "..")
    color = {
        "green": ANSI["green"],
        "yellow": ANSI["yellow"],
        "red": ANSI["red"],
        "missing": ANSI["dim"],
    }.get(status, ANSI["dim"])
    return f"{color}{DOT}{ANSI['reset']}"


def center_visible(text: str, width: int, visible_width: int) -> str:
    padding = max(0, width - visible_width)
    left = padding // 2
    right = padding - left
    return " " * left + text + " " * right


def build_slots(current: datetime, slot_minutes: int, count: int) -> List[datetime]:
    base = floor_slot(current, slot_minutes)
    return [
        base - timedelta(minutes=slot_minutes * offset)
        for offset in range(count - 1, -1, -1)
    ]


def clear_screen() -> None:
    if sys.stdout.isatty():
        os.system("")
        print("\033[2J\033[H", end="")


def configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def render(
    state: Dict[str, Dict[str, str]],
    stations: List[str],
    current: datetime,
    args: argparse.Namespace,
    error: str = "",
) -> None:
    slots = build_slots(current, args.slot_minutes, args.slots)
    labels = [slot.strftime(args.label_format) for slot in slots]
    station_width = max(7, *(len(station) for station in stations)) if stations else 7
    cell_width = max(5, max(len(label) for label in labels) + 2)

    clear_screen()
    print(f"{ANSI['bold']}SeedLink TM HZ Monitor{ANSI['reset']}")
    print(f"server={args.server}  slot={args.slot_minutes}m  updated={current:%Y-%m-%d %H:%M:%S %Z}")
    if error:
        print(f"{ANSI['red']}last error: {error}{ANSI['reset']}")
    print()
    print(" " * (station_width + 2) + "".join(label.center(cell_width) for label in labels))

    for station in stations:
        row = [station.ljust(station_width), "  "]
        station_state = state.get(station, {})
        for slot in slots:
            status = station_state.get(slot_key(slot), "missing")
            mark = status_mark(status, args.ascii)
            row.append(center_visible(mark, cell_width, 2 if args.ascii else 1))
        print("".join(row))

    print()
    if args.ascii:
        print(
            f"OK <= {args.ok_lag_minutes:g}m   "
            f"!! <= {args.warn_lag_minutes:g}m   "
            f"XX > {args.warn_lag_minutes:g}m/no data"
        )
    else:
        print(
            f"{ANSI['green']}{DOT}{ANSI['reset']} <= {args.ok_lag_minutes:g}m   "
            f"{ANSI['yellow']}{DOT}{ANSI['reset']} <= {args.warn_lag_minutes:g}m   "
            f"{ANSI['red']}{DOT}{ANSI['reset']} > {args.warn_lag_minutes:g}m/no data"
        )


def station_display_list(
    station_filter: List[str], state: Dict[str, Dict[str, str]], latest: Dict[str, datetime]
) -> List[str]:
    if station_filter:
        return station_filter
    return sorted(set(state) | set(latest))


def main() -> int:
    configure_stdout()
    args = parse_args()
    station_filter = normalize_station_list(args.stations)
    state_path = Path(args.state)
    state = load_state(state_path)
    last_error = ""

    while True:
        current = now_for_mode(args.time_zone)
        try:
            lines = run_query(args)
            latest = latest_by_station(lines, station_filter, args.time_zone, args.network)
            stations = station_display_list(station_filter, state, latest)
            update_current_slot(
                state,
                stations,
                latest,
                current,
                args.slot_minutes,
                args.ok_lag_minutes,
                args.warn_lag_minutes,
            )
            trim_state(state, current, args.slot_minutes, args.slots)
            save_state(state_path, state)
            append_poll_log(args.log_file, stations, latest, current, args)
            last_error = ""
        except Exception as exc:
            last_error = str(exc)
            stations = station_display_list(station_filter, state, {})
            append_poll_log(args.log_file, stations, {}, current, args, last_error)

        render(state, stations, current, args, last_error)

        if args.once:
            break
        time.sleep(max(5, args.poll_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
