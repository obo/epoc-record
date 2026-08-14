"""Regression test against a library of real EmotivPRO captures collected
across many window sizes (see tests/fixtures/resize_regression/README.md).

Each screenshot has a "golden" CSV of digitize_capture()'s verified-correct
output alongside it (same filename, .csv instead of .png; a leading
"# spacing_uv=..." comment line records the expected OCR'd spacing value,
followed by the normal CSVWriter-format rows). This test re-runs
digitize_capture() on every screenshot and checks the output still matches
-- catching any future change that silently breaks reading at some window
size/aspect ratio, without needing the live app or a human to re-verify by
eye each time.
"""

import csv
import glob
import io
import os

import numpy as np
import pytest
from PIL import Image

import screenrecord as sr

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "resize_regression")

# Small tolerance for float comparisons: the pipeline is deterministic
# given identical input pixels, so this only needs to absorb harmless
# float-formatting round-trips, not real drift.
VALUE_TOLERANCE = 0.05


def _pairs():
    pngs = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.png")))
    return [(p, p[:-4] + ".csv") for p in pngs]


def _read_golden(csv_path):
    with open(csv_path) as f:
        first_line = f.readline()
        assert first_line.startswith("# spacing_uv="), f"{csv_path}: missing spacing header"
        spacing_str = first_line.strip().split("=", 1)[1]
        golden_spacing = None if spacing_str == "None" else float(spacing_str)
        rows = list(csv.DictReader(io.StringIO(f.read())))
    return golden_spacing, rows


@pytest.mark.parametrize(
    "png_path,csv_path", _pairs(), ids=lambda p: os.path.basename(p) if isinstance(p, str) else None
)
def test_matches_golden_output(png_path, csv_path):
    assert os.path.exists(csv_path), f"missing golden CSV for {png_path}"
    arr = np.array(Image.open(png_path).convert("RGB"))
    frame = sr.Frame(rgb=arr, width=arr.shape[1], height=arr.shape[0], capture_ts_ns=1_800_000_000_000_000_000)
    result, spacing = sr.digitize_capture(frame, last_spacing_uv=None, verbose=False)

    golden_spacing, golden_rows = _read_golden(csv_path)
    assert spacing == golden_spacing, f"{png_path}: spacing {spacing} != golden {golden_spacing}"

    if not golden_rows:
        # A golden fixture with zero rows records a capture that was
        # verified to be legitimately unreadable (e.g. a mid-resize
        # transient where the app hadn't finished redrawing) -- the
        # regression check is just that it's STILL unreadable, not that
        # it produces zero rows for some new, different reason.
        assert result is None or len(result.samples) == 0
        return

    assert result is not None, f"{png_path}: used to digitize, now returns None"
    got_rows = result.samples
    assert len(got_rows) == len(golden_rows), (
        f"{png_path}: sample count changed ({len(got_rows)} vs golden {len(golden_rows)})"
    )

    for i, (golden, (ts_ns, values)) in enumerate(zip(golden_rows, got_rows)):
        for ch in sr.CHANNELS:
            golden_v = golden[ch]
            got_v = values.get(ch)
            if golden_v == "":
                assert got_v is None, f"{png_path} row {i} {ch}: golden N/A, got {got_v}"
            else:
                assert got_v is not None, f"{png_path} row {i} {ch}: golden {golden_v}, got N/A"
                assert abs(float(golden_v) - got_v) <= VALUE_TOLERANCE, (
                    f"{png_path} row {i} {ch}: golden {golden_v}, got {got_v}"
                )
