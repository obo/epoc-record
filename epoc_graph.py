"""Shared EmotivPRO screen-reading machinery: X11 window/root-window
capture, digit-template OCR, plot-canvas geometry, per-channel curve
color calibration and digitization, capture stitching, and CSV output.

This module is intentionally passive: nothing in it sends synthetic
mouse/keyboard input. It is used by:

* ``epoc-record`` -- passively digitizes EmotivPRO's LIVE 14-channel EEG
  chart (see that script's own docstring).
* ``epoc-reread`` -- actively drives EmotivPRO's replay UI (via xdotool,
  in that script itself, never here) to isolate and re-digitize one
  electrode/motion-sensor curve at a time at higher fidelity; the
  clicking lives entirely in ``epoc-reread``, but the screen-reading
  primitives below (window capture, OCR, plot-bounds math, curve
  digitization, stitching) are shared as-is.

See README.md for the fixed-pixel-UI-geometry rationale behind the
constants below -- EmotivPRO's window does NOT scale uniformly on
resize: side panels/legends/chrome stay fixed pixel size, only the plot
canvas between them grows or shrinks.
"""

import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from Xlib import X, display
from Xlib.error import XError

# --- Fixed-pixel UI geometry ---------------------------------------------

WINDOW_TITLE_RE = re.compile(r"EmotivPRO")
# Two windows can match this title (a small helper/frame window plus the
# real chart window) -- filter to a sane minimum content size so we never
# pick the wrong one.
MIN_WINDOW_W, MIN_WINDOW_H = 400, 400

# Fixed channel order, top-to-bottom in both the plot and the legend, for
# the live all-channels EEG view.
CHANNELS = [
    "AF3", "F7", "F3", "FC5", "T7", "P7", "O1",
    "O2", "P8", "T8", "FC6", "F4", "F8", "AF4",
]

# Fixed sensor order for the Motion tab (the 4th icon in the left gray
# bar), confirmed live against a real EmotivPRO 4.11.3 session: quaternion
# components, then accelerometer, then magnetometer axes.
MOTION_CHANNELS = [
    "Q0", "Q1", "Q2", "Q3",
    "ACCX", "ACCY", "ACCZ",
    "MAGX", "MAGY", "MAGZ",
]

# "Channel spacing (uV)" input box digit area: a genuinely fixed absolute
# pixel rectangle, byte-for-byte identical across all 16 captures
# including every height tested -- the left control panel's content never
# moves or resizes at all; only the blank space below it grows/shrinks.
# Tight around just the digits themselves, deliberately excluding the
# "Channel spacing (uV)" label text above and the +/- stepper buttons to
# the right, both of which would otherwise contaminate glyph
# segmentation. (Coordinates are relative to this script's own
# root-window Xlib capture path -- note this is offset from what a
# window-manager screenshot tool like `import` reports, since
# get_geometry()/translate_coords() on the client window excludes
# decorations that `import -window` may include.)
SPACING_BOX_X0_PX, SPACING_BOX_Y0_PX = 82, 106
SPACING_BOX_X1_PX, SPACING_BOX_Y1_PX = 110, 124

# Plot canvas bounds, as fixed pixel margins from each edge. Channel
# baselines are NOT listed as fixed offsets: they're computed by
# find_channel_baselines as N rows proportionally spaced within this
# canvas's actual (fixed-margin-derived, so per-window-size) height --
# that part genuinely does stretch with the window, since the row
# spacing visibly grows on a taller window.
PLOT_X0_PX = 311              # fixed distance from the left edge
PLOT_X_RIGHT_MARGIN_PX = 88   # plot_x1 = window_width - this
PLOT_Y0_PX = 64               # fixed distance from the top edge
PLOT_Y_BOTTOM_MARGIN_PX = 131  # plot_y1 = window_height - this
PLOT_SATURATION_THRESHOLD = 25  # max-min RGB channel spread counted as "colorful" (curve) pixel

# Right-hand legend: dot and text horizontal position as fixed pixel
# distances from the RIGHT edge (the legend column is fixed-width,
# anchored right -- confirmed byte-identical across every window WIDTH
# tested, including width-only stretches to 1600px). Only present in the
# ALL-channels EEG view (epoc-record's live view); the single/few-channel
# view epoc-reread uses has no such legend column (see epoc-reread for
# its own, separately-calibrated single-channel-view geometry).
LEGEND_DOT_X0_FROM_RIGHT_PX, LEGEND_DOT_X1_FROM_RIGHT_PX = 61, 56
LEGEND_TEXT_X0_FROM_RIGHT_PX, LEGEND_TEXT_X1_FROM_RIGHT_PX = 45, 20
LEGEND_ROW_HALF_HEIGHT_PX = 8  # vertical +/- around each row's y, in pixels

# X-axis tick label row, just below the plot -- searched as a fixed pixel
# height below the detected plot bottom (that content doesn't stretch
# with window height either -- it's fixed-size UI text).
TICK_LABEL_BAND_HEIGHT_PX = 90
# Column gap (pixels) above which two adjacent tick-label glyphs are
# considered different ticks rather than digits of the same multi-digit
# tick (e.g. "10"). See read_axis_tick_labels for why this is a fixed
# constant rather than computed per-line.
TICK_LABEL_BREAK_PX = 20

# Fixed floating UI overlay (the "HRS" toggle + eye/visibility-toggle
# buttons) that EmotivPRO draws docked in the plot's top-right corner, on
# top of the chart -- confirmed live to sit directly over AF3's curve
# (AF3 is the topmost channel row) for roughly its most recent ~1.5-2s of
# samples, every single capture (it's a fixed UI element, not part of the
# data). Left unhandled, this causes two distinct problems, not just a
# clean gap in AF3: (1) the overlay's "HRS" label text is a saturated
# cyan close enough to some channels' curve colors (e.g. within
# CURVE_MATCH_THRESHOLD of F3's teal) to risk a spurious false-positive
# match against icon pixels instead of a clean "no data" for any channel
# whose search window reaches this corner -- not just AF3; (2) even where
# it wouldn't false-match, it's still wasted/misleading signal. The fix:
# digitize_capture blanks this rectangle to pure white in its working
# copy of the plot before per-channel matching, which both channels
# nearby and AF3 itself then correctly (and cheaply) read as "no match,"
# i.e. N/A -- never fabricated -- for every capture, permanently, which
# is the actually-correct behavior since the real curve pixels underneath
# are never visible on screen at all. Fixed pixel offsets from the top
# and right edges (confirmed identical across every captured size).
OVERLAY_ICON_X0_FROM_RIGHT_PX, OVERLAY_ICON_X1_FROM_RIGHT_PX = 250, 76
OVERLAY_ICON_Y0_PX, OVERLAY_ICON_Y1_PX = 48, 110

# Quality color legend, calibrated empirically against real EmotivPRO
# captures: a flat (79,78,79) gray dot was observed on every channel during
# a noisy/no-lock moment ("stale" anchor), and red/green dots were observed
# elsewhere ("poor"/"good" anchors).
QUALITY_LEGEND = {
    "good": (1, 195, 88),
    "fair": (161, 224, 93),
    "poor": (218, 0, 0),
    "stale": (79, 78, 79),
    "off": (0, 0, 0),
}
QUALITY_MATCH_THRESHOLD = 60  # Manhattan RGB distance
QUALITY_TIER_CODE = {"off": 0, "poor": 1, "fair": 2, "good": 3}

# Curve-color matching. Looser than the quality-dot threshold: overlapping
# anti-aliased lines blend colors more than a flat quality dot ever does.
CURVE_MATCH_THRESHOLD = 90
# Alpha-blend match parameters (see _alpha_blend_match docstring): minimum
# fraction-of-the-way from white to the target color a pixel must be, and
# the maximum Manhattan residual off that white->color line, to count as
# a match. Tuned empirically against a real quiet/low-amplitude capture
# (no one wearing the headset -- thin, mostly-1px anti-aliased strokes),
# where a flat CURVE_MATCH_THRESHOLD alone missed a large fraction of a
# channel's own genuine curve pixels (e.g. a partially-covered pixel
# blended ~60% toward white still IS that channel's curve, just fainter).
CURVE_MIN_ALPHA = 0.35
CURVE_RESIDUAL_THRESHOLD = 40

