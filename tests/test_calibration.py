import numpy as np

import screenrecord as sr


def _close(rgb, expected, tol=25):
    return rgb is not None and all(abs(a - b) <= tol for a, b in zip(rgb, expected))


def test_calibrate_channel_colors_af3_is_near_black_not_fringe(clean_frame, clean_baselines):
    # Regression test: AF3's legend text is near-black but renders with
    # ClearType-style blue/orange sub-pixel-AA fringe pixels at glyph
    # edges. An earlier "most saturated pixel" heuristic was fooled by
    # this into picking a saturated blue fringe pixel instead of the true
    # near-black fill -- this asserts the modal-color approach recovers
    # the true near-black (low-saturation) fill color instead.
    colors, _ = sr.calibrate_channel_colors(clean_frame, clean_baselines)
    af3 = colors["AF3"]
    assert af3 is not None
    r, g, b = af3
    # Near-black / low-saturation: channels close to each other, all dark.
    assert max(r, g, b) < 160
    assert max(r, g, b) - min(r, g, b) < 20


def test_calibrate_channel_colors_match_expected_palette(clean_frame, clean_baselines):
    colors, _ = sr.calibrate_channel_colors(clean_frame, clean_baselines)
    expected = {
        "F7": (43, 188, 58),
        "FC5": (249, 219, 8),
        "O2": (225, 87, 227),
        "F8": (255, 161, 58),
    }
    for ch, exp in expected.items():
        assert _close(colors[ch], exp), f"{ch}: got {colors[ch]}, expected near {exp}"


def test_calibrate_channel_colors_all_channels_readable(clean_frame, clean_baselines):
    colors, _ = sr.calibrate_channel_colors(clean_frame, clean_baselines)
    for ch in sr.CHANNELS:
        assert colors[ch] is not None, f"{ch} color unreadable"


def test_classify_quality_matches_legend_anchors():
    for tier, anchor in sr.QUALITY_LEGEND.items():
        assert sr.classify_quality(anchor) == tier


def test_classify_quality_none_when_far_from_all_anchors():
    assert sr.classify_quality((128, 128, 255)) is None


def test_modal_color_ignores_near_white_edge_pixels():
    # Mostly white patch with a small solid-color blob plus a few
    # near-white anti-aliased edge pixels that shouldn't win the vote.
    patch = np.full((20, 3), 255, dtype=np.uint8)
    patch[:10] = [200, 160, 4]  # solid "true" color, majority
    patch[10:13] = [245, 248, 250]  # near-white edge noise
    color = sr._modal_color(patch)
    assert color is not None
    assert abs(color[0] - 200) < 15 and abs(color[1] - 160) < 15
