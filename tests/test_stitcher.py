import io

import screenrecord as sr


def _result(ts_list, window_width=1024, quality=None):
    samples = [(ts, {"AF3": float(i)}) for i, ts in enumerate(ts_list)]
    return sr.CaptureResult(
        capture_ts_ns=ts_list[-1],
        samples=samples,
        quality=quality or {"AF3": "good"},
        window_width=window_width,
    )


def _rows(out):
    lines = out.getvalue().strip().split("\n")
    return lines[1:]  # skip header


def test_stitcher_emits_all_on_first_capture():
    out = io.StringIO()
    writer = sr.CSVWriter(out=out)
    stitcher = sr.Stitcher()
    ts_list = list(range(1000, 1010))
    stitcher.process(_result(ts_list), writer)
    assert len(_rows(out)) == 10
    assert stitcher.last_emitted_ts_ns == 1009


def test_stitcher_dedups_overlapping_tail():
    out = io.StringIO()
    writer = sr.CSVWriter(out=out)
    stitcher = sr.Stitcher()
    stitcher.process(_result(list(range(1000, 1010))), writer)
    # Second capture overlaps [1005, 1014]; only 1010..1014 are genuinely new.
    stitcher.process(_result(list(range(1005, 1015))), writer)
    rows = _rows(out)
    assert len(rows) == 10 + 5
    emitted_ts = [int(r.split(",")[0]) for r in rows]
    assert emitted_ts == sorted(emitted_ts)
    assert stitcher.last_emitted_ts_ns == 1014


def test_stitcher_no_new_samples_does_not_crash():
    out = io.StringIO()
    writer = sr.CSVWriter(out=out)
    stitcher = sr.Stitcher()
    stitcher.process(_result(list(range(1000, 1010))), writer)
    # Entirely stale capture (all timestamps <= last emitted).
    stitcher.process(_result(list(range(990, 1000))), writer)
    assert stitcher.last_emitted_ts_ns == 1009
    assert len(_rows(out)) == 10  # nothing new got appended


def test_stitcher_handles_window_resize_between_captures():
    out = io.StringIO()
    writer = sr.CSVWriter(out=out)
    stitcher = sr.Stitcher(verbose=True)
    stitcher.process(_result(list(range(1000, 1010)), window_width=1024), writer)
    # Should not raise even though window_width differs (overlap value
    # comparison is skipped, per design, when a resize is detected).
    stitcher.process(_result(list(range(1005, 1015)), window_width=800), writer)
    assert stitcher.last_emitted_ts_ns == 1014