WINDOW_RESEARCH_INTERVAL = 2.0  # seconds between re-searches while window is lost

# Rolling plot window duration fallback (seconds) used only if the x-axis
# tick labels can't be OCR'd this capture. Empirically the visible window
# is close to 10s (observed tick labels ranging over "...9" to "...10"
# across different capture moments).
FALLBACK_WINDOW_SECONDS = 10.0


# --- Digit-template OCR --------------------------------------------------
# Reference bitmaps for digits 0-9, captured once offline from a real
# EmotivPRO x-axis tick-label row (which conveniently shows every digit
# 0-9 at once) and normalized (aspect-ratio-preserving pad + resize to a
# fixed 16x16 square) so matching is scale-invariant across window sizes.
# Cross-validated by eye against a same-session "Channel spacing (uV)"
# reading of "80" (a different UI element/font weight) using IoU distance
# -- top-1 match was correct for both digits. See the plan/README for how
# to recapture these if a future EmotivPRO version changes its font.
DIGIT_TEMPLATE_SIZE = 16
DIGIT_TEMPLATES_RAW = {
    "0": ("0000000000000000", "0000000000000000", "0000011110000000", "0000011111000000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000011111000000", "0000011110000000", "0000000000000000", "0000000000000000"),
    "1": ("0000000000000000", "0000000000000000", "0000000100000000", "0000001110000000", "0000010110000000", "0000000110000000", "0000000110000000", "0000000110000000", "0000000110000000", "0000000110000000", "0000000110000000", "0000000110000000", "0000011111100000", "0000011111100000", "0000000000000000", "0000000000000000"),
    "2": ("0000000000000000", "0000000000000000", "0000011110000000", "0000011111000000", "0000100000100000", "0000000000100000", "0000000000100000", "0000000001000000", "0000000000000000", "0000001100000000", "0000010000000000", "0000100000000000", "0000111111100000", "0000111111100000", "0000000000000000", "0000000000000000"),
    "3": ("0000000000000000", "0000000000000000", "0000011110000000", "0000011111000000", "0000100000100000", "0000000000100000", "0000000000100000", "0000000111000000", "0000000111000000", "0000000000100000", "0000000000100000", "0000100000100000", "0000011111000000", "0000011110000000", "0000000000000000", "0000000000000000"),
    "4": ("0000000000000000", "0000000000000000", "0000000000000000", "0000000011000000", "0000000111000000", "0000001001000000", "0000010001000000", "0000100001000000", "0000100011000000", "0000111111100000", "0000000001000000", "0000000001000000", "0000000001000000", "0000000000000000", "0000000000000000", "0000000000000000"),
    "5": ("0000000000000000", "0000000000000000", "0000111111100000", "0000111111100000", "0000100000000000", "0000100000000000", "0000111111000000", "0000000000100000", "0000000000100000", "0000000000100000", "0000000000100000", "0000100000100000", "0000011111000000", "0000011110000000", "0000000000000000", "0000000000000000"),
    "6": ("0000000000000000", "0000000000000000", "0000000110000000", "0000001111000000", "0000010000000000", "0000100000000000", "0000111111000000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000011111000000", "0000011110000000", "0000000000000000", "0000000000000000"),
    "7": ("0000000000000000", "0000000000000000", "0000111111100000", "0000111111100000", "0000000000100000", "0000000001000000", "0000000001000000", "0000000010000000", "0000000110000000", "0000000100000000", "0000001000000000", "0000001000000000", "0000010000000000", "0000010000000000", "0000000000000000", "0000000000000000"),
    "8": ("0000000000000000", "0000000000000000", "0000011110000000", "0000011111000000", "0000100000100000", "0000100000100000", "0000100000100000", "0000011111000000", "0000011111000000", "0000100000100000", "0000100000100000", "0000100000100000", "0000011111000000", "0000011110000000", "0000000000000000", "0000000000000000"),
    "9": ("0000000000000000", "0000000000000000", "0000011110000000", "0000011111000000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100000100000", "0000100001100000", "0000011111100000", "0000000000100000", "0000000001000000", "0000011110000000", "0000011100000000", "0000000000000000", "0000000000000000"),
}
DIGIT_TEMPLATES = {
    d: np.array([[c == "1" for c in row] for row in rows], dtype=bool)
    for d, rows in DIGIT_TEMPLATES_RAW.items()
}

# Extra confirmed-real template variants for digits whose single
# tick-label-derived template above doesn't generalize well to other font
# contexts (the spacing box renders at a different size/weight). Matching
# takes the best score across a digit's primary template AND its variants
# (see _glyph_scores), rather than replacing the primary -- both are
# real, ground-truth-confirmed renderings, just from different UI
# elements, and keeping both avoids trading one context's accuracy for
# another's.
#
# "2": the tick-label-derived template scored a genuine near-tie against
# "7" (0.455 vs 0.449, margin 0.006 -- nowhere near DIGIT_MIN_MARGIN) when
# matching a real spacing-box "2", confirmed live (a whole session's
# "Channel spacing (uV)" reading of 20 came back unreadable). This
# variant is that exact glyph, extracted and ground-truth-confirmed by
# eye against the source screenshot.
#
# "-" (minus sign): needed only for epoc-reread's autoscale-pass Y-axis
# OCR (epoc-record's own OCR targets -- x-axis seconds, channel spacing --
# are never negative). A minus sign is just a short horizontal bar
# vertically centered in the glyph cell; captured as a hand-built
# reference shape at the same 16x16 normalized geometry as the digit
# templates (not extracted from a real capture, since a synthetic bar is
# an exact, unambiguous match for what a minus glyph looks like at any
# render size once normalized -- unlike digits, there's no shape
# variation to get wrong).
DIGIT_TEMPLATE_VARIANTS_RAW: Dict[str, List[Tuple[str, ...]]] = {
    "2": [
        (
            "0000000000000000", "0000000000000000", "0000011111000000", "0000011111100000",
            "0000011111100000", "0000000001100000", "0000000001100000", "0000000001100000",
            "0000000011000000", "0000000110000000", "0000001100000000", "0000011000000000",
            "0000011000000000", "0000011111100000", "0000000000000000", "0000000000000000",
        ),
    ],
}
DIGIT_TEMPLATE_VARIANTS: Dict[str, List[np.ndarray]] = {
    d: [np.array([[c == "1" for c in row] for row in rows], dtype=bool) for rows in variants]
    for d, variants in DIGIT_TEMPLATE_VARIANTS_RAW.items()
}

MINUS_TEMPLATE_RAW = (
    # Real glyph, not synthetic: a hand-built bar shape was tried first
    # and confirmed live to score below "4"/"9"/"2" on a real "-80"
    # reading in the Amplitude min field -- its proportions/position
    # after normalize_glyph's aspect-preserving resize didn't match a
    # real minus sign closely enough. This is the actual glyph, extracted
    # from that same live capture the same way DIGIT_TEMPLATE_VARIANTS's
    # "2" was (see its comment) -- ground-truth-confirmed by eye against
    # the source screenshot.
    "0000000000000000", "0000000000000000", "0000000000000000", "0000111111110000",
    "0001111111111000", "0001111111111000", "0001111111111000", "0001111111111000",
    "0000111111110000", "0000011111100000", "0000000000000000", "0000000000000000",
    "0000000000000000", "0000000000000000", "0000000000000000", "0000000000000000",
)
MINUS_TEMPLATE = np.array([[c == "1" for c in row] for row in MINUS_TEMPLATE_RAW], dtype=bool)
DIGIT_TEMPLATES_WITH_MINUS: Dict[str, np.ndarray] = dict(DIGIT_TEMPLATES, **{"-": MINUS_TEMPLATE})

# Minimum top-1 IoU score and minimum margin over the runner-up for an OCR
# read to be accepted -- calibrated empirically (cross-context matches
# against real captures scored ~0.27-0.43 IoU with a clear top-1 margin;
# thresholds set comfortably below that to tolerate font/AA variation
# while still rejecting genuinely ambiguous glyphs).
DIGIT_MIN_SCORE = 0.12
DIGIT_MIN_MARGIN = 0.03


