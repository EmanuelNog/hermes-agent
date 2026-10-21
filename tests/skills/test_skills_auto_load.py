"""``skills.auto_load`` — the mechanism that injects always-on skills into every session.

These tests pin the contract the standing-standards skill depends on: a configured
skill's body reaches the prompt, a typo never blocks session start, and the order is
the config's order. They are deliberately home-independent (a tmp_path home), so they
describe the mechanism rather than any one machine's config.
"""

from __future__ import annotations

from pathlib import Path

from agent.skill_commands import build_auto_load_prompt

SKILL_TEMPLATE = """---
name: {name}
description: "test fixture"
---

# {name}

BODY-OF-{name}
"""


def _home(tmp_path: Path, *names: str, category: str = "system") -> Path:
    """A throwaway HERMES_HOME holding the named skills under skills/<category>/<name>."""
    for name in names:
        skill_dir = tmp_path / "skills" / category / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(SKILL_TEMPLATE.format(name=name))
    return tmp_path


def _cfg(*names: str) -> dict:
    return {"skills": {"auto_load": list(names)}}


def test_auto_load_injects_the_skill_body(tmp_path):
    home = _home(tmp_path, "always-on")
    text, loaded, missing = build_auto_load_prompt(
        user_config=_cfg("always-on"), home_override=home
    )
    assert loaded == ["always-on"], loaded
    assert missing == []
    assert "BODY-OF-always-on" in text
    # the loader must mark it as active guidance, not as passive context
    assert "auto-loaded via config (skills.auto_load)" in text


def test_unknown_name_is_reported_and_never_raises(tmp_path):
    """A typo in config must not block session start."""
    text, loaded, missing = build_auto_load_prompt(
        user_config=_cfg("no-such-skill"), home_override=_home(tmp_path)
    )
    assert loaded == []
    assert missing == ["no-such-skill"]
    assert text == ""


def test_empty_auto_load_is_a_noop(tmp_path):
    text, loaded, missing = build_auto_load_prompt(
        user_config=_cfg(), home_override=_home(tmp_path)
    )
    assert (text, loaded, missing) == ("", [], [])


def test_no_config_at_all_is_a_noop(tmp_path):
    text, loaded, missing = build_auto_load_prompt(
        user_config={}, home_override=_home(tmp_path)
    )
    assert (text, loaded, missing) == ("", [], [])


def test_multiple_skills_keep_config_order(tmp_path):
    home = _home(tmp_path, "first", "second")
    text, loaded, missing = build_auto_load_prompt(
        user_config=_cfg("first", "second"), home_override=home
    )
    assert loaded == ["first", "second"]
    assert missing == []
    assert text.index("BODY-OF-first") < text.index("BODY-OF-second")


def test_one_bad_name_does_not_hide_the_good_one(tmp_path):
    """A single typo must not cost the operator the skills that do exist."""
    home = _home(tmp_path, "good")
    text, loaded, missing = build_auto_load_prompt(
        user_config=_cfg("good", "typo"), home_override=home
    )
    assert loaded == ["good"]
    assert missing == ["typo"]
    assert "BODY-OF-good" in text
