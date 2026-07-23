"""
ENKI Command Center — Dev profile entrypoint (thin wrapper).

Docker configs reference `services.api.src.main_dev:app`. All logic lives
in main.py. We set ENKI_PROFILE=dev as the default if not already set —
main.py's profile resolver will fail-closed if anything production-pointing
leaks into the dev environment.
"""

import os

os.environ.setdefault("ENKI_PROFILE", "dev")

from services.api.src.main import app  # noqa: F401, E402