def color_dist(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def classify_quality(rgb: Optional[Tuple[int, int, int]]) -> Optional[str]:
    if rgb is None:
        return None
    best_tier, best_dist = None, QUALITY_MATCH_THRESHOLD + 1
    for tier, anchor in QUALITY_LEGEND.items():
        d = color_dist(rgb, anchor)
        if d < best_dist:
            best_tier, best_dist = tier, d
    return best_tier if best_dist <= QUALITY_MATCH_THRESHOLD else None


def normalize_glyph(mask: np.ndarray, size: int = DIGIT_TEMPLATE_SIZE) -> np.ndarray:
    """Pad a boolean glyph crop to a square (centered, with margin) and
    resize to a fixed size, preserving aspect ratio -- critical for
    telling apart narrow glyphs (e.g. '1') from wide ones without
    distortion, and for making matching scale-invariant across window
    sizes."""
    h, w = mask.shape
    if h == 0 or w == 0:
        return np.zeros((size, size), dtype=bool)
    side = max(h, w)
    pad = max(1, side // 4)
    canvas = np.zeros((side + 2 * pad, side + 2 * pad), dtype=np.uint8)
    y0 = pad + (side - h) // 2
    x0 = pad + (side - w) // 2
    canvas[y0 : y0 + h, x0 : x0 + w] = mask.astype(np.uint8) * 255
    im = Image.fromarray(canvas).resize((size, size), Image.BILINEAR)
    return np.array(im) > 128


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.count_nonzero(a & b)
    union = np.count_nonzero(a | b)
    return inter / union if union else 0.0


def _glyph_scores(
    norm: np.ndarray,
    templates: Dict[str, np.ndarray] = DIGIT_TEMPLATES,
    variants: Dict[str, List[np.ndarray]] = DIGIT_TEMPLATE_VARIANTS,
) -> Dict[str, float]:
    """IoU (intersection-over-union on ink pixels) score against every
    reference digit template -- taking the BEST score across a digit's
    primary template and any extra confirmed-real variants for it (see
    DIGIT_TEMPLATE_VARIANTS), not just the primary alone.

    ``variants`` defaults to the shared module-level set but can be
    overridden by a caller with its own context-specific variants (e.g.
    a font at a different render size where the shared set's glyph for
    some digit doesn't match well) WITHOUT mutating the shared set --
    confirmed live that a variant tuned for one context (a small
    Amplitude-field "5") can actively misclassify a different context's
    real digits (broke a "140" spacing-box reading into "155")."""
    scores = {}
    for d, t in templates.items():
        best = _iou(norm, t)
        for variant in variants.get(d, ()):
            best = max(best, _iou(norm, variant))
        scores[d] = best
    return scores


def match_glyph(norm: np.ndarray, templates: Dict[str, np.ndarray] = DIGIT_TEMPLATES) -> Optional[str]:
    """Nearest-template match by IoU. Returns None (never guesses) if the
    best match isn't confidently ahead of the runner-up, or too weak in
    absolute terms."""
    ranked = sorted(_glyph_scores(norm, templates).items(), key=lambda kv: -kv[1])
    if not ranked:
        return None
    best_d, best_s = ranked[0]
    runner_s = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_s < DIGIT_MIN_SCORE or (best_s - runner_s) < DIGIT_MIN_MARGIN:
        return None
    return best_d


def segment_glyphs(ink: np.ndarray) -> List[Tuple[int, int]]:
    """Column-projection segmentation: contiguous runs of ink-bearing
    columns, separated by at least one all-background column. A run may
    still contain more than one glyph if adjacent digits are kerned
    tightly enough to share no all-background column (e.g. "10", or "40"
    at small font sizes) -- splitting those apart is `split_wide_runs`'s
    job, not this function's."""
    if ink.size == 0:
        return []
    col_has_ink = ink.any(axis=0)
    runs = []
    start = None
    for x, v in enumerate(col_has_ink):
        if v and start is None:
            start = x
        elif not v and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(col_has_ink)))
    return runs


# Typical single-digit width in pixels, calibrated empirically against
# real captures at the ~1024px-wide reference window (this app has not
# been observed to be resizable, so a fixed pixel constant rather than a
# frame-scaled one is an acceptable simplification for now): the
# "Channel spacing" box's digits measured ~7px wide, x-axis tick-label
# digits ~5-6px -- 7 is a shared estimate since split boundaries get
# refined by classify_glyph's small local search below anyway, so being
# off by a pixel or two doesn't need to be exact.
TYPICAL_GLYPH_WIDTH_PX = 7
# A run wider than this multiple of TYPICAL_GLYPH_WIDTH_PX is assumed to
# be >1 touching glyph and gets split.
WIDE_RUN_SPLIT_RATIO = 1.4
# How far (in pixels) either edge of a post-split glyph boundary is
# allowed to shift during classify_glyph's local refinement search.
GLYPH_BOUNDARY_SLOP_PX = 2
# Minimum score improvement a shifted boundary must show over the exact
# given boundary before classify_glyph will prefer it.
GLYPH_BOUNDARY_MARGIN = 0.08
# An anchor boundary scoring at or above this is trusted outright, with
# no neighborhood search at all -- calibrated below the weakest
# real-digit score observed at a correct boundary ("4", ~0.35-0.39) so a
# correct-but-weak anchor is never second-guessed.
GLYPH_ANCHOR_CONFIDENT_SCORE = 0.3


