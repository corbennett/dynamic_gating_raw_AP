import json

import numpy as np

from dynamic_gating_ap.validation import validate_probe


def test_recovers_waveform_and_rejects_wrong_clock(tmp_path):
    rng = np.random.default_rng(42)
    count, channels = 300000, 8
    raw = rng.normal(0, 2, (count, channels)).astype("<i2")
    x = np.arange(82)
    waveform = -1000 * np.exp(-(((x - 20) / 3) ** 2)) + 400 * np.exp(-(((x - 35) / 8) ** 2))
    indices = np.linspace(2000, count - 2000, 300, dtype=int)
    for i in indices:
        raw[i - 20 : i + 62, 3] += waveform.astype(np.int16)
    dat = tmp_path / "continuous.dat"
    raw.tofile(dat)
    alignment_path = tmp_path / "alignment.json"
    alignment_path.write_text('{"A": {"sample_rate": 30000.1, "shift": -2.25}}')
    reference_path = tmp_path / "ref.npz"
    np.savez(reference_path, waveform=waveform, spike_times=2.25 + indices / 30000.1)
    row = dict(
        dat_path=str(dat),
        n_channels=str(channels),
        alignment_path=str(alignment_path),
        probe="probeA",
        lims_session_id="123",
        channel_gains_json=json.dumps([0.195] * channels),
    )
    reference = dict(
        reference_path=str(reference_path),
        peak_channel=3,
        unit_id=99,
        qc_source="nwb_default_qc",
        waveform_stored_unit="microvolts",
    )
    result = validate_probe(row, reference, tmp_path / "correct")
    assert result["status"] == "passed"
    assert result["correlation"] > 0.99
    assert result["best_lag_samples"] == 0
    assert (tmp_path / "correct.png").exists()
    # A 5 ms alignment error must not be hidden by waveform alignment.
    alignment_path.write_text('{"A": {"sample_rate": 30000.1, "shift": -2.255}}')
    result = validate_probe(row, reference, tmp_path / "wrong")
    assert result["status"] == "review_required"
