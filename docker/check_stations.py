"""Quick stream availability check for the final M3U playlist."""

import argparse
import concurrent.futures
import contextlib
import json
import sys
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlsplit

import requests

from dl import parse_m3u, station_id_from_url

CONNECT_TIMEOUT_SECONDS = 3
FIRST_BYTE_TIMEOUT_SECONDS = 2
DEFAULT_WORKERS = 20
DEFAULT_WORKERS_PER_HOST = 2
HOST_REQUEST_INTERVAL_SECONDS = 0.25
MAX_REDIRECTS = 5
VLC_USER_AGENT = "VLC/3.0.23 LibVLC/3.0.23"

_thread_local = threading.local()
_host_lock = threading.Lock()
_host_semaphores = {}
_host_next_request = {}


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


def get_host(url):
    return (urlsplit(url).hostname or "").lower()


def get_host_semaphore(host, workers_per_host):
    with _host_lock:
        semaphore = _host_semaphores.get(host)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(workers_per_host)
            _host_semaphores[host] = semaphore
    return semaphore


@contextlib.contextmanager
def host_request_slot(url, workers_per_host):
    host = get_host(url)
    semaphore = get_host_semaphore(host, workers_per_host)
    semaphore.acquire()
    try:
        with _host_lock:
            now = time.monotonic()
            request_time = max(now, _host_next_request.get(host, now))
            _host_next_request[host] = (
                request_time + HOST_REQUEST_INTERVAL_SECONDS
            )
        delay = request_time - now
        if delay > 0:
            time.sleep(delay)
        yield
    finally:
        semaphore.release()


def check_station(station, workers_per_host):
    url = station["stream"]
    station_id = station_id_from_url(url)
    try:
        current_url = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            with host_request_slot(current_url, workers_per_host):
                with get_session().get(
                    current_url,
                    allow_redirects=False,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT_SECONDS, FIRST_BYTE_TIMEOUT_SECONDS),
                ) as response:
                    if response.is_redirect or response.is_permanent_redirect:
                        location = response.headers.get("Location")
                        if not location or redirect_count == MAX_REDIRECTS:
                            return station_id, False
                        current_url = urljoin(current_url, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        return station_id, False
                    first_chunk = next(response.iter_content(chunk_size=1), b"")
                    return station_id, bool(first_chunk)
        return station_id, False
    except requests.RequestException:
        return station_id, False


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
    if not isinstance(document, list):
        return {}
    return {
        entry["id"]: entry
        for entry in document
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def distribute_stations_by_host(stations):
    host_queues = {}
    for station in stations:
        host = get_host(station["stream"])
        host_queues.setdefault(host, deque()).append(station)

    active_hosts = deque(host_queues.values())
    distributed = []
    while active_hosts:
        host_queue = active_hosts.popleft()
        distributed.append(host_queue.popleft())
        if host_queue:
            active_hosts.append(host_queue)
    return distributed


def build_report(stations, previous_failures, workers, workers_per_host):
    unavailable = {}
    distributed_stations = distribute_stations_by_host(stations)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(check_station, station, workers_per_host)
            for station in distributed_stations
        ]
        for completed, future in enumerate(
            concurrent.futures.as_completed(futures), start=1
        ):
            station_id, available = future.result()
            if not available:
                previous = previous_failures.get(station_id, {})
                unavailable[station_id] = {
                    "id": station_id,
                    "consecutive_failures": int(
                        previous.get("consecutive_failures", 0)
                    )
                    + 1,
                }
            if completed % 50 == 0 or completed == len(stations):
                print(
                    f"Checked {completed}/{len(stations)}, "
                    f"unavailable: {len(unavailable)}",
                    file=sys.stderr,
                )

    return [unavailable[station_id] for station_id in sorted(unavailable)]


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("playlist")
    parser.add_argument("previous_report")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--workers-per-host", type=int, default=DEFAULT_WORKERS_PER_HOST
    )
    return parser.parse_args()


def write_report(report):
    print("[")
    for index, entry in enumerate(report):
        suffix = "," if index + 1 < len(report) else ""
        encoded = json.dumps(entry, ensure_ascii=False)
        print(f"  {encoded}{suffix}")
    print("]")


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
    write_report(report)


if __name__ == "__main__":
    main()
