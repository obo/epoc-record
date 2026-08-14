import io

import screenrecord as sr


def test_csv_header():
    out = io.StringIO()
    sr.CSVWriter(out=out)
    header = out.getvalue().strip().split(",")
    assert header[:2] == ["timestamp_ns", "timestamp_iso"]
    assert header[2:16] == sr.CHANNELS
    assert header[16:30] == [f"{ch}_q" for ch in sr.CHANNELS]


def test_write_row_formats_values_and_blanks_missing():
    out = io.StringIO()
    writer = sr.CSVWriter(out=out)
    values = {ch: None for ch in sr.CHANNELS}
    values["AF3"] = 12.345
    quality = {ch: None for ch in sr.CHANNELS}
    quality["AF3"] = "good"
    quality["F7"] = "stale"  # stale has no numeric code -> blank
    writer.write_row(1_700_000_000_123_456_789, values, quality)
    lines = out.getvalue().strip().split("\n")
    row = lines[1].split(",")
    assert row[0] == "1700000000123456789"
    assert row[1] == sr._iso_ns(1_700_000_000_123_456_789)
    af3_idx = 2 + sr.CHANNELS.index("AF3")
    assert row[af3_idx] == "12.35" or row[af3_idx] == "12.34"  # rounding
    f7_idx = 2 + sr.CHANNELS.index("F7")
    assert row[f7_idx] == ""
    af3_q_idx = 2 + len(sr.CHANNELS) + sr.CHANNELS.index("AF3")
    assert row[af3_q_idx] == "3"  # good
    f7_q_idx = 2 + len(sr.CHANNELS) + sr.CHANNELS.index("F7")
    assert row[f7_q_idx] == ""  # stale -> blank, not a fabricated code


def test_iso_ns_nanosecond_precision():
    ts = 1_700_000_000_123_456_789
    iso = sr._iso_ns(ts)
    assert iso.endswith("456789Z")
    assert "T" in iso
