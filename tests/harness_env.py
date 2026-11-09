"""Child environments for tests that spawn a real Hermes launch.

``tests/conftest.py`` marks every process it starts with
``pm.environments.OWN_PACKAGES_MARKER`` so PM's boot leaves a test run on the test
environment's own packages (no re-exec into the store Python, no ``sys.path`` rewrite).
A child that models the CLI a user launches must not carry that mark, or PM never takes
it over and the launch contracts under test never run.
"""

from __future__ import annotations

import os


def launch_env(**overrides: str) -> dict[str, str]:
    """The current environment without the suite's mark, plus *overrides*."""
    from pm.environments import OWN_PACKAGES_MARKER

    return {**{k: v for k, v in os.environ.items() if k != OWN_PACKAGES_MARKER}, **overrides}
