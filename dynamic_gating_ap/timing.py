"""Compact per-recording clock metadata with the pinned QC/npc-ephys convention."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .common import write_json


@dataclass(frozen=True)
class Alignment:
    sample_rate: float
    shift: float

    def __post_init__(self):
        if not np.isfinite(self.sample_rate) or not 25000 < self.sample_rate < 35000:
            raise ValueError("Invalid adjusted AP sample_rate")
        if not np.isfinite(self.shift):
            raise ValueError("Non-finite clock shift")

    def times(self, indices):
        return np.asarray(indices, dtype=np.float64) / self.sample_rate - self.shift

    def indices(self, times):
        return np.rint((np.asarray(times) + self.shift) * self.sample_rate).astype(np.int64)


def read_alignment(path: Path, probe: str) -> Alignment:
    value = json.loads(path.read_text())
    key = probe.removeprefix("probe")
    if key not in value and probe not in value:
        raise ValueError(f"Missing alignment for {probe} in {path}")
    record = value[key] if key in value else value[probe]
    # These exact names are written by np_pipeline_qc.analysis.probe_sync_report.
    return Alignment(float(record["sample_rate"]), float(record["shift"]))


def sample_count(path: Path, channels: int) -> int:
    if channels <= 0:
        raise ValueError("Channel count must be positive")
    size = path.stat().st_size
    if size == 0 or size % (2 * channels):
        raise ValueError(
            "AP size must be a nonzero multiple of 2 * n_channels (little-endian int16)"
        )
    return size // (2 * channels)


def write_timing_metadata(
    dat: Path,
    channels: int,
    alignment_path: Path,
    probe: str,
    destination: Path,
    session_id: str,
    nwb_asset_id: str,
    sample_numbers: Path | None = None,
) -> dict:
    """A compact affine clock description, replacing multi-GB timestamp arrays."""
    alignment = read_alignment(alignment_path, probe)
    count = sample_count(dat, channels)
    source_stat = dat.stat()
    counter_check = {"status": "not_available"}
    if sample_numbers:
        numbers = np.load(sample_numbers, mmap_mode="r", allow_pickle=False)
        if numbers.shape != (count,) or numbers.dtype.kind not in "iu":
            raise ValueError("Sample counter shape/dtype mismatch")
        # Sparse checks keep this stage inexpensive; they are explicitly not a full scan.
        indices = np.unique(np.linspace(0, count - 1, min(count, 16), dtype=np.int64))
        origin = int(numbers[0])
        if not np.array_equal(numbers[indices], origin + indices):
            raise ValueError(
                "Sample counter discontinuity: cannot describe this file with one affine clock"
            )
        counter_check = dict(
            status="16_or_fewer_equally_spaced_checkpoints_passed",
            checked_samples=len(indices),
            full_continuity_scan=False,
            acquisition_counter_origin=origin,
            source=str(sample_numbers),
        )
    result = dict(
        schema_version="1.0",
        session_id=session_id,
        probe=probe,
        nwb_asset_id=nwb_asset_id,
        clock="master_sync",
        time_unit="seconds",
        start_time=-alignment.shift,
        sample_rate=alignment.sample_rate,
        sample_rate_unit="Hz",
        n_samples=count,
        timestamp_formula="start_time + sample_index / sample_rate",
        sample_index_origin=0,
        last_sample_time=float(alignment.times(count - 1)),
        stop_time_exclusive=float(alignment.times(count)),
        dat_path=str(dat.absolute()),
        dat_dtype="<i2",
        n_channels=channels,
        dat_layout="sample_major_interleaved_channels",
        dat_size_bytes=source_stat.st_size,
        dat_mtime_ns=source_stat.st_mtime_ns,
        alignment_source=str(alignment_path.absolute()),
        alignment_sha256=hashlib.sha256(alignment_path.read_bytes()).hexdigest(),
        source_shift=alignment.shift,
        sample_counter_check=counter_check,
    )
    write_json(destination, result)
    return result
