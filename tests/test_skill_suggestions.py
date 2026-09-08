"""A new project must never be a dead end when its history lives under another key.

project_key is a hard partition (git remote, else path). The same work done from
a different machine or a different checkout gets a different key, so a brief can
correctly report "no prior sessions" while the history sits one key away. These
tests cover the hint that points the agent at it.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skill" / "agent-memory" / "memory.py"
_spec = importlib.util.spec_from_file_location("skill_memory", SKILL)
memory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(memory)


# --- the search seed -------------------------------------------------------

@pytest.mark.parametrize(
    "folder, expected",
    [
        ("RedLamb-build", "RedLamb"),      # 'RedLamb-build' scores 0.20; 'redlamb' scores 1.00
        ("scholarcal-marketing", "scholarcal marketing"),
        ("my_cool_project", "my cool"),
        ("mrie", "mrie"),
    ],
)
def test_search_seed_drops_scaffolding_words(tmp_path, folder, expected):
    d = tmp_path / folder
    d.mkdir()
    assert memory._search_seed(str(d)) == expected


def test_search_seed_keeps_name_when_every_token_is_generic(tmp_path):
    d = tmp_path / "build-dist"
    d.mkdir()
    assert memory._search_seed(str(d)) == "build dist"


# --- the suggestion itself -------------------------------------------------

PROJECTS = {
    "projects": [
        {"project_key": "path:/home/theif", "session_count": 4, "repo_root": None},
        {"project_key": r"path:C:\Users\theif", "session_count": 15, "repo_root": None},
        {"project_key": "github.com/ethanrxla/mrie", "session_count": 32,
         "repo_root": "/home/ethanrisden/mrie"},
        {"project_key": "path:/Volumes/T7 Shield/T7 shield/RedLamb-build",
         "session_count": 0, "repo_root": None},
    ]
}


def _fake_hub(search_results):
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if path.startswith("/v1/projects"):
            return PROJECTS
        if path == "/v1/search":
            return {"results": search_results}
        raise AssertionError(f"unexpected call {path}")

    return request, calls


def test_semantic_fallback_finds_history_with_no_lexical_overlap(tmp_path, monkeypatch, capsys):
    """The real RedLamb failure: the work is under path:/home/theif and
    path:C:\\Users\\theif, which share no substring with 'RedLamb-build'."""
    cwd = tmp_path / "RedLamb-build"
    cwd.mkdir()
    request, calls = _fake_hub([
        {"project_key": "path:/home/theif", "relevance": 1.0},
        {"project_key": r"path:C:\Users\theif", "relevance": 1.0},
        {"project_key": "github.com/ethanrxla/mrie", "relevance": 0.13},
    ])
    monkeypatch.setattr(memory, "request", request)

    memory._suggest_nearby("path:/Volumes/T7 Shield/T7 shield/RedLamb-build", str(cwd))

    out = capsys.readouterr().out
    assert "path:/home/theif" in out
    assert r"path:C:\Users\theif" in out
    # the seed must be the stripped name, not the raw folder
    search = [c for c in calls if c[1] == "/v1/search"]
    assert search and search[0][2]["query"] == "RedLamb"
    assert search[0][2]["scope"] == "global"


def test_low_relevance_projects_are_not_suggested(tmp_path, monkeypatch, capsys):
    """A forced weak match is what sends an agent off track."""
    cwd = tmp_path / "RedLamb-build"
    cwd.mkdir()
    request, _ = _fake_hub([{"project_key": "github.com/ethanrxla/mrie", "relevance": 0.13}])
    monkeypatch.setattr(memory, "request", request)

    memory._suggest_nearby("path:/Volumes/T7 Shield/T7 shield/RedLamb-build", str(cwd))

    assert "mrie" not in capsys.readouterr().out


def test_current_project_is_never_suggested_to_itself(tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "RedLamb-build"
    cwd.mkdir()
    pk = "path:/Volumes/T7 Shield/T7 shield/RedLamb-build"
    request, _ = _fake_hub([{"project_key": pk, "relevance": 1.0}])
    monkeypatch.setattr(memory, "request", request)

    memory._suggest_nearby(pk, str(cwd))

    assert "Related projects" not in capsys.readouterr().out


def test_lexical_and_semantic_hits_are_merged_without_duplicates(tmp_path, monkeypatch, capsys):
    cwd = tmp_path / "mrie"
    cwd.mkdir()
    request, _ = _fake_hub([{"project_key": "github.com/ethanrxla/mrie", "relevance": 1.0}])
    monkeypatch.setattr(memory, "request", request)

    memory._suggest_nearby("path:/somewhere/else", str(cwd))

    out = capsys.readouterr().out
    assert out.count("github.com/ethanrxla/mrie") == 1


def test_hub_failure_during_suggestion_is_not_fatal(tmp_path, monkeypatch, capsys):
    """brief already printed its answer; a failing hint must not kill the run."""
    cwd = tmp_path / "RedLamb-build"
    cwd.mkdir()

    def request(method, path, payload=None):
        if path.startswith("/v1/projects"):
            return PROJECTS
        raise SystemExit("hub error HTTP 500")

    monkeypatch.setattr(memory, "request", request)
    memory._suggest_nearby("path:/x", str(cwd))  # must not raise


# --- client defers to the hub ----------------------------------------------

def _brief_response(related):
    return {
        "display_name": "RedLamb-build", "sessions": [], "decisions": [],
        "open_threads": [], "session_count": 0, "estimated_tokens": 0,
        "message": "No prior sessions...\nRelated projects with recorded sessions:\n  - path:/home/theif",
        "related_projects": related,
    }


def _run_brief(monkeypatch, response, cwd):
    calls = []

    def request(method, path, payload=None):
        calls.append(path)
        if "/brief" in path:
            return response
        if path.startswith("/v1/projects"):
            return PROJECTS
        if path == "/v1/search":
            return {"results": [{"project_key": "path:/home/theif", "relevance": 1.0}]}
        raise AssertionError(path)

    monkeypatch.setattr(memory, "request", request)
    args = argparse.Namespace(
        project_key="path:/Volumes/T7 Shield/T7 shield/RedLamb-build",
        cwd=str(cwd), max_sessions=5,
    )
    memory.cmd_brief(args)
    return calls


def test_client_does_not_repeat_a_hint_the_hub_already_gave(tmp_path, monkeypatch, capsys):
    """The hub embeds the hint in `message`; printing it twice is noise."""
    cwd = tmp_path / "RedLamb-build"
    cwd.mkdir()
    calls = _run_brief(
        monkeypatch,
        _brief_response([{"project_key": "path:/home/theif", "session_count": 4, "why": "semantic match 1.00"}]),
        cwd,
    )
    out = capsys.readouterr().out
    assert out.count("Related projects with recorded sessions") == 1
    assert not any(c == "/v1/search" for c in calls), "hub already answered; don't re-search"


def test_client_still_helps_against_an_older_hub(tmp_path, monkeypatch, capsys):
    """A hub without the fix returns no related_projects — fall back locally."""
    cwd = tmp_path / "RedLamb-build"
    cwd.mkdir()
    response = _brief_response([])
    response["message"] = "No prior sessions recorded for this project."
    del response["related_projects"]

    _run_brief(monkeypatch, response, cwd)

    assert "path:/home/theif" in capsys.readouterr().out


def test_client_prints_the_hint_on_a_non_empty_brief(monkeypatch, capsys):
    """A young project gets both its own brief and the pointer to the rest."""
    def request(method, path, payload=None):
        return {
            "display_name": "RedLamb-build", "session_count": 1,
            "estimated_tokens": 263, "decisions": [], "open_threads": [],
            "sessions": [{"session_id": "01a081a4", "title": None, "tool": "codex",
                          "status": "idle", "summary": "only build artifacts, no source"}],
            "message": "This project has little history under this key...\n"
                       "Related projects with recorded sessions:\n  - path:/home/theif",
            "related_projects": [{"project_key": "path:/home/theif",
                                  "session_count": 4, "why": "semantic match 1.00"}],
        }

    monkeypatch.setattr(memory, "request", request)
    memory.cmd_brief(argparse.Namespace(
        project_key="path:/Volumes/T7 Shield/T7 shield/RedLamb-build", cwd=".", max_sessions=5))

    out = capsys.readouterr().out
    assert "only build artifacts" in out, "its own brief still shown"
    assert "path:/home/theif" in out, "and the pointer to the real history"