def split_wide_runs(runs: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Splits any run wider than WIDE_RUN_SPLIT_RATIO * TYPICAL_GLYPH_WIDTH_PX
    into `round(width / TYPICAL_GLYPH_WIDTH_PX)` equal-ish parts (touching
    digits, e.g. "10" or "40" kerned with zero gap between them).

    This uses a single FIXED calibrated constant rather than a per-call
    median over the runs being split, deliberately: an earlier version
    computed the "typical width" from the median of whatever runs were on
    the same line, which is only robust with many samples -- with as few
    as 2-3 runs (common: e.g. a 2-3 character field, or a handful of
    tick labels), a single outlier (either the wide merged run itself, or
    a narrow "1") skews the median enough to hide the merge entirely, and
    was confirmed live to do so both ways. Boundaries from equal division
    don't need to be pixel-perfect since classify_glyph (called on each
    resulting range) does a small local search to refine them."""
    out = []
    for x0, x1 in runs:
        w = x1 - x0
        n = max(1, round(w / TYPICAL_GLYPH_WIDTH_PX))
        if n <= 1 or w <= WIDE_RUN_SPLIT_RATIO * TYPICAL_GLYPH_WIDTH_PX:
            out.append((x0, x1))
        else:
            step = w / n
            for i in range(n):
                out.append((int(x0 + i * step), int(x0 + (i + 1) * step)))
    return out


def classify_glyph(
    shape_mask: np.ndarray,
    x0: int,
    x1: int,
    templates: Dict[str, np.ndarray] = DIGIT_TEMPLATES,
    variants: Dict[str, List[np.ndarray]] = DIGIT_TEMPLATE_VARIANTS,
) -> str:
    """Classifies a single glyph given an approximate (x0, x1) column
    range into shape_mask (e.g. from split_wide_runs, possibly off by a
    pixel or two at a touching-glyph split point): searches a small local
    neighborhood of the given left/right edges (+/- GLYPH_BOUNDARY_SLOP_PX)
    and keeps whichever variant scores best against the template set, to
    absorb that imprecision. Returns '?' (never guesses) if nothing in
    the neighborhood is confident.

    ``templates`` defaults to the digit-only set; pass
    DIGIT_TEMPLATES_WITH_MINUS to also recognize a leading minus sign
    (needed for OCR'ing negative axis values, never for epoc-record's own
    always-non-negative OCR targets).

    Deliberately anchored on the exact given boundary, not a free search
    over the neighborhood: an earlier version simply kept whichever (lo,
    hi) in the neighborhood scored highest, which reintroduced the same
    bias a wide-open search has -- a neighboring crop that clips off part
    of the glyph is often thin/vertical-stroke-shaped and can spuriously
    out-score the correct exact boundary against the "1" template.
    Confirmed live: it corrupted an already-correctly-segmented "80"
    (exact gap-separated boundaries from segment_glyphs) into "83". So the
    exact given boundary is scored first as an anchor, and a neighbor is
    only used if it beats the anchor by GLYPH_BOUNDARY_MARGIN -- enough to
    fix a genuinely imprecise equal-division boundary (from
    split_wide_runs, e.g. the "10"/"40" touching-glyph case) without ever
    overriding a boundary that was already precise."""
    h, w = shape_mask.shape

    def score_at(lo: int, hi: int) -> Optional[Tuple[float, str, float]]:
        glyph = shape_mask[:, lo:hi]
        rows_ink = np.where(glyph.any(axis=1))[0]
        if rows_ink.size == 0:
            return None
        glyph = glyph[rows_ink.min() : rows_ink.max() + 1]
        norm = normalize_glyph(glyph)
        scores = _glyph_scores(norm, templates, variants)
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top_d, top_s = ranked[0]
        runner_s = ranked[1][1] if len(ranked) > 1 else 0.0
        return (top_s, top_d, top_s - runner_s)

    anchor = score_at(x0, min(x1, w)) or (0.0, "?", 0.0)
    best = anchor
    # Only search the neighborhood at all if the anchor itself isn't
    # already a confident read. This matters because different digits
    # have very different achievable IoU scores against this template set
    # -- "4" tops out around 0.35-0.39 even at its ideal, correctly-
    # segmented boundary, well below what a spuriously-matching narrow
    # "1"-like sliver can reach (~0.47) elsewhere in the neighborhood.
    # Searching unconditionally let a low-but-already-correct anchor lose
    # to that kind of accidental match; requiring the anchor to be weak
    # first means a precise boundary's already-correct read is never
    # second-guessed, and the search only ever fires to rescue a
    # genuinely bad (e.g. equal-division) boundary.
    #
    # Also gate on the anchor's OWN margin, not just its raw score:
    # confirmed live that a score can cross GLYPH_ANCHOR_CONFIDENT_SCORE
    # by a hair (0.302 vs the 0.3 cutoff) while still being a near-tie
    # with the runner-up (margin 0.0035, nowhere near DIGIT_MIN_MARGIN) --
    # score-only gating treated that as "confident enough to stop," which
    # skipped the neighborhood search that would have found the correct
    # boundary (a much higher-confidence "0" one column over) and instead
    # locked in the anchor's own unreliable pick.
    if anchor[0] < GLYPH_ANCHOR_CONFIDENT_SCORE or anchor[2] < DIGIT_MIN_MARGIN:
        for lo in range(max(0, x0 - GLYPH_BOUNDARY_SLOP_PX), min(x0 + GLYPH_BOUNDARY_SLOP_PX, x1 - 1) + 1):
            hi_min = max(lo + 1, x1 - GLYPH_BOUNDARY_SLOP_PX)
            hi_max = min(w, x1 + GLYPH_BOUNDARY_SLOP_PX)
            for hi in range(hi_min, hi_max + 1):
                if lo == x0 and hi == x1:
                    continue
                candidate = score_at(lo, hi)
                if candidate is not None and candidate[0] > best[0] + GLYPH_BOUNDARY_MARGIN:
                    best = candidate
    score, digit, margin = best
    # Both an absolute-confidence floor AND a margin over the runner-up
    # are required -- matching match_glyph's rule (see DIGIT_MIN_MARGIN).
    # Confirmed live this margin check was missing here: a "2" scored
    # 0.449 against a "7" scoring 0.455 (margin 0.006, nowhere near
    # DIGIT_MIN_MARGIN=0.03) and silently misread as "7" instead of
    # failing closed as "?".
    if score >= DIGIT_MIN_SCORE and margin >= DIGIT_MIN_MARGIN:
        return digit
    return "?"


def _ink_masks(rgb: np.ndarray, dark_thresh: int = 210, loose_thresh: int = 225, gray_tol: int = 20):
    """Two ink masks over the same region: a strict gray+dark mask used
    ONLY for segmenting glyphs apart (excluding ClearType-style
    blue/orange sub-pixel-AA fringe pixels at glyph edges keeps a real
    background gap between adjacent digits, which a hue-blind mask
    bridges into one merged blob -- confirmed empirically), and a looser
    hue-agnostic mask used for the glyph SHAPE itself once segmented
    (within an already-known x-range, so there's no bridging risk, and
    including the fringe pixels recovers the true glyph outline that the
    strict mask alone would clip/distort -- also confirmed empirically,
    e.g. a "0" losing its right arc and being misread as "6")."""
    r = rgb[..., 0].astype(int)
    g = rgb[..., 1].astype(int)
    b = rgb[..., 2].astype(int)
    gray_ish = (np.abs(r - g) < gray_tol) & (np.abs(g - b) < gray_tol) & (np.abs(r - b) < gray_tol)
    seg_mask = gray_ish & (rgb.min(axis=2) < dark_thresh)
    shape_mask = rgb.min(axis=2) < loose_thresh
    return seg_mask, shape_mask


def read_digits_in_box(
    rgb: np.ndarray, templates: Dict[str, np.ndarray] = DIGIT_TEMPLATES
) -> Optional[str]:
    """OCR a horizontal run of gray/dark digits on a light background
    within an (h, w, 3) RGB array. Returns the digit string, or None if
    nothing readable was segmented, or a substring of digits with '?' in
    place of any single unreadable glyph (caller decides whether a partial
    read is usable). Pass templates=DIGIT_TEMPLATES_WITH_MINUS to also
    recognize a leading minus sign."""
    if rgb.size == 0:
        return None
    seg_mask, shape_mask = _ink_masks(rgb)
    rows_with_ink = np.where(seg_mask.any(axis=1))[0]
    if rows_with_ink.size == 0:
        return None
    y0, y1 = rows_with_ink.min(), rows_with_ink.max() + 1
    seg_mask = seg_mask[y0:y1]
    shape_mask = shape_mask[y0:y1]
    glyph_ranges = split_wide_runs(segment_glyphs(seg_mask))
    if not glyph_ranges:
        return None
    chars = [classify_glyph(shape_mask, x0, x1, templates) for x0, x1 in glyph_ranges]
    return "".join(chars)


# --- Window finding + root-window capture --------------------------------


def find_window(win):
    """Recursively search the X window tree for a window whose title
    matches WINDOW_TITLE_RE and whose geometry looks like the real chart
    window (not a small helper/frame window sharing the same title
    prefix), filtered by a minimum-size check."""
    try:
        name = win.get_wm_name()
    except (XError, Exception):
        name = None
    if name and WINDOW_TITLE_RE.search(str(name)):
        try:
            geo = win.get_geometry()
            attrs = win.get_attributes()
            if (
                geo.width >= MIN_WINDOW_W
                and geo.height >= MIN_WINDOW_H
                and attrs.map_state == X.IsViewable
            ):
                return win
        except (XError, Exception):
            pass
    try:
        children = win.query_tree().children
    except (XError, Exception):
        return None
    for child in children:
        found = find_window(child)
        if found is not None:
            return found
    return None


@dataclass
class Frame:
    rgb: np.ndarray  # (H, W, 3) uint8
    width: int
    height: int
    capture_ts_ns: int


class Capture:
    """Finds the EmotivPRO window and captures its content via the ROOT
    window (not the app's own window -- see module docstring for why:
    this desktop's non-compositing WM returns stale/black pixels for
    occluded regions of a window captured directly via its own XID, while
    capturing the root window and cropping reflects true on-screen
    content). Window geometry is re-read fresh on every capture since the
    window can move/resize/scroll at any time."""

    def __init__(self, d):
        self.d = d
        self.root = d.screen().root
        self.root_w = d.screen().width_in_pixels
        self.root_h = d.screen().height_in_pixels
        self.window = None
        self.last_search = 0.0

    def locate(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self.last_search) < WINDOW_RESEARCH_INTERVAL:
            return
        self.last_search = now
        try:
            win = find_window(self.d.screen().root)
        except (XError, Exception):
            win = None
        self.window = win

    def capture_frame(self, verbose: bool = False) -> Optional[Frame]:
        self.locate()
        if self.window is None:
            return None
        try:
            geo = self.window.get_geometry()
            if geo.width < MIN_WINDOW_W or geo.height < MIN_WINDOW_H:
                self.window = None
                return None
            attrs = self.window.get_attributes()
            if attrs.map_state != X.IsViewable:
                self.window = None
                return None
            coords = self.root.translate_coords(self.window, 0, 0)
            x0, y0 = coords.x, coords.y
            w, h = geo.width, geo.height
        except (XError, Exception):
            self.window = None
            return None

        # Clamp to root bounds in case the window is partially off-screen.
        cx0 = max(0, x0)
        cy0 = max(0, y0)
        cx1 = min(self.root_w, x0 + w)
        cy1 = min(self.root_h, y0 + h)
        if cx1 <= cx0 or cy1 <= cy0:
            return None

        try:
            img = self.root.get_image(cx0, cy0, cx1 - cx0, cy1 - cy0, X.ZPixmap, 0xFFFFFFFF)
        except (XError, Exception):
            self.window = None
            return None
        ts_ns = time.time_ns()

        cw, ch = cx1 - cx0, cy1 - cy0
        arr = np.frombuffer(img.data, dtype=np.uint8)
        expected = cw * ch * 4
        if arr.size < expected:
            return None
        arr = arr[:expected].reshape(ch, cw, 4)
        rgb = arr[:, :, [2, 1, 0]]  # BGRX -> RGB

        # If the captured rect (clamped to screen) is smaller than the
        # window's own geometry (partially off-screen), pad with zeros so
        # fraction-based coordinates below still map consistently to the
        # *window's* full width/height rather than the clipped capture.
        if cw != w or ch != h:
            padded = np.zeros((h, w, 3), dtype=np.uint8)
            oy = cy0 - y0
            ox = cx0 - x0
            padded[oy : oy + ch, ox : ox + cw] = rgb
            rgb = padded

        return Frame(
            rgb=rgb,
            width=w,
            height=h,
            capture_ts_ns=ts_ns,
        )


# --- Plot-area + baseline detection --------------------------------------


@dataclass
class PlotBounds:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


def find_plot_bounds(frame: Frame, verbose: bool = False) -> Optional[PlotBounds]:
    """The plot canvas's pixel bounds, from the fixed-pixel margins
    calibrated against 16 real captures spanning 8 window sizes (see the
    "Fixed-pixel UI geometry" comment block) -- direct arithmetic, not
    detection, since the canvas's offset from each edge doesn't vary with
    window size at all. (An earlier version searched for the canvas via a
    "colorful curve pixels" bounding box within a fraction-of-window-size
    ROI; that broke under non-uniform resize because the ROI itself, and
    the legend-span cross-check it was validated against, were both
    fraction-based and silently misaligned as soon as width and height
    stopped scaling by the same factor.)"""
    x0 = PLOT_X0_PX
    x1 = frame.width - PLOT_X_RIGHT_MARGIN_PX
    y0 = PLOT_Y0_PX
    y1 = frame.height - PLOT_Y_BOTTOM_MARGIN_PX
    if x1 <= x0 or y1 <= y0:
        if verbose:
            sys.stderr.write(
                f"[plot-bounds] window {frame.width}x{frame.height} too small "
                "for the fixed UI margins to leave any plot canvas\n"
            )
        return None
    return PlotBounds(x0, y0, x1, y1)


def find_channel_baselines(bounds: PlotBounds, channels: List[str] = CHANNELS) -> Dict[str, int]:
    """Each channel's plot baseline row: len(channels) rows spaced
    proportionally within the plot canvas's actual height. Confirmed
    empirically (see the "Fixed-pixel UI geometry" comment block) that
    this per-row spacing itself stretches with window height -- unlike
    the canvas's own top/bottom offsets, which are fixed -- so this must
    be computed from the already-resolved canvas bounds, not from window
    height directly.

    ``channels`` defaults to the 14 EEG channels (epoc-record's live
    all-channels view); epoc-reread passes a single-element list for its
    isolated-curve captures."""
    n = len(channels)
    height = bounds.y1 - bounds.y0
    return {ch: int(bounds.y0 + (i + 0.5) / n * height) for i, ch in enumerate(channels)}


def calibrate_uv_per_pixel(baselines: Dict[str, int], spacing_uv: float) -> Optional[float]:
    if spacing_uv <= 0:
        return None
    ys = sorted(baselines.values())
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    if not gaps:
        return None
    gaps.sort()
    median_gap = gaps[len(gaps) // 2]
    if median_gap <= 0:
        return None
    return median_gap / spacing_uv


def blank_channel_overlay(plot_rgb: np.ndarray, frame: Frame, bounds: PlotBounds) -> None:
    """Blanks the fixed HRS/eye-icon UI overlay (see OVERLAY_ICON_*_PX)
    to pure white, in place, within a plot-local RGB array. Pure white is
    far enough (Manhattan distance) from every real curve color that no
    channel's color match can land there, so affected columns cleanly
    read as N/A for whichever channel(s) the overlay currently covers,
    instead of risking a spurious match against the overlay's own cyan
    text / gray icon pixels."""
    ox0 = frame.width - OVERLAY_ICON_X0_FROM_RIGHT_PX
    ox1 = frame.width - OVERLAY_ICON_X1_FROM_RIGHT_PX
    oy0, oy1 = OVERLAY_ICON_Y0_PX, OVERLAY_ICON_Y1_PX
    lx0 = max(0, ox0 - bounds.x0)
    lx1 = min(plot_rgb.shape[1], ox1 - bounds.x0)
    ly0 = max(0, oy0 - bounds.y0)
    ly1 = min(plot_rgb.shape[0], oy1 - bounds.y0)
    if lx1 > lx0 and ly1 > ly0:
        plot_rgb[ly0:ly1, lx0:lx1] = 255


def read_spacing_uv(frame: Frame, verbose: bool = False) -> Optional[float]:
    region = frame.rgb[SPACING_BOX_Y0_PX : SPACING_BOX_Y1_PX, SPACING_BOX_X0_PX : SPACING_BOX_X1_PX]
    digits = read_digits_in_box(region)
    if not digits or "?" in digits:
        if verbose:
            sys.stderr.write(f"[ocr] spacing box unreadable (got {digits!r})\n")
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    return value if value > 0 else None


def read_axis_tick_labels(frame: Frame, bounds: PlotBounds, verbose: bool = False) -> Optional[List[Tuple[int, int]]]:
    """OCR the x-axis tick labels below the plot. Returns a list of
    (pixel_x_in_frame, seconds) pairs for successfully-read ticks, or None
    if nothing usable was read. Cross-checked for the known a-priori
    structure (increasing consecutive integers from 0)."""
    y0 = bounds.y1
    y1 = min(frame.height, y0 + TICK_LABEL_BAND_HEIGHT_PX)
    if y1 <= y0:
        return None
    region = frame.rgb[y0:y1, bounds.x0 : bounds.x1]
    seg_mask, shape_mask = _ink_masks(region)
    if not seg_mask.any():
        return None
    # The search band (TICK_LABEL_BAND_HEIGHT_PX below the plot) is
    # deliberately generous and captures BOTH the tick-number row and the
    # "Time (s)" axis caption below it -- confirmed live these are two
    # separate row-bands with a clear all-background gap between them.
    # Using the full min..max span of ink rows merges both into one
    # column-projection pass, contaminating tick-digit segmentation with
    # caption letters. Take only the FIRST (topmost) contiguous ink
    # row-band -- the tick numbers always sit immediately below the plot,
    # above the caption.
    row_has_ink = seg_mask.any(axis=1)
    ry0 = int(np.argmax(row_has_ink))
    ry1 = ry0
    while ry1 < len(row_has_ink) and row_has_ink[ry1]:
        ry1 += 1
    seg_mask = seg_mask[ry0:ry1]
    shape_mask = shape_mask[ry0:ry1]
    glyph_ranges = segment_glyphs(seg_mask)
    if not glyph_ranges:
        return None

    # Group adjacent single-digit glyphs into multi-digit labels: gaps
    # between glyphs of the SAME label are near-zero (kerning), gaps
    # between DIFFERENT labels (tick spacing) are much larger. Use a
    # fixed calibrated threshold, not a relative one computed from the
    # gaps on this line: a per-line median was tried and found to
    # misfire live whenever every visible tick happens to be a lone
    # single digit (e.g. "0".."9" with no "10" yet in view) -- gaps are
    # then all roughly equal (inter-tick spacing), so a relative
    # "median * 3" threshold never exceeds itself and every digit gets
    # merged into one giant bogus label. TICK_LABEL_BREAK_PX sits
    # comfortably above real intra-digit kerning (a few px at most) and
    # comfortably below real inter-tick spacing (tens of px, since ~10
    # ticks are spread across the plot's ~600px width).
    labels: List[List[Tuple[int, int]]] = [[glyph_ranges[0]]]
    for prev, cur in zip(glyph_ranges, glyph_ranges[1:]):
        if cur[0] - prev[1] > TICK_LABEL_BREAK_PX:
            labels.append([])
        labels[-1].append(cur)

    results = []
    for group in labels:
        sub_ranges = split_wide_runs(group)
        text = "".join(classify_glyph(shape_mask, x0, x1) for x0, x1 in sub_ranges)
        if "?" in text or not text:
            continue
        try:
            seconds = int(text)
        except ValueError:
            continue
        center_x = (group[0][0] + group[-1][1]) // 2 + bounds.x0
        results.append((center_x, seconds))

    if len(results) < 2:
        if verbose:
            sys.stderr.write(f"[ocr] tick labels: only {len(results)} readable, need >=2\n")
        return None

    # Self-consistency: strictly increasing seconds, left to right.
    results.sort(key=lambda p: p[0])
    for (x0, s0), (x1, s1) in zip(results, results[1:]):
        if not (s1 > s0 and x1 > x0):
            if verbose:
                sys.stderr.write(f"[ocr] tick labels not monotonic: {results}\n")
            return None
    return results


# --- Channel color / quality calibration ----------------------------------


def _modal_color(
    pixels: np.ndarray, bucket: int = 24, white_dist_thresh: int = 40
) -> Optional[Tuple[int, int, int]]:
    """Modal (most-frequent) color among non-near-white pixels, quantized
    to coarse buckets to merge near-identical anti-aliased shades before
    voting, then averaged in full precision within the winning bucket.

    Deliberately NOT a "most saturated pixel" heuristic -- that approach
    was tried and failed for near-black text, whose ClearType-style
    sub-pixel antialiasing produces a minority of blue/orange fringe
    pixels at glyph edges that a max-saturation pick incorrectly prefers
    over the true (low-saturation, near-black) fill color.

    Two details tuned empirically against real captures, both needed --
    without them a light/thin-stroke glyph (e.g. a gold "O1") can lose to
    a near-white anti-aliased-edge bucket instead:
    * "near-white" is a SUM-of-channel-distance-from-white test, not a
      per-channel one -- a per-channel `all(channel > 235)` test lets
      pixels through where just one channel dips slightly under the cutoff
      (common at glyph edges), which then out-votes the true fill color
      since edge pixels vastly outnumber interior fill pixels for small
      fonts.
    * the quantization bucket is coarse (24, not 8) -- anti-aliasing
      produces a gradient of many *slightly* different shades of the true
      color; too fine a bucket fragments those votes across many buckets
      so none wins clearly.
    """
    if pixels.size == 0:
        return None
    dist_from_white = (255 - pixels.astype(np.int32)).sum(axis=1)
    fg = pixels[dist_from_white > white_dist_thresh]
    if fg.shape[0] == 0:
        return None
    quantized = (fg.astype(np.int32) // bucket)
    keys = quantized[:, 0] * 10000 + quantized[:, 1] * 100 + quantized[:, 2]
    unique, counts = np.unique(keys, return_counts=True)
    winner_key = unique[np.argmax(counts)]
    mask = keys == winner_key
    winning_pixels = fg[mask]
    mean = winning_pixels.mean(axis=0)
    return int(mean[0]), int(mean[1]), int(mean[2])


def calibrate_channel_colors(
    frame: Frame, baselines: Dict[str, int]
) -> Tuple[Dict[str, Optional[Tuple[int, int, int]]], Dict[str, Optional[str]]]:
    """For each channel's legend row, calibrate (a) the curve identity
    color from the modal color of the name-label text, and (b) the
    current quality tier from the dot color. Run fresh every capture.

    Only meaningful for the live ALL-channels EEG view, which has a
    fixed-position right-hand legend column (see LEGEND_*_FROM_RIGHT_PX);
    epoc-reread's isolated single-curve captures use a different,
    simpler color/ink detection (see epoc-reread's own
    find_lone_curve_pixels) since there's no legend to calibrate against
    when only one curve is ever on screen."""
    half_h = LEGEND_ROW_HALF_HEIGHT_PX
    colors: Dict[str, Optional[Tuple[int, int, int]]] = {}
    quality: Dict[str, Optional[str]] = {}

    dot_x = frame.width - (LEGEND_DOT_X0_FROM_RIGHT_PX + LEGEND_DOT_X1_FROM_RIGHT_PX) // 2
    text_x0 = frame.width - LEGEND_TEXT_X0_FROM_RIGHT_PX
    text_x1 = frame.width - LEGEND_TEXT_X1_FROM_RIGHT_PX

    for ch, y in baselines.items():
        y0 = max(0, y - half_h)
        y1 = min(frame.height, y + half_h)

        # Quality dot: small patch around (dot_x, y).
        dx0, dx1 = max(0, dot_x - 6), min(frame.width, dot_x + 6)
        dot_patch = frame.rgb[y0:y1, dx0:dx1].reshape(-1, 3)
        dot_color = _modal_color(dot_patch, bucket=4)
        quality[ch] = classify_quality(dot_color)

        # Curve identity color: modal non-white color of the name label.
        text_patch = frame.rgb[y0:y1, text_x0:text_x1].reshape(-1, 3)
        colors[ch] = _modal_color(text_patch)

    return colors, quality


# --- Curve digitization ---------------------------------------------------


def _alpha_blend_match(
    plot_rgb: np.ndarray,
    color: Tuple[int, int, int],
    min_alpha: float = CURVE_MIN_ALPHA,
    residual_threshold: float = CURVE_RESIDUAL_THRESHOLD,
) -> np.ndarray:
    """Boolean (h, w) match mask that accepts partially-covered
    anti-aliased curve pixels, not just solid-color ones.

    A thin (often ~1px at low signal amplitude) anti-aliased line drawn
    over a white background renders most of its pixels as a partial blend
    of (line color) and (white), not the solid line color -- e.g. a pixel
    only 60% covered by the stroke renders at 60% color + 40% white. A
    flat Manhattan-distance-to-solid-color threshold (the original
    approach here) rejects most of these, since the blend can be very far
    (in absolute RGB terms) from the solid color even though it's
    unambiguously THAT line and no other. This was found empirically: on
    a real quiet/no-one-wearing-the-headset capture (thin, mostly-partial
    strokes throughout), a solid-color threshold alone missed a large
    fraction of a channel's genuine curve pixels -- worst for AF3
    specifically, whose gray is mid-brightness so partial coverage drifts
    furthest (in absolute terms) from its solid value.

    Instead: project each pixel onto the line segment from white (255,
    255, 255) to the target color in RGB space, giving an implied
    coverage fraction ("alpha"). Accept the pixel if that fraction is at
    least `min_alpha` (rejects near-white background/other far colors)
    AND the pixel's perpendicular residual off that line is small (rejects
    a differently-hued blend that happens to reach a similar brightness --
    hue, not brightness, is what discriminates between channels here).
    """
    h, w, _ = plot_rgb.shape
    white = np.array([255, 255, 255], dtype=np.float64)
    target = np.array(color, dtype=np.float64)
    direction = target - white  # points from white toward the target color
    denom = float(np.dot(direction, direction))
    if denom == 0:
        return np.zeros((h, w), dtype=bool)

    delta = plot_rgb.astype(np.float64) - white  # (h, w, 3)
    alpha = (delta @ direction) / denom  # (h, w)
    alpha_clamped = np.clip(alpha, 0.0, 1.0)
    projected = white + alpha_clamped[..., None] * direction  # (h, w, 3)
    residual = np.abs(plot_rgb.astype(np.float64) - projected).sum(axis=2)

    return (alpha >= min_alpha) & (residual <= residual_threshold)


def classify_plot_pixels(
    plot_rgb: np.ndarray, channel_colors: Dict[str, Optional[Tuple[int, int, int]]]
) -> Dict[str, np.ndarray]:
    """Per-pixel WINNER-TAKE-ALL channel assignment across all channels at
    once, rather than testing each channel's color threshold in isolation.

    Returns one (h, w) boolean match mask per channel: a pixel counts as
    channel X's only if X's alpha-blend residual (see _alpha_blend_match)
    both clears the threshold AND is the single lowest residual among all
    channels at that pixel.

    This closes a real cross-channel bleed found live: P8 and FC6 are
    both cyan-family colors. With P8's curve genuinely covered by another
    window, independent-per-channel matching (the original approach) let
    P8's search window -- intentionally wide, to tolerate real amplitude
    excursions -- reach into FC6's territory and lock onto FC6's actual
    curve there, since FC6's blended color happened to also clear P8's
    threshold in isolation. It produced a stable, plausible-looking but
    entirely fabricated ~-140uV reading for P8 for the whole covered
    span, instead of a clean gap. Requiring a pixel to be the single best
    match among ALL channels (not just an acceptable match for one)
    means a pixel that's genuinely FC6's curve can never be claimed by
    P8, however wide P8's own search window is."""
    h, w, _ = plot_rgb.shape
    residuals: Dict[str, np.ndarray] = {}
    alphas: Dict[str, np.ndarray] = {}
    white = np.array([255, 255, 255], dtype=np.float64)
    rgb_f = plot_rgb.astype(np.float64)
    for ch, color in channel_colors.items():
        if color is None:
            continue
        target = np.array(color, dtype=np.float64)
        direction = target - white
        denom = float(np.dot(direction, direction))
        if denom == 0:
            continue
        delta = rgb_f - white
        alpha = (delta @ direction) / denom
        alpha_clamped = np.clip(alpha, 0.0, 1.0)
        projected = white + alpha_clamped[..., None] * direction
        residuals[ch] = np.abs(rgb_f - projected).sum(axis=2)
        alphas[ch] = alpha

    result: Dict[str, np.ndarray] = {ch: np.zeros((h, w), dtype=bool) for ch in channel_colors}
    if not residuals:
        return result

    valid_channels = list(residuals.keys())
    stacked = np.stack([residuals[ch] for ch in valid_channels], axis=0)  # (C, h, w)
    best_idx = np.argmin(stacked, axis=0)  # (h, w)
    for i, ch in enumerate(valid_channels):
        is_best = best_idx == i
        within_threshold = (alphas[ch] >= CURVE_MIN_ALPHA) & (residuals[ch] <= CURVE_RESIDUAL_THRESHOLD)
        result[ch] = is_best & within_threshold
    return result


