"""Bounded-depth filesystem discovery, run in a subprocess for SMB timeouts."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def scan(root: Path, check_only: bool = False) -> dict:
    root.stat()  # Missing, denied, and timed-out are distinct outcomes.
    with os.scandir(root) as entries:
        next(entries, None)
    if check_only:
        return {"status": "ok"}
    streams, unresolved, compressed, errors = [], [], [], []

    def raise_error(exc):
        raise exc

    for directory, dirs, files in os.walk(root, onerror=raise_error):
        path = Path(directory)
        compressed.extend(str(path / name) for name in files if name.endswith(".npx2"))
        if len(path.relative_to(root).parts) >= 7:
            dirs[:] = []
        if "structure.oebin" not in files:
            continue
        metadata_path = path / "structure.oebin"
        metadata = json.loads(metadata_path.read_text())
        for stream in metadata.get("continuous", []):
            rate = float(stream["sample_rate"])
            if not 25000 <= rate <= 35000:
                continue
            folder = stream["folder_name"].rstrip("/")
            dat = path / "continuous" / folder / "continuous.dat"
            try:
                size = dat.stat().st_size
            except FileNotFoundError:
                continue
            probe = re.search(r"[Pp]robe([A-F])(?:[-_./]|$)", folder)
            if not probe:
                probe = re.search(r"[Pp]robe([A-F])(?:[_/]|$)", str(path))
            if not probe:
                unresolved.append(str(dat))
                continue
            channels = int(stream["num_channels"])
            if channels <= 0 or size == 0 or size % (channels * 2):
                errors.append(
                    {
                        "probe": "probe" + probe[1],
                        "path": str(dat),
                        "error": "Invalid AP byte count",
                    }
                )
                continue
            gains = [float(ch["bit_volts"]) for ch in stream.get("channels", [])]
            streams.append(
                dict(
                    probe="probe" + probe[1],
                    dat_path=str(dat),
                    dat_size_bytes=size,
                    n_samples=size // (channels * 2),
                    n_channels=channels,
                    nominal_rate_hz=rate,
                    structure_oebin_path=str(metadata_path),
                    channel_gains_json=json.dumps(gains),
                    channel_mapping="structure.oebin",
                    sample_numbers_path=str(dat.parent / "sample_numbers.npy")
                    if (dat.parent / "sample_numbers.npy").exists()
                    else "",
                    original_timestamps_path=str(dat.parent / "timestamps.npy")
                    if (dat.parent / "timestamps.npy").exists()
                    else "",
                )
            )
    if unresolved:
        return {
            "status": "unresolved_probe_mapping",
            "error": json.dumps(unresolved),
            "streams": streams,
        }
    return {
        "status": "ok",
        "streams": streams,
        "compressed_recordings": compressed,
        "invalid_streams": errors,
    }


if __name__ == "__main__":
    try:
        result = scan(Path(sys.argv[1]), "--check-only" in sys.argv)
    except PermissionError as exc:
        result = {"status": "permission_denied", "error": str(exc)}
    except FileNotFoundError as exc:
        result = {"status": "path_missing", "error": str(exc)}
    except Exception as exc:
        result = {"status": "scan_error", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))
