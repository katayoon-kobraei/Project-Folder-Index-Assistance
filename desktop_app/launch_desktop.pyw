"""
Double-click entry point for the installed app — this is what the Desktop
and Start Menu shortcuts actually launch, via pythonw.exe (no console
window). Not used for day-to-day development; `python -m desktop_app.main`
still works fine for that (see run_desktop.ps1).

Mirrors the sibling INGEVIA Email Assistant app's own launch_desktop.pyw:
resolve APP_ROOT from this file's own location (not the current working
directory, which a shortcut's "Start in" folder already sets correctly
anyway, but this makes it work even if that's ever missing/wrong), chdir
into it, put it on sys.path, then hand off to the real entry point.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
os.chdir(APP_ROOT)
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from desktop_app.main import main

raise SystemExit(main())