def digitize_curve(
    match: np.ndarray,
    baseline_y_local: int,
    max_excursion_px: float,
) -> List[Optional[int]]:
    """Vectorized per-column digitization of one channel's curve from its
    precomputed (h, w) boolean match mask (see classify_plot_pixels).
    Returns one matched row-index (or None if that column has no
    confident match -- e.g. obscured by another opaque curve, or won by
    a competing channel) per column. NEVER interpolates a missing column.

    Candidate matches are restricted to a window around this channel's
    FIXED baseline row (+/- max_excursion_px), and when a column has
    multiple disjoint runs in its mask, the one closest to the fixed
    baseline wins -- not the one closest to the previous column's match.
    An earlier version tie-broke toward the running previous-column
    prediction (matching the original design intent of tolerating fast
    legitimate transients); empirically, against a real busy 14-channel
    capture, that let a channel lock onto a spurious weak match far from
    its true position and then stay locked there for an entire capture,
    since each wrong match reinforced the next column's prediction. Anchor
    to the fixed baseline instead: it can't drift, at the cost of being
    slightly less tolerant of a large excursion that persists for many
    consecutive columns right at the edge of another channel's territory.
    """
    h, w = match.shape
    if h == 0 or w == 0:
        return []

    row_lo = max(0, int(baseline_y_local - max_excursion_px))
    row_hi = min(h, int(baseline_y_local + max_excursion_px) + 1)
    row_idx_full = np.arange(h)

    results: List[Optional[int]] = [None] * w
    for x in range(w):
        col_match = match[row_lo:row_hi, x]
        if not col_match.any():
            results[x] = None
            continue
        matched_rows = row_idx_full[row_lo:row_hi][col_match]
        # Split into contiguous runs.
        splits = np.where(np.diff(matched_rows) > 1)[0]
        run_groups = np.split(matched_rows, splits + 1)
        if len(run_groups) == 1:
            y = float(run_groups[0].mean())
        else:
            # Ambiguous: multiple disjoint runs of this color in the same
            # column (rare -- most likely an anti-aliased crossing).
            # Tie-break toward the run closest to the fixed baseline.
            centroids = [float(g.mean()) for g in run_groups]
            y = min(centroids, key=lambda c: abs(c - baseline_y_local))
        results[x] = y
    return results


