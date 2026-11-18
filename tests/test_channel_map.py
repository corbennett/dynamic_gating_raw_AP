import csv
from pathlib import Path

CHANNEL_MAP = Path(__file__).parents[1] / "channel_maps" / "neuropixels_1.0.csv"


def test_neuropixels_1_channel_map_geometry():
    with CHANNEL_MAP.open(newline="") as f:
        rows = list(csv.DictReader(f))

    assert [int(r["channel"]) for r in rows] == list(range(384))
    for c, r in enumerate(rows):
        # Staggered four-column layout, two sites per 20 um row, tip row at 20 um.
        assert int(r["x_um"]) == (43, 11, 59, 27)[c % 4]
        assert int(r["y_um"]) == 20 * (c // 2 + 1)
        assert r["shank"] == "0"
    assert [int(r["channel"]) for r in rows if r["connected"] == "false"] == [191]
