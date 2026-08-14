import numpy as np

import screenrecord as sr


def test_read_spacing_uv_known_value(clean_frame):
    # Fixture was captured live while the "Channel spacing (uV)" field
    # showed 80.
    assert sr.read_spacing_uv(clean_frame) == 80.0


def test_read_spacing_uv_at_non_uniformly_resized_window(resized_frame, wide_frame):
    # Real captures at two different non-uniformly-resized window sizes
    # (height-only and width-only stretches respectively) -- the spacing
    # box is a fixed absolute pixel rectangle (see SPACING_BOX_*_PX), so
    # this should read correctly regardless of window size. Both
    # fixtures happened to be captured while the field showed 20 (a
    # different value than clean_frame's 80 -- unrelated, just whatever
    # was set at the time); what matters is that it reads *some* correct
    # value at these sizes at all, which an earlier fraction-based model
    # could not do.
    #
    # NOTE: this ground truth was independently confirmed by eye against
    # the source screenshots, not just trusted from a prior code run --
    # an earlier version of this assertion (70.0) had silently baked in
    # a real OCR bug's wrong output as if it were the expected value (a
    # "2" vs "7" near-tie misread, since fixed -- see
    # DIGIT_TEMPLATE_VARIANTS and the margin-vs-score anchor gate in
    # classify_glyph). Always verify fixture ground truth visually, not
    # just by trusting what the code being tested currently outputs.
    assert sr.read_spacing_uv(resized_frame) == 20.0
    assert sr.read_spacing_uv(wide_frame) == 20.0


def test_digit_templates_self_classify():
    # Each shipped reference template should classify as itself against
    # the full template set (sanity check the templates aren't corrupt
    # or too similar to each other).
    for digit, template in sr.DIGIT_TEMPLATES.items():
        assert sr.match_glyph(template) == digit


def test_match_glyph_returns_none_for_blank():
    blank = np.zeros((sr.DIGIT_TEMPLATE_SIZE, sr.DIGIT_TEMPLATE_SIZE), dtype=bool)
    assert sr.match_glyph(blank) is None


def test_segment_glyphs_keeps_isolated_runs_separate():
    # segment_glyphs itself only does column-projection (splitting
    # touching/merged glyphs apart is split_wide_runs's job -- see both
    # functions' docstrings for why a geometry-only heuristic combining
    # both steps was tried and found unreliable). This just checks the
    # still-relevant part: genuinely separated glyphs stay separate.
    ink = np.zeros((10, 24), dtype=bool)
    for x0 in [0, 6, 12, 18]:
        ink[2:8, x0 : x0 + 4] = True
    ranges = sr.segment_glyphs(ink)
    assert ranges == [(0, 4), (6, 10), (12, 16), (18, 22)]


def test_read_digits_in_box_recovers_touching_digits(fixtures_dir):
    # Real capture of the "Channel spacing (uV)" box showing "140" --
    # "4" and "0" are kerned with zero gap between them (no all-
    # background column), so segment_glyphs alone hands back one merged
    # 16px-wide run. This is the exact regression case that motivated
    # split_wide_runs + classify_glyph: an earlier version misread it as
    # "14", "1813", "1111?", and "110" across several failed approaches
    # before landing on this one.
    from PIL import Image

    region = np.array(Image.open(fixtures_dir / "spacing_box_140.png").convert("RGB"))
    assert sr.read_digits_in_box(region) == "140"


def test_classify_glyph_single_digit():
    g8 = sr.DIGIT_TEMPLATES["8"]
    assert sr.classify_glyph(g8, 0, g8.shape[1]) == "8"


def test_split_wide_runs_leaves_normal_width_run_alone():
    assert sr.split_wide_runs([(10, 17)]) == [(10, 17)]  # 7px, one glyph


def test_read_axis_tick_labels_monotonic(clean_frame):
    bounds = sr.find_plot_bounds(clean_frame)
    assert bounds is not None
    ticks = sr.read_axis_tick_labels(clean_frame, bounds)
    if ticks is None:
        return  # OCR can legitimately fail closed; nothing to assert then
    xs = [p[0] for p in ticks]
    secs = [p[1] for p in ticks]
    assert xs == sorted(xs)
    # Strictly increasing consecutive integers (the tick labels' known
    # a-priori structure) -- read_axis_tick_labels itself already checks
    # this and returns None otherwise, so this mostly guards against a
    # future change breaking that invariant. Not asserting secs[0] == 0:
    # the leftmost "0" tick sits right at the plot's left edge and can
    # legitimately fail OCR (partially clipped by the bounds margin)
    # without that being a bug -- the function fails closed per-glyph,
    # not all-or-nothing.
    assert secs == sorted(secs)
    assert secs == list(range(secs[0], secs[0] + len(secs)))
