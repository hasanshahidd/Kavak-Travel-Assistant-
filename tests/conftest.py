"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

# Make `app` importable when running pytest from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
