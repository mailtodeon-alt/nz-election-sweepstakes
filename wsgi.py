"""Gunicorn entrypoint: gunicorn -k gthread --threads 8 -w 1 --timeout 0 wsgi:app"""
from __future__ import annotations
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
# Ensure src helpers (db, scoring, feed) import as top-level modules
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Load src/app.py under a unique module name to avoid clashing with this package's app.py
_spec = spec_from_file_location("sweepstakes_flask", SRC / "app.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"Cannot load Flask app from {SRC / 'app.py'}")
_mod = module_from_spec(_spec)
# Register before exec so intra-package patterns work if any
sys.modules["sweepstakes_flask"] = _mod
_spec.loader.exec_module(_mod)
app = _mod.app

__all__ = ["app"]
