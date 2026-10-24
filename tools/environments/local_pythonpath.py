"""Hermes-owned PYTHONPATH stripping for child processes. Launchers prepend the repo
root and the Hermes venv's site-packages so the backend can ``import tools``; leaked
into a child Python of a DIFFERENT version they load the backend's C extensions and
crash. Only entries proven Hermes-owned by *path provenance* are removed — never by a
cross-version heuristic. Module state (``_hermes_repo_root_aliases``, ``_in_venv``,
``_hermes_site_packages``) lives in ``tools.environments.local`` (via :func:`_state`)
so tests monkeypatching it there keep working."""

import logging
import os
import platform
import sys
from pathlib import Path

from tools.environments.local_env_policy import _ACTIVE_VENV_MARKER_VARS

_IS_WINDOWS = platform.system() == "Windows"

logger = logging.getLogger("tools.environments.local")


def _state():
    """Return the ``tools.environments.local`` module (owner of the caches)."""
    from tools.environments import local

    return local


def _same_path(left: Path, right: Path) -> bool:
    """Compare path spellings with host filesystem case semantics."""
    return [os.path.normcase(p) for p in left.parts] == [os.path.normcase(p) for p in right.parts]


def _build_hermes_repo_root_aliases(
    resolved_root: Path, lexical_root: Path, configured_home: Path,
) -> tuple[Path, ...]:
    """Exact repo-root spellings emitted by Hermes launchers. Mirrors
    ``gateway_windows._preserve_hermes_home_path`` (physical path under the resolved
    HERMES_HOME -> configured spelling) so a junction-backed install matches without
    treating arbitrary HERMES_HOME descendants as Hermes-owned. A repo-level junction
    (possibly cross-drive) is accepted only when a strict resolve proves
    <root>/<repo dirname> is the physical root (fail-closed)."""
    candidates = [resolved_root, lexical_root]
    # Profile re-home: with --profile the configured home is <root>/profiles/<name>
    # and the repo lives beside the profiles dir (as get_default_hermes_root() does).
    home_candidates = [configured_home]
    if configured_home.parent.name == "profiles":
        home_candidates.append(configured_home.parent.parent)
    for home in home_candidates:
        try:
            resolved_home = home.resolve()
            home_key = os.path.normcase(str(resolved_home))
            if os.path.commonpath([home_key, os.path.normcase(str(resolved_root))]) == home_key:
                candidates.append(home / os.path.relpath(str(resolved_root), str(resolved_home)))
        except (OSError, ValueError):
            pass
    # Repo-level junction recovery (commonpath raises across drives, so the
    # home-relative mapping above cannot express a cross-drive link).
    for home in home_candidates:
        repo_candidate = home / resolved_root.name
        try:
            if repo_candidate.resolve(strict=True) == resolved_root.resolve(strict=True):
                candidates.append(repo_candidate)
        except OSError:
            pass
    aliases: list[Path] = []
    for candidate in candidates:
        if not any(_same_path(candidate, existing) for existing in aliases):
            aliases.append(candidate)
    return tuple(aliases)


def _validated_runtime_venv(env: dict) -> Path | None:
    """Producer-owned runtime venv, or None: PM's committed generation, else a sealed
    payload's shipped venv (its interpreter is not a venv, so ``_in_venv`` misses it),
    else VIRTUAL_ENV. The variable alone is not provenance (users carry unrelated
    venvs): require the legacy Windows base-Python producer's exact ``<repo>/venv``
    layout AND a real ``pyvenv.cfg``."""
    from pm.environments import payload_venv, runtime_facts_path, selected_venv

    root = Path(__file__).resolve().parents[2]
    if runtime_facts_path(root).is_file():
        return selected_venv(root)
    try:
        shipped = payload_venv(root)
    except (RuntimeError, OSError):  # a manifest that escapes its root reads as "not a payload"
        shipped = None
    # Require the real venv, as the VIRTUAL_ENV branch does: a stray manifest.json beside a
    # checkout must not hand back a phantom path that shadows the user's own venv.
    if shipped is not None and (shipped / "pyvenv.cfg").is_file():
        return shipped
    candidate = Path(env.get("VIRTUAL_ENV") or "")
    if not env.get("VIRTUAL_ENV") or not any(
            _same_path(candidate, root / "venv") for root in _state()._hermes_repo_root_aliases):
        return None
    try:
        return candidate if (candidate / "pyvenv.cfg").is_file() else None
    except OSError:
        return None


