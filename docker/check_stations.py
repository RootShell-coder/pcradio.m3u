"""Quick stream availability check for the final M3U playlist."""

import argparse
import concurrent.futures
import json
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from dl import parse_m3u, station_id_from_url

CONNECT_TIMEOUT_SECONDS = 5
FIRST_BYTE_TIMEOUT_SECONDS = 10
DEFAULT_WORKERS = 32
DEFAULT_WORKERS_PER_HOST = 4
VLC_USER_AGENT = "VLC/3.0.23 LibVLC/3.0.23"

_thread_local = threading.local()
_host_lock = threading.Lock()
_host_semaphores = {}


def get_session():
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": VLC_USER_AGENT,
                "Accept": "*/*",
                "Connection": "close",
            }
        )
        _thread_local.session = session
    return session


def get_host_semaphore(url, workers_per_host):
    host = (urlsplit(url).hostname or "").lower()
    with _host_lock:
        semaphore = _host_semaphores.get(host)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(workers_per_host)
            _host_semaphores[host] = semaphore
    return semaphore


def check_station(station, workers_per_host):
    url = station["stream"]
    station_id = station_id_from_url(url)
    semaphore = get_host_semaphore(url, workers_per_host)
    try:
        with semaphore:
            with get_session().get(
                url,
                allow_redirects=True,
                stream=True,
                timeout=(CONNECT_TIMEOUT_SECONDS, FIRST_BYTE_TIMEOUT_SECONDS),
            ) as response:
                if not 200 <= response.status_code < 300:
                    return station_id, station, False
                first_chunk = next(response.iter_content(chunk_size=1), b"")
                return station_id, station, bool(first_chunk)
    except requests.RequestException:
        return station_id, station, False


def load_stations(filename):
    with open(filename, "r", encoding="utf-8-sig") as playlist_file:
        stations = parse_m3u(playlist_file)
    if not stations:
        raise RuntimeError("Playlist contains no stations")
    return stations


def load_previous_failures(filename):
    try:
        with open(filename, "r", encoding="utf-8") as counter_file:
            document = json.load(counter_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    failures = document.get("unavailable_stations", {})
    return failures if isinstance(failures, dict) else {}


def build_report(stations, previous_failures, workers, workers_per_host):
    unavailable = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(check_station, station, workers_per_host)
            for station in stations
        ]
        for completed, future in enumerate(
            concurrent.futures.as_completed(futures), start=1
        ):
            station_id, station, available = future.result()
            if not available:
                previous = previous_failures.get(station_id, {})
                unavailable[station_id] = {
                    "name": station["name"],
                    "url": station["stream"],
                    "consecutive_failures": int(
                        previous.get("consecutive_failures", 0)
                    )
                    + 1,
                }
            if completed % 100 == 0 or completed == len(stations):
                print(
                    f"Checked {completed}/{len(stations)}, "
                    f"unavailable: {len(unavailable)}",
                    file=sys.stderr,
                )

    unavailable = dict(sorted(unavailable.items()))
    return {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "total": len(stations),
            "available": len(stations) - len(unavailable),
            "unavailable": len(unavailable),
        },
        "unavailable_stations": unavailable,
    }


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("playlist")
    parser.add_argument("previous_report")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--workers-per-host", type=int, default=DEFAULT_WORKERS_PER_HOST
    )
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    stations = load_stations(arguments.playlist)
    previous = load_previous_failures(arguments.previous_report)
    report = build_report(
        stations,
        previous,
        max(1, arguments.workers),
        max(1, arguments.workers_per_host),
    )
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
