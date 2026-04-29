"""Pytest path: load `backend.*` imports (see tests/test_room_registry_and_config.py)."""

import os
import sys

_HAWAAI = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _HAWAAI not in sys.path:
    sys.path.insert(0, _HAWAAI)