def _get_hermes_site_packages(env: dict) -> list[Path]:
    """Exact site-packages dirs owned by the Hermes runtime (cached):
    ``site.getsitepackages()`` with a ``sys.prefix`` fallback, plus a validated
    Windows base-interpreter launch's ``VIRTUAL_ENV/Lib/site-packages``."""
    local = _state()
    if local._hermes_site_packages is None:
        result: list[Path] = []
        if local._in_venv:
            try:
                import site
                result.extend(Path(sp) for sp in site.getsitepackages())
            except Exception:
                pass
            if not result:
                pyver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
                result.append(Path(sys.prefix) / "Lib" / "site-packages" if _IS_WINDOWS
                              else Path(sys.prefix) / "lib" / pyver / "site-packages")
        local._hermes_site_packages = list(result)
    result = list(local._hermes_site_packages)

    runtime_venv = _validated_runtime_venv(env)
    if runtime_venv is not None:
        from pm.environments import site_packages
        runtime_site_packages = site_packages(runtime_venv)
        if not any(_same_path(runtime_site_packages, existing) for existing in result):
            result.append(runtime_site_packages)
    return result


def _strip_hermes_owned_pythonpath_and_runtime_markers(env: dict) -> None:
    """Strip Hermes-owned PYTHONPATH entries, then the runtime marker vars. Order is
    load-bearing: PYTHONPATH filtering runs BEFORE the markers go so a validated Windows
    base-interpreter launch (VIRTUAL_ENV -> <repo>/venv) can still prove ownership."""
    _strip_hermes_owned_pythonpath(env)
    for _marker in _ACTIVE_VENV_MARKER_VARS:
        env.pop(_marker, None)


def _generation_site_packages_roots(env: dict) -> list[Path]:
    """Dependency-store roots whose ``<generation>/venv`` site-packages are Hermes-owned.

    One install can hold several PM generations, and a session that once ran on another
    one keeps that generation's site-packages in its exported PYTHONPATH. Only the
    SELECTED venv is in ``_get_hermes_site_packages``, so a stale generation's entry
    survived the exact-match check and leaked into every child shell (#chk-j3si.127).
    """
    roots: list[Path] = []

    def _add(candidate: Path) -> None:
        if not any(_same_path(candidate, existing) for existing in roots):
            roots.append(candidate)

    try:
        from pm.environments import install_state_dir
    except Exception:  # a tree without PM still strips the selected venv
        install_state_dir = None
    if install_state_dir is not None:
        for alias in _state()._hermes_repo_root_aliases:
            try:
                _add(Path(install_state_dir(alias)) / "environments")
            except Exception:
                continue
    # A borrowed root (hermes-home override, payload) proves its own store from the
    # validated runtime venv, whose parent chain is ``<environments>/<gen>/venv``.
    runtime_venv = _validated_runtime_venv(env)
    if runtime_venv is not None and runtime_venv.parent.parent.name == "environments":
        _add(runtime_venv.parent.parent)
    return roots


def _is_generation_site_packages(entry: str, roots: list[Path]) -> bool:
    """Is *entry* ``<store>/environments/<generation>/venv/**/site-packages``?

    Shape-checked and store-scoped on purpose: a user venv that merely spells the same
    layout elsewhere, or any other descendant of the store, stays user data.
    """
    path = Path(entry)
    if path.name != "site-packages":
        return False
    if path.parent.name == "Lib":
        venv = path.parent.parent
    elif path.parent.parent.name == "lib":
        version = path.parent.name
        if not (version.startswith("python") and version.count(".") == 1
                and all(part.isdigit() for part in version[len("python"):].split("."))):
            return False
        venv = path.parent.parent.parent
    else:
        return False
    if venv.name != "venv":
        return False
    # *roots* are the stores themselves (``<install-state>/environments``).
    return any(_same_path(venv.parent.parent, root) for root in roots)


def _strip_hermes_owned_pythonpath(env: dict) -> None:
    """Remove Hermes-owned PYTHONPATH entries: only exact matches of the repo root
    (any launcher spelling) and runtime site-packages — never descendants, which are
    user paths. Empty components (= cwd) and everything else are preserved.
    A stale PM generation's ``.../environments/<gen>/venv/**/site-packages`` is
    Hermes-owned too: it is this install's own dependency store, and on a child of a
    different Python version it shadows the project venv's compiled deps.

    Everything else -- user libs, Nix plugin paths, a pythonX.Y/site-packages entry meant for a DIFFERENT
    child version -- is preserved byte-for-byte: ownership is decided by path provenance, never by a
    cross-version heuristic (#74817 follow-up).
    """
    pp = env.get("PYTHONPATH")
    if not pp:
        return
    owned_paths = [*_get_hermes_site_packages(env), *_state()._hermes_repo_root_aliases]
    entries = pp.split(os.pathsep)
    # The dependency-store lookup (and its pm import) runs only when an entry could be one.
    generation_roots = (_generation_site_packages_roots(env)
                        if any(e.endswith("site-packages") for e in entries) else [])
    stripped = [e for e in entries if e and (any(_same_path(Path(e), p) for p in owned_paths)
                                             or _is_generation_site_packages(e, generation_roots))]
    kept = [e for e in entries if e not in stripped]
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    if stripped:
        logger.debug("Stripped Hermes-owned entries from PYTHONPATH: %s", stripped)
