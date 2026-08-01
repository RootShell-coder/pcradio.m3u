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

CONNECT_TIMEOUT_SECONDS = 1.5
FIRST_BYTE_TIMEOUT_SECONDS = 1.0
STATION_NETWORK_BUDGET_SECONDS = 4.0
DEFAULT_CHECK_DEADLINE_SECONDS = 10 * 60
DEFAULT_WORKERS = 20
DEFAULT_WORKERS_PER_HOST = 2
PENDING_TASK_FACTOR = 2
HOST_REQUEST_INTERVAL_SECONDS = 0.25
MAX_REDIRECTS = 5
VLC_USER_AGENT = "VLC/3.0.23 LibVLC/3.0.23"

_thread_local = threading.local()
_host_lock = threading.Lock()
_host_semaphores = {}
_host_next_request = {}


class CheckDeadlineReached(Exception):
    pass


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


def unix_timestamp():
    return int(time.time())


def get_host_semaphore(host, workers_per_host):
    with _host_lock:
        semaphore = _host_semaphores.get(host)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(workers_per_host)
            _host_semaphores[host] = semaphore
    return semaphore


@contextlib.contextmanager
def host_request_slot(url, workers_per_host, run_deadline):
    host = get_host(url)
    semaphore = get_host_semaphore(host, workers_per_host)
    remaining = run_deadline - time.monotonic()
    if remaining <= 0 or not semaphore.acquire(timeout=remaining):
        raise CheckDeadlineReached
    try:
        with _host_lock:
            now = time.monotonic()
            request_time = max(now, _host_next_request.get(host, now))
            _host_next_request[host] = (
                request_time + HOST_REQUEST_INTERVAL_SECONDS
            )
        delay = request_time - now
        if delay > 0:
            if time.monotonic() + delay >= run_deadline:
                raise CheckDeadlineReached
            time.sleep(delay)
        yield
    finally:
        semaphore.release()


def check_station(station, workers_per_host, run_deadline):
    url = station["stream"]
    station_id = station_id_from_url(url)
    network_budget = STATION_NETWORK_BUDGET_SECONDS
    try:
        current_url = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            if time.monotonic() >= run_deadline:
                return station_id, None
            with host_request_slot(
                current_url, workers_per_host, run_deadline
            ):
                remaining_run = run_deadline - time.monotonic()
                if remaining_run <= 0:
                    return station_id, None
                if network_budget <= 0:
                    return station_id, False
                connect_timeout = max(
                    0.1,
                    min(
                        CONNECT_TIMEOUT_SECONDS,
                        network_budget,
                        remaining_run,
                    ),
                )
                read_timeout = max(
                    0.1,
                    min(
                        FIRST_BYTE_TIMEOUT_SECONDS,
                        network_budget,
                        remaining_run,
                    ),
                )
                request_started = time.monotonic()
                with get_session().get(
                    current_url,
                    allow_redirects=False,
                    stream=True,
                    timeout=(connect_timeout, read_timeout),
                ) as response:
                    network_budget -= time.monotonic() - request_started
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
    except CheckDeadlineReached:
        return station_id, None
    except requests.RequestException:
        return station_id, False


def load_stations(filename):
    with open(filename, "r", encoding="utf-8-sig") as playlist_file:
        stations = parse_m3u(playlist_file)
    if not stations:
        raise RuntimeError("Playlist contains no stations")
    return stations


def load_previous_state(filename):
    try:
        with open(filename, "r", encoding="utf-8") as counter_file:
            document = json.load(counter_file)
    except FileNotFoundError:
        return None, None, {}
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(
            f"Cannot read previous availability state: {error}"
        ) from error
    if not isinstance(document, dict):
        raise RuntimeError("Availability state root must be an object")
    cursor = document.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise RuntimeError("Availability cursor must be a string or null")
    if cursor == "":
        cursor = None
    cursor_checked_at = document.get("checked_at")
    if cursor_checked_at is not None and (
        isinstance(cursor_checked_at, bool)
        or not isinstance(cursor_checked_at, int)
        or cursor_checked_at < 0
    ):
        raise RuntimeError("Availability checked_at must be Unix time or null")
    station_entries = document.get("unavailable_stations")
    if not isinstance(station_entries, list):
        raise RuntimeError("unavailable_stations must be an array")
    failures = {}
    for entry in station_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise RuntimeError("Unavailable station must contain a string id")
        count = entry.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise RuntimeError(
                "Unavailable station count must be positive"
            )
        if entry["id"] in failures:
            raise RuntimeError("Duplicate station id in availability state")
        failures[entry["id"]] = entry
    return cursor, cursor_checked_at, failures


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


