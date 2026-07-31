""" get station pcradio """

# pylint: disable=missing-function-docstring line-too-long

import json
import os
import sys

LANG = "en"
URL = f"http://stream.pcradio.ru/list/list_{LANG}/list_{LANG}.zip"
FNV_OFFSET_BASIS = 14695981039346656037
FNV_PRIME = 1099511628211
UINT64_MASK = (1 << 64) - 1
USER_PLAYLIST = "playlist.m3u"


def station_id_from_url(url):
    value = str(url or "").strip().encode("utf-8")
    scheme = value.find(b"://")
    authority_end = 0
    if scheme >= 0:
        authority_end = len(value)
        for separator in (b"/", b"?", b"#"):
            position = value.find(separator, scheme + 3)
            if position >= 0:
                authority_end = min(authority_end, position)

    station_hash = FNV_OFFSET_BASIS
    for position, byte in enumerate(value):
        if position < authority_end and 65 <= byte <= 90:
            byte += 32
        station_hash ^= byte
        station_hash = (station_hash * FNV_PRIME) & UINT64_MASK
    return f"{station_hash:016x}"


def deduplicate_stations(stations):
    unique = []
    seen_ids = set()
    duplicate_count = 0
    for station in stations:
        station_id = station_id_from_url(station["stream"])
        if station_id in seen_ids:
            duplicate_count += 1
            continue
        seen_ids.add(station_id)
        unique.append(station)
    print(
        f"Stations: {len(stations)}, unique: {len(unique)}, "
        f"duplicates removed: {duplicate_count}",
        file=sys.stderr,
    )
    return unique


def get_json_playlist(download_zip_file):
    import pyzipper
    import requests

    password = os.getenv("ZIPPASSWORD")
    if not password:
        raise RuntimeError("ZIPPASSWORD is not configured")
    response = requests.get(
        download_zip_file,
        headers={"User-Agent": "pcradio"},
        timeout=15,
    )
    response.raise_for_status()
    zip_filename = f"list_{LANG}.zip"
    json_filename = f"list_{LANG}.json"
    with open(zip_filename, "wb") as zip_file:
        zip_file.write(response.content)
    with pyzipper.AESZipFile(zip_filename) as archive:
        archive.setpassword(password.encode("utf-8"))
        json_data = archive.read(json_filename)
    with open(json_filename, "wb") as json_file:
        json_file.write(json_data)


def load_downloaded_stations():
    with open(f"list_{LANG}.json", "r", encoding="utf-8") as json_file:
        playlist = json.load(json_file)
    return playlist["stations"]


def parse_m3u(lines):
    stations = []
    station_name = ""
    for raw_line in lines:
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            _, separator, name = line.partition(",")
            station_name = name.strip() if separator else ""
            continue
        if line.startswith("#"):
            continue
        stations.append(
            {
                "name": station_name or line,
                "stream": line,
            }
        )
        station_name = ""
    return stations


def load_user_stations(filename=USER_PLAYLIST):
    try:
        with open(filename, "r", encoding="utf-8-sig") as playlist_file:
            stations = parse_m3u(playlist_file)
    except FileNotFoundError:
        print(
            f"User playlist {filename} not found; continuing without it",
            file=sys.stderr,
        )
        return []
    print(f"User stations loaded: {len(stations)}", file=sys.stderr)
    return stations


def load_combined_stations():
    # User entries come first and take priority during deduplication.
    stations = load_user_stations()
    stations.extend(load_downloaded_stations())
    return deduplicate_stations(stations)


def write_m3u(stations):
    print("#EXTM3U")
    print("#EXTENC:UTF-8\n")
    for station in stations:
        print(f'#EXTINF:-1,{station["name"]}')
        print(f'{station["stream"]}\n')


def write_uri(stations):
    for station in stations:
        print(station["stream"])


def main(arguments):
    from dotenv import load_dotenv

    load_dotenv()
    if len(arguments) != 2 or arguments[1] not in {"m3u", "uri"}:
        print("Usage: m3u, uri", file=sys.stderr)
        return 2
    get_json_playlist(URL)
    stations = load_combined_stations()
    if arguments[1] == "m3u":
        write_m3u(stations)
    else:
        write_uri(stations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
