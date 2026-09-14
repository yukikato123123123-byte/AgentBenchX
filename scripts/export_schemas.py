"""Regenerate the portable Phase 1 contract after API changes."""

import json
from pathlib import Path

from agentbenchx.main import app

Path("packages/schemas/openapi.json").write_text(json.dumps(app.openapi(), indent=2) + "\n")
