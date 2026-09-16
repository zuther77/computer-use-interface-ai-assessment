"""Shared runtime settings.

All configuration comes from environment variables (see /config/.env.example)
so that no credentials or machine-specific values are ever hard-coded
(DECISIONS.md §8c: credentials are read from environment/config at session
start and never written to any log, artifact, or evidence file).
"""

from __future__ import annotations

import os

# ParaBank target surface (DECISIONS.md §1)
PARABANK_BASE_URL = os.environ.get(
    "PARABANK_BASE_URL", "http://localhost:8080/parabank"
)

# LLM provider settings (DECISIONS.md §2d) — GLM via the OpenAI SDK, so a
# provider swap is a base_url/api_key change, not a rewrite.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-5.2")

# Max discovery-loop steps before a typed max-steps halt (DECISIONS.md §3d).
MAX_STEPS = int(os.environ.get("BANKOPS_MAX_STEPS", "40"))