@dataclass
class CaptureResult:
    capture_ts_ns: int
    samples: List[Tuple[int, Dict[str, Optional[float]]]] = field(default_factory=list)
    quality: Dict[str, Optional[str]] = field(default_factory=dict)
    window_width: int = 0


def pixel_to_seconds_ago(
    x: int, bounds: PlotBounds, tick_calibration: Optional[List[Tuple[int, int]]]
) -> float:
    """Map a plot-absolute pixel x-coordinate to seconds-before-capture,
    using OCR'd tick points if available (piecewise-linear across them),
    else a fixed fallback assuming the rightmost plot column is "now" and
    the whole width spans FALLBACK_WINDOW_SECONDS."""
    if tick_calibration and len(tick_calibration) >= 2:
        xs = [p[0] for p in tick_calibration]
        secs = [p[1] for p in tick_calibration]
        # seconds-ago = (rightmost tick's time-value) - (this column's
        # interpolated time-value), extrapolated linearly at the ends.
        max_sec = secs[-1]
        if x <= xs[0]:
            x0, x1 = xs[0], xs[1]
            s0, s1 = secs[0], secs[1]
        elif x >= xs[-1]:
            x0, x1 = xs[-2], xs[-1]
            s0, s1 = secs[-2], secs[-1]
        else:
            for (xa, sa), (xb, sb) in zip(tick_calibration, tick_calibration[1:]):
                if xa <= x <= xb:
                    x0, x1, s0, s1 = xa, xb, sa, sb
                    break
            else:
                x0, x1, s0, s1 = xs[0], xs[1], secs[0], secs[1]
        frac = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
        this_sec = s0 + frac * (s1 - s0)
        return max(0.0, max_sec - this_sec)

    # Fallback: assume fixed window width in seconds, right edge = now.
    span = max(1, bounds.x1 - bounds.x0)
    frac = (bounds.x1 - x) / span
    return max(0.0, frac * FALLBACK_WINDOW_SECONDS)


