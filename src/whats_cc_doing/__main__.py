"""`python -m whats_cc_doing` - same entry as the `ccdoing` script.

Exists so the serve daemon can respawn itself via the running interpreter
without guessing where (or whether) the console script is on PATH.
"""

import sys

from .cli import main

sys.exit(main())
