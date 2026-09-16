"""Root conftest: its presence puts the repo root on ``sys.path`` during
collection, so cross-file test imports like ``from tests.test_agent_loop
import FakeLocator`` resolve under bare ``pytest`` / ``uv run pytest``
(no ``python -m pytest`` cwd insertion required)."""