def digitize_capture(
    frame: Frame,
    last_spacing_uv: Optional[float],
    verbose: bool = False,
    channels: List[str] = CHANNELS,
) -> Tuple[Optional[CaptureResult], Optional[float]]:
    """Full per-capture pipeline for the live ALL-channels EEG view.
    Returns (result, spacing_uv_used) -- spacing_uv_used lets the caller
    keep a last-known-good value across ticks when OCR fails.

    ``channels`` defaults to the 14 EEG channels; passing a smaller list
    only makes sense if the on-screen legend actually shows exactly those
    channels (epoc-record always shows all 14 live, so it never needs to
    override this)."""
    spacing_uv = read_spacing_uv(frame, verbose=verbose)
    if spacing_uv is None:
        spacing_uv = last_spacing_uv
        if verbose and spacing_uv is not None:
            sys.stderr.write(f"[spacing] OCR failed this tick, reusing last-known {spacing_uv}\n")
    if spacing_uv is None:
        sys.stderr.write("[warn] channel spacing unreadable and no prior value known; skipping capture\n")
        return None, None

    bounds = find_plot_bounds(frame, verbose=verbose)
    if bounds is None:
        sys.stderr.write("[warn] could not locate plot area this capture; skipping\n")
        return None, spacing_uv

    baselines = find_channel_baselines(bounds, channels)
    pixels_per_uv = calibrate_uv_per_pixel(baselines, spacing_uv)
    if not pixels_per_uv:
        sys.stderr.write("[warn] could not calibrate uV/pixel scale this capture; skipping\n")
        return None, spacing_uv

    colors, quality = calibrate_channel_colors(frame, baselines)
    tick_calibration = read_axis_tick_labels(frame, bounds, verbose=verbose)

    ys_sorted = sorted(baselines.values())
    row_gaps = [b - a for a, b in zip(ys_sorted, ys_sorted[1:])]
    median_row_gap = sorted(row_gaps)[len(row_gaps) // 2] if row_gaps else 40
    # How far (in pixels) a channel's curve may wander from its own fixed
    # baseline and still be considered that channel's, not a neighbor's --
    # generous enough for legitimate large-amplitude excursions into
    # neighboring rows, tight enough to reject spurious distant matches
    # (see digitize_curve's docstring for why this replaced pure
    # previous-column tracking).
    max_excursion_px = 2.5 * median_row_gap

    plot_rgb = frame.rgb[bounds.y0 : bounds.y1, bounds.x0 : bounds.x1].copy()
    blank_channel_overlay(plot_rgb, frame, bounds)
    match_masks = classify_plot_pixels(plot_rgb, colors)

    per_channel_series: Dict[str, List[Optional[float]]] = {}
    for ch in channels:
        color = colors.get(ch)
        baseline_local = baselines[ch] - bounds.y0
        if color is None:
            per_channel_series[ch] = [None] * plot_rgb.shape[1]
            continue
        rows = digitize_curve(match_masks[ch], baseline_local, max_excursion_px)
        values: List[Optional[float]] = []
        for y in rows:
            if y is None:
                values.append(None)
            else:
                values.append((baselines[ch] - (y + bounds.y0)) / pixels_per_uv)
        per_channel_series[ch] = values

    n_cols = plot_rgb.shape[1]
    samples: List[Tuple[int, Dict[str, Optional[float]]]] = []
    for col in range(n_cols):
        x_abs = bounds.x0 + col
        seconds_ago = pixel_to_seconds_ago(x_abs, bounds, tick_calibration)
        ts_ns = frame.capture_ts_ns - int(seconds_ago * 1e9)
        row_values = {ch: per_channel_series[ch][col] for ch in channels}
        samples.append((ts_ns, row_values))
    samples.sort(key=lambda s: s[0])

    result = CaptureResult(
        capture_ts_ns=frame.capture_ts_ns,
        samples=samples,
        quality=quality,
        window_width=frame.width,
    )
    return result, spacing_uv


# --- Stitching --------------------------------------------------------


class Stitcher:
    """Merges successive CaptureResults into one continuous,
    non-decreasing timestamp stream. Timestamp-based deduplication IS the
    splice mechanism (robust, simple); overlap-region value comparison is
    a diagnostic-only consistency check (stderr warning), never a
    blocking gate."""

    def __init__(self, verbose: bool = False, channels: List[str] = CHANNELS):
        self.last_emitted_ts_ns: Optional[int] = None
        self.prev_result: Optional[CaptureResult] = None
        self.verbose = verbose
        self.channels = channels

    def process(self, result: CaptureResult, writer) -> None:
        if self.last_emitted_ts_ns is None:
            self._check_overlap(result)
            new_samples = result.samples
        else:
            self._check_overlap(result)
            new_samples = [s for s in result.samples if s[0] > self.last_emitted_ts_ns]

        if not new_samples:
            sys.stderr.write(
                "[warn] no new samples this capture (delay too long relative to the "
                "plot's visible window, or capture stalled) -- possible gap in output\n"
            )
        else:
            # Attach this capture's single quality snapshot to the sample
            # nearest this capture's own timestamp among the newly emitted
            # tail; earlier newly-emitted rows get no quality value (relies
            # on live-viewer's hold-last-value semantics for the gaps).
            nearest_idx = min(
                range(len(new_samples)),
                key=lambda i: abs(new_samples[i][0] - result.capture_ts_ns),
            )
            for i, (ts_ns, values) in enumerate(new_samples):
                q = result.quality if i == nearest_idx else {}
                writer.write_row(ts_ns, values, q)
            self.last_emitted_ts_ns = new_samples[-1][0]

        self.prev_result = result

    def _check_overlap(self, result: CaptureResult) -> None:
        """Diagnostic-only: warn (never block) if consecutive captures
        don't share a real time range, or if they disagree substantially
        within it. Matches samples by NEAREST timestamp within a small
        tolerance rather than requiring exact equality -- each capture's
        absolute sample timestamps are independently computed from that
        capture's own OCR'd tick calibration (or the fixed fallback), so
        two captures' per-column timestamp grids are never expected to
        land on bit-identical nanosecond values even when they genuinely
        overlap in real time."""
        prev = self.prev_result
        if prev is None or not prev.samples or not result.samples:
            return
        if prev.window_width != result.window_width:
            if self.verbose:
                sys.stderr.write("[stitch] window resized since last capture; skipping overlap consistency check\n")
            return

        prev_ts = np.array([s[0] for s in prev.samples])
        result_ts = np.array([s[0] for s in result.samples])
        if result_ts[0] > prev_ts[-1] or result_ts[-1] < prev_ts[0]:
            sys.stderr.write(
                "[warn] no overlapping time range between consecutive captures -- "
                "--delay may be too large relative to the plot's visible window\n"
            )
            return

        # Sample tolerance: half the average inter-column time step.
        step_ns = (result_ts[-1] - result_ts[0]) / max(1, len(result_ts) - 1)
        tol_ns = max(1, step_ns / 2)

        diffs = []
        idx = np.searchsorted(prev_ts, result_ts)
        for i, ts in enumerate(result_ts):
            j = idx[i]
            candidates = [j - 1, j]
            best_j = min(
                (c for c in candidates if 0 <= c < len(prev_ts)),
                key=lambda c: abs(prev_ts[c] - ts),
                default=None,
            )
            if best_j is None or abs(prev_ts[best_j] - ts) > tol_ns:
                continue
            values = result.samples[i][1]
            prev_values = prev.samples[best_j][1]
            for ch in self.channels:
                a, b = values.get(ch), prev_values.get(ch)
                if a is not None and b is not None:
                    diffs.append(abs(a - b))

        if diffs:
            mean_diff = sum(diffs) / len(diffs)
            if mean_diff > 5.0 and self.verbose:
                sys.stderr.write(
                    f"[stitch] overlap consistency check: mean abs diff {mean_diff:.2f} uV "
                    f"across {len(diffs)} overlapping samples (large values may indicate "
                    "mis-detected plot bounds or clock drift)\n"
                )
        elif self.verbose:
            sys.stderr.write(
                "[stitch] overlap consistency check: time ranges overlapped but no "
                "directly comparable non-N/A sample pairs found within tolerance\n"
            )


# --- CSV output ------------------------------------------------------------


def _iso_ns(ts_ns: int) -> str:
    """Format a ns wall-clock timestamp as ISO-8601 UTC with nanoseconds
    (datetime's own isoformat() only supports microsecond precision)."""
    import datetime as _dt

    secs, ns = divmod(ts_ns, 1_000_000_000)
    dt = _dt.datetime.fromtimestamp(secs, tz=_dt.timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{ns:09d}Z"


CSV_HEADER = (
    ["timestamp_ns", "timestamp_iso"]
    + CHANNELS
    + [f"{ch}_q" for ch in CHANNELS]
)


class CSVWriter:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.out.write(",".join(CSV_HEADER) + "\n")
        self.out.flush()

    def write_row(
        self,
        ts_ns: int,
        values: Dict[str, Optional[float]],
        quality: Dict[str, Optional[str]],
    ) -> None:
        row = [str(ts_ns), _iso_ns(ts_ns)]
        for ch in CHANNELS:
            v = values.get(ch)
            row.append("" if v is None else f"{v:.2f}")
        for ch in CHANNELS:
            tier = quality.get(ch)
            code = QUALITY_TIER_CODE.get(tier) if tier else None
            row.append("" if code is None else str(code))
        self.out.write(",".join(row) + "\n")

    def flush(self) -> None:
        self.out.flush()
