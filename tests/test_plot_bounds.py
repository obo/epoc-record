import screenrecord as sr


def test_find_plot_bounds_reasonable(clean_frame):
    bounds = sr.find_plot_bounds(clean_frame)
    assert bounds is not None
    assert bounds.width > 0.4 * clean_frame.width
    assert bounds.height > 0.4 * clean_frame.height
    # Plot should sit right of the control panel and above the tick labels.
    assert bounds.x0 > 0.2 * clean_frame.width
    assert bounds.x1 < 0.95 * clean_frame.width


def test_find_plot_bounds_uses_fixed_pixel_margins_not_fractions(clean_frame, resized_frame, wide_frame):
    # Regression test for the real bug this model replaced: EmotivPRO does
    # NOT scale uniformly on resize (confirmed against 16 real captures
    # spanning 8 window sizes) -- the plot canvas sits at a FIXED PIXEL
    # offset from each edge, while only the canvas itself grows/shrinks.
    # An earlier fraction-of-window-size model happened to work at the
    # single reference size it was calibrated against and silently broke
    # (wrong bounds, garbage OCR) as soon as width and height stopped
    # scaling by the same factor -- which non-uniform resizing (dragging
    # one edge/corner) always does. Assert the fixed-margin invariant
    # directly against real captures at three different sizes, including
    # width-only and height-only stretches.
    for frame in (clean_frame, resized_frame, wide_frame):
        bounds = sr.find_plot_bounds(frame)
        assert bounds is not None, f"no bounds for {frame.width}x{frame.height}"
        assert bounds.x0 == sr.PLOT_X0_PX
        assert bounds.x1 == frame.width - sr.PLOT_X_RIGHT_MARGIN_PX
        assert bounds.y0 == sr.PLOT_Y0_PX
        assert bounds.y1 == frame.height - sr.PLOT_Y_BOTTOM_MARGIN_PX


def test_find_channel_baselines_ordered_and_spaced(clean_bounds):
    baselines = sr.find_channel_baselines(clean_bounds)
    ys = [baselines[ch] for ch in sr.CHANNELS]
    assert ys == sorted(ys)
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    # Roughly evenly spaced (no huge outlier gap).
    avg_gap = sum(gaps) / len(gaps)
    assert all(0.5 * avg_gap < g < 1.5 * avg_gap for g in gaps)


def test_find_channel_baselines_spacing_scales_with_plot_height(resized_frame):
    # The per-row gap itself DOES stretch with window height (unlike the
    # canvas's fixed top/bottom offsets) -- confirmed live: a taller
    # window visibly spreads the 14 channel rows further apart. Assert
    # the gap matches plot_height/14 for a real non-uniformly-resized
    # capture, not just the reference size.
    bounds = sr.find_plot_bounds(resized_frame)
    baselines = sr.find_channel_baselines(bounds)
    ys = [baselines[ch] for ch in sr.CHANNELS]
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    avg_gap = sum(gaps) / len(gaps)
    expected_gap = bounds.height / len(sr.CHANNELS)
    assert abs(avg_gap - expected_gap) < 2.0


def test_calibrate_uv_per_pixel_positive(clean_baselines):
    pixels_per_uv = sr.calibrate_uv_per_pixel(clean_baselines, spacing_uv=80.0)
    assert pixels_per_uv is not None
    assert pixels_per_uv > 0


def test_calibrate_uv_per_pixel_rejects_zero_spacing(clean_baselines):
    assert sr.calibrate_uv_per_pixel(clean_baselines, spacing_uv=0) is None
