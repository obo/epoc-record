import numpy as np

import screenrecord as sr


def test_digitize_curve_length_matches_width(clean_frame, clean_bounds, clean_baselines):
    bounds = clean_bounds
    baselines = clean_baselines
    plot_rgb = clean_frame.rgb[bounds.y0 : bounds.y1, bounds.x0 : bounds.x1]
    color = (43, 188, 58)  # F7 green
    baseline_local = baselines["F7"] - bounds.y0
    match = sr._alpha_blend_match(plot_rgb, color)
    rows = sr.digitize_curve(match, baseline_local, max_excursion_px=100)
    assert len(rows) == plot_rgb.shape[1]


def test_digitize_curve_mostly_matches_present_color(clean_frame, clean_bounds, clean_baselines):
    bounds = clean_bounds
    baselines = clean_baselines
    plot_rgb = clean_frame.rgb[bounds.y0 : bounds.y1, bounds.x0 : bounds.x1]
    colors, _ = sr.calibrate_channel_colors(clean_frame, baselines)
    match_masks = sr.classify_plot_pixels(plot_rgb, colors)
    ys_sorted = sorted(baselines.values())
    row_gap = ys_sorted[1] - ys_sorted[0]
    for ch in ["F7", "FC5", "O2"]:
        baseline_local = baselines[ch] - bounds.y0
        rows = sr.digitize_curve(match_masks[ch], baseline_local, max_excursion_px=2.5 * row_gap)
        non_none = sum(1 for r in rows if r is not None)
        assert non_none / len(rows) > 0.5, f"{ch}: only {non_none}/{len(rows)} columns matched"


def test_digitize_curve_never_fabricates_absent_color(clean_frame, clean_bounds):
    # A color that (very probably) doesn't appear anywhere in the plot
    # should yield an all-None result -- never a guessed/interpolated
    # value.
    bounds = clean_bounds
    plot_rgb = clean_frame.rgb[bounds.y0 : bounds.y1, bounds.x0 : bounds.x1]
    # Pure blue: none of the 14 channel colors point in a blue-ish
    # direction (the palette is gray/green/teal/yellow/salmon/magenta/
    # orange/purple), so no real anti-aliased blend toward white should
    # land near this line either -- verified empirically (0 matching
    # pixels against the fixture) before picking it, since the
    # alpha-blend matcher accepts a wide swath of a given hue direction
    # and a naively "different-looking" color can still coincide with a
    # real curve's blend-toward-white for a similar hue.
    absent_color = (0, 0, 255)
    match = sr._alpha_blend_match(plot_rgb, absent_color)
    rows = sr.digitize_curve(match, baseline_y_local=10, max_excursion_px=1000)
    assert all(r is None for r in rows)


def test_classify_plot_pixels_prevents_cross_channel_bleed():
    # Regression test for a bug found live: two similarly-hued channels
    # (P8 teal (16,182,179) and FC6 cyan (102,218,244)) -- when P8's own
    # curve is entirely absent (e.g. covered by another window) but
    # FC6's real curve is visible within P8's search window, P8 must NOT
    # claim FC6's pixels just because they also clear P8's own threshold
    # in isolation. classify_plot_pixels' winner-take-all assignment
    # should give every matching pixel to FC6 (the actual closer match),
    # leaving P8 with nothing to claim.
    h, w = 20, 5
    plot_rgb = np.full((h, w), 255, dtype=np.uint8)
    plot_rgb = np.stack([plot_rgb] * 3, axis=-1)
    fc6_color = (102, 218, 244)
    plot_rgb[10, :] = fc6_color  # a real FC6 curve, no P8 curve anywhere
    colors = {"P8": (16, 182, 179), "FC6": fc6_color}
    masks = sr.classify_plot_pixels(plot_rgb, colors)
    assert not masks["P8"].any(), "P8 must not claim any pixels here"
    assert masks["FC6"][10, :].all(), "FC6 should claim its own real curve pixels"

    # And confirm digitize_curve then correctly reports P8 as all-None
    # even with a search window wide enough to reach FC6's row.
    rows = sr.digitize_curve(masks["P8"], baseline_y_local=2, max_excursion_px=15)
    assert all(r is None for r in rows)


def test_digitize_capture_end_to_end(clean_frame):
    result, spacing_used = sr.digitize_capture(clean_frame, last_spacing_uv=None)
    assert spacing_used == 80.0
    assert result is not None
    assert len(result.samples) > 0
    # Timestamps within one capture must be non-decreasing (sorted before
    # being handed to the stitcher).
    ts_values = [ts for ts, _ in result.samples]
    assert ts_values == sorted(ts_values)


def test_digitize_capture_end_to_end_at_non_uniform_resize(resized_frame, wide_frame):
    # The actual regression this whole fixed-pixel-margin model exists
    # for: digitize_capture must produce real, plausible per-channel
    # samples (not None, not a garbage-scale explosion) at window sizes
    # where width and height were resized by different factors -- this
    # used to either fail spacing OCR entirely or read a wildly wrong
    # multi-digit value, which blew up every channel's uV scale.
    for frame in (resized_frame, wide_frame):
        result, spacing_used = sr.digitize_capture(frame, last_spacing_uv=None)
        assert spacing_used == 20.0
        assert result is not None
        assert len(result.samples) > 0
        non_none_af3 = [s[1]["AF3"] for s in result.samples if s[1]["AF3"] is not None]
        assert len(non_none_af3) > 0
        # Plausible EEG-scale amplitude, not a garbage-scale explosion
        # (a mis-detected spacing used to produce values in the
        # thousands or more).
        assert all(abs(v) < 500 for v in non_none_af3)
