import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

# The module under test lives in the repo root as `epoc-record` (no .py
# extension, matching this project's other command-line tools), so it can't
# be found by a plain `import screenrecord`, and its extension-less filename
# also isn't auto-recognized as source by spec_from_file_location -- an
# explicit SourceFileLoader is needed. Load it explicitly from its file path
# and register it in sys.modules under the name every test module imports
# it as, so their own plain `import screenrecord as sr` resolves from the
# cache without needing to know the real on-disk filename.
_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "epoc-record")
_loader = SourceFileLoader("screenrecord", _SCRIPT_PATH)
_spec = importlib.util.spec_from_loader("screenrecord", _loader)
sr = importlib.util.module_from_spec(_spec)
sys.modules["screenrecord"] = sr
_loader.exec_module(sr)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(FIXTURES_DIR)


def load_frame(filename: str, capture_ts_ns: int = 1_700_000_000_000_000_000) -> "sr.Frame":
    img = Image.open(os.path.join(FIXTURES_DIR, filename)).convert("RGB")
    rgb = np.array(img)
    h, w = rgb.shape[:2]
    return sr.Frame(
        rgb=rgb,
        width=w,
        height=h,
        capture_ts_ns=capture_ts_ns,
    )


@pytest.fixture
def clean_frame():
    return load_frame("emotivpro_1024x850_clean.png")


@pytest.fixture
def resized_frame():
    # A REAL capture at a non-uniformly-resized window size (width and
    # height scaled by very different factors: 1.176x vs 1.380x), not a
    # synthetically-shrunk copy of the reference image -- a uniform
    # synthetic resize can't exercise the fixed-pixel-vs-proportional UI
    # geometry bug this fixture exists to catch (see PLOT_X0_PX etc. in
    # epoc-record).
    return load_frame("emotivpro_1024x1173_resized.png")


@pytest.fixture
def wide_frame():
    # A REAL capture at a width-only stretch (height unchanged from the
    # reference), the other half of the non-uniform-resize regression
    # coverage.
    return load_frame("emotivpro_1500x850_resized.png")


@pytest.fixture
def clean_bounds(clean_frame):
    return sr.find_plot_bounds(clean_frame)


@pytest.fixture
def clean_baselines(clean_bounds):
    return sr.find_channel_baselines(clean_bounds)