def rotate_after_cursor(stations, cursor):
    if not cursor:
        return list(stations)
    for index, station in enumerate(stations):
        if station_id_from_url(station["stream"]) == cursor:
            next_index = index + 1
            return list(stations[next_index:]) + list(stations[:next_index])
    return list(stations)


def build_report(
    stations,
    previous_failures,
    previous_cursor,
    previous_cursor_checked_at,
    workers,
    workers_per_host,
    deadline_seconds,
):
    playlist_ids = {
        station_id_from_url(station["stream"]) for station in stations
    }
    unavailable = {
        station_id: {
            "id": station_id,
            "count": int(entry.get("count", 0)),
        }
        for station_id, entry in previous_failures.items()
        if station_id in playlist_ids
        and int(entry.get("count", 0)) > 0
    }
    ordered_stations = rotate_after_cursor(
        distribute_stations_by_host(stations),
        previous_cursor,
    )
    pending_stations = deque(ordered_stations)
    run_checked_at = unix_timestamp()
    run_deadline = time.monotonic() + deadline_seconds
    pending_limit = max(workers, workers * PENDING_TASK_FACTOR)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    pending_futures = {}
    completed_sequences = {}
    next_sequence = 0
    next_cursor_sequence = 0
    cursor = previous_cursor if previous_cursor in playlist_ids else None
    cursor_checked_at = previous_cursor_checked_at if cursor else None
    evaluated = 0

    def fill_pending():
        nonlocal next_sequence
        while (
            pending_stations
            and len(pending_futures) < pending_limit
            and time.monotonic() < run_deadline
        ):
            station = pending_stations.popleft()
            future = executor.submit(
                check_station,
                station,
                workers_per_host,
                run_deadline,
            )
            pending_futures[future] = next_sequence
            next_sequence += 1

    fill_pending()
    while pending_futures and time.monotonic() < run_deadline:
        remaining = run_deadline - time.monotonic()
        done, _ = concurrent.futures.wait(
            pending_futures.keys(),
            timeout=min(1.0, max(0.0, remaining)),
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        if not done:
            continue
        for future in done:
            sequence = pending_futures.pop(future)
            evaluated += 1
            station_id, available = future.result()
            if available is None:
                continue
            checked_at = run_checked_at
            completed_sequences[sequence] = (station_id, checked_at)
            if available is False:
                previous = unavailable.get(station_id, {})
                unavailable[station_id] = {
                    "id": station_id,
                    "count": int(previous.get("count", 0)) + 1,
                }
            else:
                unavailable.pop(station_id, None)
            while next_cursor_sequence in completed_sequences:
                cursor, cursor_checked_at = completed_sequences.pop(
                    next_cursor_sequence
                )
                next_cursor_sequence += 1
            if evaluated % 50 == 0:
                print(
                    f"Processed {evaluated}/{len(stations)} checks, "
                    f"unavailable: {len(unavailable)}",
                    file=sys.stderr,
                )
        fill_pending()

    for future in pending_futures:
        future.cancel()
    executor.shutdown(wait=True, cancel_futures=True)

    print(
        f"Availability check finished: checks={evaluated}/"
        f"{len(stations)}, "
        f"unavailable={len(unavailable)}",
        file=sys.stderr,
    )

    report = [unavailable[station_id] for station_id in sorted(unavailable)]
    return cursor, cursor_checked_at, report


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("playlist")
    parser.add_argument("previous_report")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--workers-per-host", type=int, default=DEFAULT_WORKERS_PER_HOST
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_CHECK_DEADLINE_SECONDS,
    )
    return parser.parse_args()


def write_report(cursor, cursor_checked_at, report):
    print("{")
    print(f'  "cursor": {json.dumps(cursor)},')
    print(f'  "checked_at": {json.dumps(cursor_checked_at)},')
    print('  "unavailable_stations": [')
    for index, entry in enumerate(report):
        suffix = "," if index + 1 < len(report) else ""
        encoded = json.dumps(entry, ensure_ascii=False)
        print(f"    {encoded}{suffix}")
    print("  ]")
    print("}")


def main():
    arguments = parse_arguments()
    stations = load_stations(arguments.playlist)
    previous_cursor, previous_cursor_checked_at, previous = (
        load_previous_state(arguments.previous_report)
    )
    cursor, cursor_checked_at, report = build_report(
        stations,
        previous,
        previous_cursor,
        previous_cursor_checked_at,
        max(1, arguments.workers),
        max(1, arguments.workers_per_host),
        max(1.0, arguments.deadline_seconds),
    )
    write_report(cursor, cursor_checked_at, report)


if __name__ == "__main__":
    main()
