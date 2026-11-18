"""Unshifted waveform comparisons across recording time, plus a jitter control."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path("outputs/cache/matplotlib").absolute()))
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .common import read_csv, write_csv, write_json
from .timing import read_alignment, sample_count


def normalize(wave):
    wave = np.asarray(wave, dtype=float)
    wave = wave - np.mean(wave[: min(10, len(wave))])
    norm = np.linalg.norm(wave)
    if not np.isfinite(wave).all() or norm == 0:
        raise ValueError("Empty, flat, or non-finite waveform")
    return wave / norm


def correlation(a, b):
    if np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def waveform_metrics(actual, reference):
    a, b = normalize(actual), normalize(reference)
    lags = np.arange(-10, 11)
    scores = []
    for lag in lags:
        x, y = (a[lag:], b[:-lag]) if lag > 0 else (a[:lag], b[-lag:]) if lag < 0 else (a, b)
        scores.append(correlation(x, y))
    return dict(
        correlation=correlation(a, b),
        best_lag_samples=int(lags[np.argmax(scores)]),
        best_lag_correlation=float(max(scores)),
    )


def snippets(raw, indices, channel, pre, length, gain):
    result = []
    for index in indices:
        block = np.asarray(raw[index - pre : index - pre + length], dtype=np.float64)
        # Common-median reference across probe channels, then pre-spike baseline.
        wave = (block[:, channel] - np.median(block, axis=1)) * gain
        wave -= np.mean(wave[: min(10, pre)])
        result.append(wave)
    return np.asarray(result)


class APRangeReader:
    """One bounded read per snippet avoids many mmap page faults on SMB mounts."""

    def __init__(self, path: Path, channels: int):
        self.stream = path.open("rb", buffering=0)
        self.channels = channels

    def __getitem__(self, window: slice):
        if window.step is not None or window.start < 0 or window.stop <= window.start:
            raise ValueError("Expected a positive contiguous sample window")
        self.stream.seek(window.start * self.channels * 2)
        expected = (window.stop - window.start) * self.channels * 2
        content = self.stream.read(expected)
        if len(content) != expected:
            raise ValueError("Short read from AP binary")
        return np.frombuffer(content, dtype="<i2").reshape(-1, self.channels)

    def close(self):
        self.stream.close()


def validate_probe(
    row: dict,
    reference: dict,
    destination: Path,
    max_spikes: int = 150,
    pre: int = 20,
    force: bool = False,
) -> dict:
    if max_spikes < 30 or pre < 1:
        raise ValueError("Need >=30 spikes and a positive pre-spike window")
    dat = Path(row["dat_path"])
    channels = int(row["n_channels"])
    count = sample_count(dat, channels)
    alignment_path = Path(row["alignment_path"])
    alignment = read_alignment(alignment_path, row["probe"])
    signature = dict(
        dat_path=str(dat),
        dat_size=dat.stat().st_size,
        dat_mtime=dat.stat().st_mtime_ns,
        alignment_sha256=hashlib.sha256(alignment_path.read_bytes()).hexdigest(),
        reference_sha256=hashlib.sha256(Path(reference["reference_path"]).read_bytes()).hexdigest(),
        max_spikes=max_spikes,
        pre_samples=pre,
        algorithm_version=1,
    )
    result_path = destination.with_suffix(".json")
    if result_path.exists() and not force:
        cached = json.loads(result_path.read_text())
        if cached.get("signature") == signature and destination.with_suffix(".png").exists():
            return cached
    with np.load(reference["reference_path"], allow_pickle=False) as data:
        spikes = data["spike_times"]
        waveform = data["waveform"]
    length = len(waveform)
    if not pre < length:
        raise ValueError("pre_samples must be smaller than reference waveform length")
    channel = int(reference["peak_channel"])
    if not 0 <= channel < channels:
        raise ValueError("NWB channel outside raw binary channel range")
    gains = json.loads(row["channel_gains_json"])
    if len(gains) != channels or not np.isfinite(gains[channel]) or gains[channel] <= 0:
        raise ValueError("Missing/invalid structure.oebin channel gain")
    indices = alignment.indices(spikes)
    valid = (indices >= pre) & (indices + length - pre < count)
    in_bounds_fraction = float(np.mean(valid)) if len(valid) else 0
    valid_indices = indices[valid]
    if len(valid_indices) < 30:
        raise ValueError("Too few in-bounds spikes to validate")
    # Stratify over recording thirds to expose scale errors/drift, not only offset.
    chosen, strata = [], []
    for third in range(3):
        within = valid_indices[
            (valid_indices >= count * third / 3) & (valid_indices < count * (third + 1) / 3)
        ]
        if len(within) < 10:
            raise ValueError(f"Fewer than 10 valid spikes in recording third {third + 1}")
        select = within[
            np.linspace(0, len(within) - 1, min(max_spikes // 3, len(within)), dtype=int)
        ]
        chosen.extend(select)
        strata.extend([third] * len(select))
    chosen = np.asarray(chosen, dtype=np.int64)
    strata = np.asarray(strata)
    raw = APRangeReader(dat, channels)
    try:
        waves = snippets(raw, chosen, channel, pre, length, gains[channel])
        jittered = chosen + int(round(alignment.sample_rate * 0.05))
        jittered = jittered[(jittered >= pre) & (jittered + length - pre < count)]
        controls = snippets(raw, jittered, channel, pre, length, gains[channel])
    finally:
        raw.close()
    if not len(controls):
        raise ValueError("No in-bounds jitter-control windows")
    mean = waves.mean(axis=0)
    control = controls.mean(axis=0)
    metrics = waveform_metrics(mean, waveform)
    third_metrics = [waveform_metrics(waves[strata == k].mean(axis=0), waveform) for k in range(3)]
    control_ratio = float(np.ptp(control) / np.ptp(mean)) if np.ptp(mean) else 1e9
    passed = (
        in_bounds_fraction >= 0.99
        and metrics["correlation"] >= 0.8
        and abs(metrics["best_lag_samples"]) <= 2
        and control_ratio < 0.5
        and all(m["correlation"] >= 0.7 and abs(m["best_lag_samples"]) <= 2 for m in third_metrics)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    x = (np.arange(length) - pre) / alignment.sample_rate * 1000
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.6), constrained_layout=True)
    axes[0].plot(x, mean, label="Raw AP STA", color="black")
    axes[0].fill_between(
        x,
        mean - waves.std(axis=0) / np.sqrt(len(waves)),
        mean + waves.std(axis=0) / np.sqrt(len(waves)),
        alpha=0.2,
    )
    axes[0].plot(x, control, label="+50 ms control", alpha=0.7)
    axes[0].set_ylabel("µV (Open Ephys gain)")
    axes[1].plot(x, normalize(waveform), label="NWB reference", color="black", linewidth=2)
    axes[1].plot(x, normalize(mean), label="Raw AP STA")
    axes[1].set_ylabel("Normalized waveform")
    for third in range(3):
        axes[2].plot(
            x,
            normalize(waves[strata == third].mean(axis=0)),
            label=("Early", "Middle", "Late")[third],
        )
    axes[2].plot(x, normalize(waveform), color="black", linestyle="--", label="NWB reference")
    axes[2].set_ylabel("Normalized waveform")
    for axis in axes:
        axis.axvline(0, color="gray", linewidth=0.5)
        axis.set_xlabel("Time from NWB spike (ms)")
        axis.legend(fontsize=8)
    status = "passed" if passed else "review_required"
    fig.suptitle(
        f"{row['lims_session_id']} {row['probe']} | unit {reference['unit_id']} | "
        f"r={metrics['correlation']:.3f}, lag={metrics['best_lag_samples']} samples | {status}\n"
        f"QC: {'legacy fallback' if reference['qc_source'].startswith('legacy') else 'default_qc'}; "
        f"{len(waves)} spikes; no waveform shifting",
        fontsize=10,
    )
    fig.savefig(destination.with_suffix(".png"), dpi=150)
    plt.close(fig)
    np.savez_compressed(
        destination.with_suffix(".npz"),
        sample_indices=chosen,
        thirds=strata,
        waveforms_uv=waves,
        reference_native=waveform,
        control_uv=control,
        time_ms=x,
    )
    result = dict(
        status=status,
        signature=signature,
        **metrics,
        thirds=third_metrics,
        unit_id=reference["unit_id"],
        peak_channel=channel,
        qc_source=reference["qc_source"],
        spike_count=len(waves),
        in_bounds_fraction=in_bounds_fraction,
        control_ptp_ratio=control_ratio,
        plot=str(destination.with_suffix(".png").absolute()),
        nwb_waveform_stored_unit=reference["waveform_stored_unit"],
        waveform_comparison="normalized; legacy NWB amplitude units not assumed",
        thresholds=dict(
            correlation=0.8,
            third_correlation=0.7,
            max_lag_samples=2,
            max_control_ptp_ratio=0.5,
            min_in_bounds_fraction=0.99,
        ),
    )
    write_json(result_path, result)
    return result


def _task(row, reference, destination, max_spikes, pre, force):
    try:
        return validate_probe(row, reference, destination, max_spikes, pre, force)
    except Exception as exc:
        return dict(status="validation_error", error=f"{type(exc).__name__}: {exc}")


def validate_inventory(output: Path, workers: int, max_spikes: int, pre: int, force: bool):
    rows = read_csv(output / "inventory.csv")
    index = {x["asset_id"]: x for x in json.loads((output / "nwb_index.json").read_text())}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for row in rows:
            if row["discovery_status"] != "dat_found":
                row["validation_status"] = "unavailable_" + row["discovery_status"]
                continue
            session = index.get(row["nwb_asset_id"], {})
            if (
                session.get("identifier") != row["lims_session_id"]
                or session.get("subject_id") != row["subject_id"]
            ):
                row["validation_status"] = "nwb_lims_identity_mismatch"
                continue
            reference = session.get("probes", {}).get(row["probe"], {})
            if reference.get("status") != "reference_ready":
                row["validation_status"] = reference.get("status", "no_nwb_reference")
                continue
            destination = output / "validation" / row["lims_session_id"] / row["probe"]
            future = pool.submit(_task, row, reference, destination, max_spikes, pre, force)
            futures[future] = row
        for n, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            result = future.result()
            row.update(
                validation_status=result["status"],
                validation_plot=result.get("plot", ""),
                validation_error=result.get("error", ""),
                unit_id=result.get("unit_id", ""),
                waveform_correlation=result.get("correlation", ""),
                waveform_lag_samples=result.get("best_lag_samples", ""),
                qc_source=result.get("qc_source", ""),
            )
            write_csv(output / "inventory.csv", rows)
            print(
                f"Validation {n}/{len(futures)} {row['lims_session_id']}/{row['probe']}: {result['status']}",
                flush=True,
            )
    write_csv(output / "inventory.csv", rows)
