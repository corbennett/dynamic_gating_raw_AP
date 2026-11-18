import json

import numpy as np
import pytest

from dynamic_gating_ap.timing import Alignment, sample_count, write_timing_metadata


def test_clock_sign_and_compact_metadata(tmp_path):
    dat = tmp_path / "continuous.dat"
    np.zeros((101, 4), dtype="<i2").tofile(dat)
    registration = tmp_path / "alignment.json"
    registration.write_text(json.dumps({"A": {"sample_rate": 30000.125, "shift": -3.25}}))
    numbers = tmp_path / "sample_numbers.npy"
    np.save(numbers, np.arange(101, dtype=np.int64) + 987654)
    output = tmp_path / "timing.json"
    result = write_timing_metadata(dat, 4, registration, "probeA", output, "123", "asset", numbers)
    assert result["start_time"] == 3.25
    assert result["sample_rate"] == 30000.125
    assert result["n_samples"] == 101
    assert result["last_sample_time"] == 3.25 + 100 / 30000.125
    assert result["sample_counter_check"]["acquisition_counter_origin"] == 987654
    assert result["sample_counter_check"]["full_continuity_scan"] is False
    assert output.stat().st_size < 4096
    alignment = Alignment(30000.125, -3.25)
    indices = np.array([0, 1, 100, 100000000])
    np.testing.assert_array_equal(alignment.indices(alignment.times(indices)), indices)


@pytest.mark.parametrize("rate,shift", [(0, 1), (float("nan"), 1), (30000, float("inf"))])
def test_reject_invalid_alignment(rate, shift):
    with pytest.raises(ValueError):
        Alignment(rate, shift)


def test_reject_partial_sample_and_counter_gap(tmp_path):
    dat = tmp_path / "continuous.dat"
    dat.write_bytes(b"\0" * 7)
    with pytest.raises(ValueError):
        sample_count(dat, 2)
    np.zeros((20, 2), dtype="<i2").tofile(dat)
    registration = tmp_path / "alignment.json"
    registration.write_text('{"A": {"sample_rate": 30000, "shift": 2}}')
    numbers = np.arange(20, dtype=np.int64)
    numbers[10:] += 1
    np.save(tmp_path / "numbers.npy", numbers)
    with pytest.raises(ValueError, match="discontinuity"):
        write_timing_metadata(
            dat,
            2,
            registration,
            "probeA",
            tmp_path / "timing.json",
            "123",
            "asset",
            tmp_path / "numbers.npy",
        )
    assert not (tmp_path / "timing.json").exists()
