"""Read only selected NWB HDF5 ranges; cache small per-probe reference records."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import fsspec
import h5py
import numpy as np

from .common import write_json


def decode(value):
    return value.decode() if isinstance(value, bytes) else str(value)


@contextmanager
def open_nwb(location: str):
    if location.startswith(("http://", "https://")):
        with fsspec.open(
            location,
            block_size=2**20,
            cache_type="blockcache",
            client_kwargs={"timeout": __import__("aiohttp").ClientTimeout(total=90)},
        ) as stream:
            with h5py.File(stream, "r") as file:
                yield file
    else:
        with h5py.File(location, "r") as file:
            yield file


def qc_mask(units: h5py.Group, legacy: bool = False):
    if "default_qc" in units:
        values = units["default_qc"][:]
        if not np.isin(values, [0, 1]).all():
            raise ValueError("default_qc is not a boolean column")
        return values.astype(bool), "nwb_default_qc"
    if not legacy:
        raise ValueError("missing_default_qc: use --legacy-qc only with an approved fallback")
    quality = np.array([decode(x) for x in units["quality"][:]])
    mask = (
        (quality == "good")
        & (units["amplitude_cutoff"][:] <= 0.1)
        & (units["presence_ratio"][:] >= 0.95)
        & (units["isi_violations"][:] <= 0.5)
    )
    return (
        mask,
        "legacy_quality_good_amplitude_cutoff_le_0.1_presence_ratio_ge_0.95_isi_violations_le_0.5",
    )


def ragged(group, name, index):
    ends = group[name + "_index"]
    start = int(ends[index - 1]) if index else 0
    return group[name][start : int(ends[index])]


def extract_reference(asset: dict, output: Path, legacy: bool = False) -> dict:
    """Each selected unit is the largest amplitude QC-passing unit on its probe."""
    folder = output / "cache" / "nwb" / asset["asset_id"]
    folder.mkdir(parents=True, exist_ok=True)
    metadata_path = folder / ("legacy.json" if legacy else "strict.json")
    if metadata_path.exists():
        import json

        return json.loads(metadata_path.read_text())
    blob = asset.get("blob", "")
    url = (
        f"https://dandiarchive.s3.amazonaws.com/blobs/{blob[:3]}/{blob[3:6]}/{blob}"
        if blob
        else asset["nwb_url"]
    )
    result = dict(asset_id=asset["asset_id"], nwb_url=url, nwb_path=asset["path"], probes={})
    with open_nwb(url) as file:
        result["identifier"] = decode(file["identifier"][()])
        result["session_start_time"] = decode(file["session_start_time"][()])
        result["subject_id"] = decode(file["general/subject/subject_id"][()])
        if "units" not in file:
            result["status"] = "no_units"
        else:
            units = file["units"]
            electrodes = file["general/extracellular_ephys/electrodes"]
            ids = electrodes["id"][:]
            groups = np.array([decode(x) for x in electrodes["group_name"][:]])
            result["unit_columns"] = list(units)
            result["has_default_qc"] = "default_qc" in units
            mask, qc_source = qc_mask(units, legacy)
            # This DANDIset uses global electrode IDs, not positional row indices.
            if "peak_channel_id" not in units or "probe_channel_number" not in electrodes:
                raise ValueError(
                    "Unsupported NWB channel schema; require explicit electrode mapping"
                )
            lookup = {int(channel): i for i, channel in enumerate(ids)}
            peak_rows = np.array([lookup[int(c)] for c in units["peak_channel_id"][:]])
            unit_probes = groups[peak_rows]
            amplitudes = units["amplitude"][:]
            channels = electrodes["probe_channel_number"][:]
            for probe in sorted(set(groups)):
                selection = np.flatnonzero(mask & (unit_probes == probe) & np.isfinite(amplitudes))
                if not selection.size:
                    result["probes"][probe] = dict(status="no_qc_unit", qc_source=qc_source)
                    continue
                index = int(selection[np.argmax(amplitudes[selection])])
                channel = int(channels[peak_rows[index]])
                waveform = ragged(units, "waveform_mean", index)
                # Legacy Allen NWB layout: ragged channels x waveform samples.
                if waveform.ndim != 2 or waveform.shape[0] != np.sum(groups == probe):
                    raise ValueError(f"Unexpected legacy waveform layout: {waveform.shape}")
                local_channels = channels[groups == probe]
                if not np.array_equal(local_channels, np.arange(len(local_channels))):
                    raise ValueError("Nonidentity legacy waveform channel ordering")
                reference = np.asarray(waveform[channel], dtype=float)
                spikes = np.asarray(ragged(units, "spike_times", index), dtype=float)
                if not np.isfinite(spikes).all() or np.any(np.diff(spikes) < 0):
                    raise ValueError("Non-finite or unordered spike times")
                path = folder / f"{probe}.npz"
                np.savez_compressed(path, spike_times=spikes, waveform=reference)
                probe_id = (
                    int(electrodes["probe_id"][peak_rows[index]])
                    if "probe_id" in electrodes
                    else None
                )
                result["probes"][probe] = dict(
                    status="reference_ready",
                    unit_id=int(units["id"][index]),
                    unit_row=index,
                    peak_channel=channel,
                    peak_electrode_id=int(ids[peak_rows[index]]),
                    probe_id=probe_id,
                    amplitude=float(amplitudes[index]),
                    qc_source=qc_source,
                    reference_path=str(path.absolute()),
                    n_spikes=len(spikes),
                    waveform_samples=len(reference),
                    waveform_stored_unit=decode(
                        units["waveform_mean"].attrs.get("unit", "unknown")
                    ),
                )
            result["status"] = "ok"
    write_json(metadata_path, result)
    return result


def session_assets(catalog):
    return [a for a in catalog if re.search(r"_ses-\d{8}T", a["path"])]
