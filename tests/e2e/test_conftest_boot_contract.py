"""Importing the e2e conftest must not boot the gateway's dependency selection.

pytest imports conftest modules before it builds the session-start header. ``gateway.run`` starts
by importing ``hermes_bootstrap``, which selects PM's committed dependency generation and rewrites
``sys.path`` onto it, so a conftest-level gateway import leaves the process without the metadata of
everything only the test environment has — ``pluggy``'s among them — and any non-quiet run dies
building the header::

    msg += f", pytest-{_pytest._version.version}, pluggy-{pluggy.__version__}"
    importlib.metadata.PackageNotFoundError: No package metadata was found for pluggy
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import sys

import pm.environments

calls = []
pm.environments.activate_dependencies = lambda root: calls.append(str(root))
sys.path.insert(0, %(root)r)

import tests.e2e.conftest  # noqa: F401

print("activate_dependencies_calls:", calls)
print("gateway_run_imported:", "gateway.run" in sys.modules)
"""


def test_importing_the_e2e_conftest_does_not_select_pm_dependencies():
    probe = _PROBE % {"root": str(REPO_ROOT)}
    child = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT,
                           capture_output=True, text=True, timeout=180, check=False)

    assert child.returncode == 0, child.stderr
    assert "activate_dependencies_calls: []" in child.stdout, child.stdout + child.stderr
    assert "gateway_run_imported: False" in child.stdout, child.stdout + child.stderr
