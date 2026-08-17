# epoc-record

Digitizes **EmotivPRO**'s live scrolling multi-channel EEG chart (via X11
screen capture) and streams the result as CSV on stdout, in a format the
sibling [`mudrarecord`](https://github.com/obo/mudrarecord)'s
[`live-viewer`](https://github.com/obo/mudrarecord/live-viewer) can plot live.

The screen-reading machinery (window capture, OCR, plot geometry, curve
digitization, stitching) lives in the shared module `epoc_graph.py`. The
companion script [`epoc-reread`](#epoc-reread) reuses it to *actively*
re-read a stored recording at higher fidelity -- see below.

## Why

EmotivPRO doesn't expose per-electrode EEG. The live
scrolling chart in EmotivPRO's own GUI is the only place this data exists
at all.

This script reads it visually: it finds the `EmotivPRO ...`
window, screenshots it every `--delay` seconds, and for each of the 14
electrodes finds that channel's uniquely-colored curve and reads its
vertical (µV) position column by column, plus the color-coded
contact/signal quality dot next to each channel's name in the legend.

The script is passive: only screenshots + window-geometry queries, never
synthetic input — safe to run alongside a real, currently-recording
EmotivPRO session.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Prerequisites: EmotivPRO running under Wine

This script needs the Windows **EmotivPRO** GUI already open on screen — it
only ever reads its window via X11 screenshot, it never launches or drives
it itself.

**If you don't have it installed yet**, run the 
[`prerequisities-installation`](prerequisities-installation) bundle
first (`./install.sh` from that directory — see its own README for
details/flags). It's a one-time, ~1.5 GB, `sudo`-requiring setup with one
**interactive** step (a GUI installer wizard you click through) that
installs:

1. The **native Linux Cortex service** (`cortex`/`cortexsync` systemd
   units) — this is what actually talks BLE to the headset.
2. **Wine** (`winehq-stable`, via the official WineHQ apt repo — Ubuntu's
   bundled Wine is too old for this Qt6 installer).
3. The Windows **EmotivPRO.exe** itself, via Wine, into a `WINEPREFIX`.

Check what's already present with:

```bash
dpkg -l emotivapps                          # native Cortex + Launcher package
systemctl is-active cortex cortexsync       # must both be "active"
wine --version                              # must be present (winehq-stable)
```

To locate the installed Windows app files (`EmotivPRO.exe`), check
`$WINEPREFIX/drive_c/.../EmotivApps*` first; per
`prerequisities-installation`'s documented "known quirk", the Windows
installer sometimes instead drops a full `EmotivApps*` directory as a
*sibling* of wherever `install.sh` was run from (e.g.
`./EmotivApps`), outside the `WINEPREFIX` entirely — check there too
before falling back to a slower full search:
`find / -iname 'EmotivPRO.exe' 2>/dev/null`.

### Launching it

Wine has no working BLE stack, so the Wine-installed GUI never talks to
the headset directly — it's just a local WebSocket client of Cortex on
`tcp/6868`, and doesn't care whether the backend serving that port is the
native Linux Cortex or a Windows one. So: make sure native Cortex is up,
then run the Windows `.exe` under Wine pointed at *any* `WINEPREFIX` —
confirmed by testing that a throwaway prefix created with nothing but
`wineboot --init` (no reinstall needed) is enough, since the installed app
directory ships every DLL it needs alongside the `.exe` and barely touches
the Wine registry:

```bash
systemctl is-active cortex cortexsync   # confirm native Cortex is up first

export WINEPREFIX=~/.wine-emotiv        # wherever install.sh set one up; a fresh/throwaway one also works
export WINEARCH=win64
wineboot --init                         # harmless no-op if the prefix already exists

cd "$(dirname "$(find / -iname 'EmotivPRO.exe' 2>/dev/null | head -1)")"
wine "EmotivPRO.exe" &
```

Notes from testing this end-to-end without a physical headset attached:

- EmotivPRO comes up and connects to native Cortex correctly (confirmed: it
  shows your logged-in account's recordings and license tier) even with no
  headset powered on nearby — Cortex being reachable is all that's required
  for the GUI to render. Reading real electrode data still needs the
  physical headset paired and in range.
- **EmotivPRO shows two one-off dialogs** ("Welcome to EMOTIV PRO-Lite",
  then a data-policy notice) the first time it runs in a given
  `WINEPREFIX`; click through them once (`OK, CONTINUE` / `Ok, I
  understand`) to reach the Recordings list. They don't reappear on later
  launches of the same prefix, but until dismissed the actual UI
  `epoc-record` needs to read is not the frontmost content, so captures
  will come back unreadable.
- `epoc-record` needs EmotivPRO's live chart view specifically (see Usage
  below) — the Recordings list or a dialog on top of it will correctly
  read back as "unreadable" rather than garbage.

## Usage

Open EmotivPRO to the live EEG chart view (all 14 electrodes visible,
scrolling), then:

```bash
./venv/bin/python epoc-record | mudrarecord/live-viewer/live-viewer \
    --width=10s --group='A*' --group='F*' --group='O*' --group='P*' --group='T*' \
    --group='*_q'
```

CSV columns: `timestamp_ns,timestamp_iso`, then one µV column per channel
(`AF3,F7,F3,FC5,T7,P7,O1,O2,P8,T8,FC6,F4,F8,AF4`), then one quality column
per channel (`AF3_q,...,AF4_q`, coded `0`=off, `1`=poor, `2`=fair, `3`=good,
blank when stale/unreadable). A blank/`nan` EEG value means that channel's
curve was obscured by another overlapping curve at that instant (or
otherwise unreadable) — **never interpolated/guessed**, per `live-viewer`'s
own "no sample, hold last value" convention.

## Options

| flag | default | meaning |
|---|---|---|
| `--delay` | `7.0` | seconds between screenshots (max `9.0`) |
| `--verbose` / `-v` | off | extra OCR/calibration/stitching diagnostics on stderr |
| `--once` | off | single capture, emit, exit (useful for manual testing) |

`--delay` is capped at `9.0` because the chart's rolling visible window is
~10 seconds; a longer delay couldn't guarantee a ≥1s of overlap between
consecutive screenshots that the stitching logic uses to splice them into
one continuous stream without gaps or duplicates. Lower delay = lower
latency (and higher CPU, since each screenshot involves real image
processing); higher delay = lower CPU.

## Limitations

- **Resolution ceiling**: one digitized sample per plot pixel-column —
  roughly 60-100 samples/second depending on window width, well below the
  headset's real ~256Hz. This is a fundamental limit of reading values off
  a rendered chart, not a bug.
- **The EmotivPRO window must be on screen and not covered by another
  window** for the covered region to be readable at all — screenshots
  can't see through opaque windows. (This script captures via the root/
  screen window rather than the app's own window specifically so that
  *uncovered* regions stay readable even while other parts are covered —
  a non-compositing window manager makes a direct per-window capture return
  stale/black pixels for any occluded region.)
- **Quality columns are point-in-time snapshots**, not continuous traces —
  there's no historical quality trace on screen to recover, only whatever
  the dot currently shows.
- Digit-template OCR (for the "Channel spacing (uV)" field and the x-axis
  tick labels) is a small self-contained matcher calibrated against one
  EmotivPRO version's font rendering (see the `DIGIT_TEMPLATES` constant
  and comments in `epoc-record`); a future EmotivPRO UI/font change may
  need it recalibrated the same way the electrode coordinates do.
  Occasionally two digits are a genuine near-tie for this template set
  (confirmed live: "2" vs "7") — the matcher requires a minimum margin
  over the runner-up, not just a minimum score, and fails closed (reports
  the field unreadable) rather than guess when a read is too close to
  call. `DIGIT_TEMPLATE_VARIANTS` lets a digit have more than one
  confirmed-real reference shape (used for the "2"/"7" case) if a future
  ambiguity needs the same fix.

### Window resizing

The EmotivPRO window can be freely moved and resized (including live,
mid-recording) and `epoc-record` tracks it correctly. Note that EmotivPRO keeps
its left
control-panel sidebar, right-hand legend column, and header/tick-label
chrome at a fixed pixel size regardless of window size — only the plot
canvas between them grows or shrinks. All of `epoc-record`'s UI
coordinates (`PLOT_X0_PX`, `SPACING_BOX_*_PX`, `LEGEND_*_FROM_RIGHT_PX`,
etc.) are therefore fixed pixel offsets from a specific window edge, not
fractions of window width/height.

One related, non-bug edge case: a screenshot taken at the exact instant
the window is actively being dragged to a new size can catch EmotivPRO's
own UI mid-redraw (e.g. the legend text briefly blanked out while the
canvas hasn't yet settled into its new layout) — `epoc-record` correctly
reports the affected channels as unreadable for that one tick rather than
fabricate anything, and recovers on the next capture once the resize
settles. See `f17_1012x799.png`/`.csv` in the fixtures below for a
captured example.

## epoc-reread

`epoc-reread` actively drives EmotivPRO's own replay UI (simulated
`xdotool` clicks -- unlike `epoc-record`, this is NOT passive) to
re-digitize a *stored* recording one electrode (or, with `--motion-too`,
one motion sensor) at a time, at much higher fidelity than the live
14-at-once view allows.

```bash
./venv/bin/python epoc-reread log.20260814-1659.gz --dry-run   # plan only, no clicking
./venv/bin/python epoc-reread log.20260814-1659.gz --recording-index 1 --channels AF3,F7
```

Given an `epoc-record` log, it finds the EmotivPRO window, tries to
identify the matching row in the Recordings list by OCR'ing each row's
Date Collected/Duration and overlapping that against the log's own
(approximate) time span, opens it, and for each electrode reads two
passes by default (`--zooms`): a **fine** range (tight, from the log's
own observed values, for maximum resolution) and a **wide** range (a
generous multiple of the log's observed extreme, since a value that was
off-screen in the original capture shows up as *missing*, not wrong --
EmotivPRO's own "Autoscale" button turned out, confirmed live, to just
reset to its fixed default rather than truly autofit, so it isn't used).
Each (zoom level x high-pass-filter-on/off) combination is its own pass,
written incrementally to `LOGFILE.ELECTRODE.RANGE[.hp]`, plus a
`LOGFILE.match-report.txt` with a cross-correlation-based "how well does
this line up with the original log" check per pass.

**The automatic Recordings-list match is best-effort, not certain**: that
table renders in a font the digit-template OCR doesn't read as reliably
as the contexts `epoc-record` itself relies on (confirmed live). It fails
closed most of the time, but given how long a real run takes, confirm the
matched row against what's on screen yourself, or skip the guesswork
entirely with `--recording-index N` (1-based, counting from the top of
the currently-displayed list -- no OCR involved at all).

**Motion sensors** (`--motion-too`) have no Amplitude/High-pass controls
in EmotivPRO at all (confirmed live) -- one autoscaled pass per sensor,
written as `LOGFILE.SENSOR` with a `value_frac_of_visible_range` column
(0..1 within whatever range EmotivPRO auto-picked that capture) rather
than a fabricated absolute unit, since that axis's own tick labels
render too small for reliable OCR.

**Runtime is long**: replay runs at ~1x realtime (confirmed live), so
N electrodes x Z zoom levels x 2 filter states x a D-long recording takes
roughly N*Z*2*D just for EEG. Use `--channels`/`--zooms`/`--dry-run` to
scope a test run before committing to a full one.

**Must run attended, in the foreground -- never in the background
(`&`/`nohup`/etc).** It shares the mouse/keyboard/window with whatever
else you're doing, and before every click and periodically during a pass
it checks the EmotivPRO window is still the size/focus it left it at and
the pointer hasn't moved on its own; the moment that's no longer true, it
stops, prints what it saw, and waits on stdin for you to confirm before
resuming (from the start of the current pass, not mid-action). Give it
`--duration SECONDS` whenever you know the recording's real length,
especially with `--recording-index` (which has no OCR'd duration of its
own) -- confirmed live that the play/pause-icon end-of-playback detection
alone can misfire and truncate a pass early; `--duration` is a hard floor
that overrides it.

At the end of a run, all of a run's per-electrode/per-sensor pass files
are combined into one `LOGFILE.reread.csv` (all 14 electrode columns,
plus the 10 motion columns if `--motion-too` was used): per electrode,
the finest-resolution pass that has a real sample at a given instant
wins, falling back to coarser passes and finally the original log to
fill in anything a pass didn't cover -- never interpolated, so a blank
cell still means no source had a real reading there.

## Testing against real captures

`tests/fixtures/resize_regression/` holds 55 real EmotivPRO screenshots
collected live across many window sizes, each with a 
"golden" CSV of the expected `digitize_capture()` output next to it (see
the README in that directory for how they were collected, and
the ground rules for regenerating them). `tests/test_resize_regression.py`
replays every one of them against the current code on each test run.

Run the whole suite with:

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest -q
```

Or just the resize-regression suite with:

```bash
./venv/bin/python -m pytest tests/test_resize_regression.py -q
```
