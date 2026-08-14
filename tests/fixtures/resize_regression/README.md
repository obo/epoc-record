# Resize regression fixtures

55 real EmotivPRO captures collected live across two sessions, spanning
window sizes from 1012x799 up to 1878x1177 (both pure width-only and pure
height-only stretches, many mixed aspect ratios, and several near-full-screen
sizes), used to verify and guard against the non-uniform-resize bug fixed in
commit `38c75fd` (fraction-of-window-size UI coordinates silently breaking
whenever width and height scaled by different factors) and the digit-OCR
near-tie bug ("2" misread as "7") found and fixed afterward.

- `v*.png` — the first verification pass (27 captures, programmatic resize
  via `xdotool windowsize` between shots).
- `f*.png` — the second pass (28 captures, taken once per second for 30s
  while the window was being interactively moved/resized by hand, to catch
  anything a clean programmatic resize wouldn't).
- `*.csv` — the corresponding "golden" `digitize_capture()` output for each
  screenshot (same base filename), each independently verified against the
  actual pixel content (not just trusted from a prior code run — see the
  project history for why that distinction matters: an earlier version of
  this fixture set had baked in a real OCR bug's wrong output as if it were
  ground truth). First line is a `# spacing_uv=...` comment recording the
  expected OCR'd "Channel spacing (uV)" reading; the rest is the normal
  screenrecord.py CSV format.

  `f17_1012x799.csv` is intentionally near-empty: that capture landed on a
  genuine EmotivPRO rendering transient (legend text hadn't redrawn yet
  mid-drag), verified by eye to be legitimately unreadable at that exact
  instant — the golden file records "correctly reports unreadable," not
  "was skipped by accident."

Exercised by `tests/test_resize_regression.py`, which re-runs
`digitize_capture()` on every screenshot here and compares against its
golden CSV. Run just this suite with:

```bash
./venv/bin/python -m pytest tests/test_resize_regression.py -q
```

## Regenerating golden CSVs

Only do this after independently re-confirming the new expected values by
eye against the screenshots (crop the "Channel spacing (uV)" box and each
channel region and look at them) — do not regenerate goldens from a code
run you haven't separately verified, or a real regression will silently
become the new "expected" baseline.
