"""The harness readers import each other as top-level modules, the way
`uv run tools/backfill.py` sees them, so the tests put `tools/` on the path.

    uv run --with pytest pytest tools/tests
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
