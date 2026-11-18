import json

import h5py
import numpy as np
import pytest

from dynamic_gating_ap import discovery
from dynamic_gating_ap.nwb import qc_mask
from dynamic_gating_ap.scan import scan


def test_pagination_count_only_present_on_first_page(monkeypatch, tmp_path):
    pages = iter(
        [
            {"count": 2, "next": "next-page", "results": [{"asset_id": "a"}]},
            {"count": None, "next": None, "results": [{"asset_id": "b"}]},
        ]
    )
    monkeypatch.setattr(discovery, "get_json", lambda *_: next(pages))
    assert len(discovery.assets(tmp_path)) == 2


def test_legacy_qc_is_explicit_and_respects_thresholds(tmp_path):
    with h5py.File(tmp_path / "units.h5", "w") as file:
        units = file.create_group("units")
        units["quality"] = np.array([b"good", b"noise", b"good", b"good"])
        units["amplitude_cutoff"] = [0.1, 0, 0.1001, 0]
        units["presence_ratio"] = [0.95, 1, 1, 0.949]
        units["isi_violations"] = [0.5, 0, 0, 0]
        with pytest.raises(ValueError, match="missing_default_qc"):
            qc_mask(units)
        mask, source = qc_mask(units, legacy=True)
        assert mask.tolist() == [True, False, False, False]
        assert source.startswith("legacy_")
        units["default_qc"] = [False, True, False, False]
        assert qc_mask(units, legacy=True)[0].tolist() == [False, True, False, False]


def test_scan_ap_only_and_detect_npx2(tmp_path):
    recording = tmp_path / "recording1"
    streams = []
    for name, rate in [("ProbeA-AP", 30000), ("ProbeA-LFP", 2500)]:
        folder = recording / "continuous" / name
        folder.mkdir(parents=True)
        np.zeros((20, 4), dtype="<i2").tofile(folder / "continuous.dat")
        streams.append(
            dict(
                folder_name=name + "/",
                num_channels=4,
                sample_rate=rate,
                channels=[dict(bit_volts=0.195)] * 4,
            )
        )
    (recording / "structure.oebin").write_text(json.dumps({"continuous": streams}))
    (tmp_path / "recording.npx2").write_bytes(b"example")
    found = scan(tmp_path)
    assert found["status"] == "ok"
    assert len(found["streams"]) == 1
    assert found["streams"][0]["n_samples"] == 20
    assert found["streams"][0]["probe"] == "probeA"
    assert len(found["compressed_recordings"]) == 1
