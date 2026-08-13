"""Import this FIRST in any script under utils/ — before `from src.config import settings`.

    from _bootstrap import REPO_ROOT          # noqa: F401  (side effect: loads .env)
    from src.config import settings

WHY THIS EXISTS
---------------
`src/config/settings.py` deliberately does NOT load .env. It reads os.environ only,
because under systemd the environment arrives via EnvironmentFile=, and systemd does
not strip inline `# comments` the way python-dotenv does — a value like
`POLL_INTERVAL_SECS=1   # fast` crashes int() under systemd but parses fine under
dotenv. Keeping settings.py env-only avoids that divergence.

The consequence is that anything run from a shell must load .env itself. Forgetting
it does not fail loudly: settings falls back to built-in DEFAULTS and an empty
INFLUX_TOKEN, so the script runs and every query returns 401 — or worse, succeeds
against the wrong configuration and reports numbers for a system that is not the one
in production. That has happened twice:

  * diag_cycle.py (2026-08-08) silently used 50 instruments / ltp,vtt,oi / 180m
    lookback instead of the live 99 / ltp,vtt,ltq / 60m, and 401'd on every query.
  * trade_conditions.py (2026-08-13) 401'd on the first Influx call.

sys.path is also set here so `from src.config import ...` resolves regardless of the
directory the script is invoked from.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Explicit path, not load_dotenv()'s CWD-relative search: these scripts get run from
# the repo root, from utils/, and from cron, and the search would find a different
# file (or none) depending on which.
load_dotenv(REPO_ROOT / '.env')
