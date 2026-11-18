"""Small, atomic artifacts and cached, bounded HTTP requests."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    os.replace(temporary, path)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    temporary = path.with_suffix(".csv.partial")
    with temporary.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def get_json(url: str, cache: Path):
    key = hashlib.sha256(url.encode()).hexdigest()
    path = cache / (key + ".json")
    if path.exists():
        return json.loads(path.read_text())
    with requests.Session() as session:
        session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]
                )
            ),
        )
        response = session.get(url, timeout=(10, 45))
        response.raise_for_status()
        value = response.json()
    write_json(path, value)
    return value


def file_uri(path: Path) -> str:
    return path.absolute().as_uri()
