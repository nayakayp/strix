from __future__ import annotations

from typing import Any

from natsuhat.interface.completions import completion_candidates, run_completions


def test_root_completion_candidates() -> None:
    assert completion_candidates(["au"]) == ["auth"]
    assert "cloud" not in completion_candidates([""])
    assert "completions" in completion_candidates([""])


def test_completion_scripts_cover_supported_shells(capsys: Any) -> None:
    for shell in ("zsh", "bash", "fish"):
        assert run_completions([shell]) == 0
        output = capsys.readouterr().out
        assert "completions --candidates" in output


def test_bash_completion_preserves_candidates_with_spaces(capsys: Any) -> None:
    assert run_completions(["bash"]) == 0
    output = capsys.readouterr().out
    assert 'COMPREPLY=("${candidates[@]}")' in output
    assert "while IFS= read -r candidate" in output
    assert "mapfile" not in output


def test_completion_rejects_unknown_shell(capsys: Any) -> None:
    assert run_completions(["powershell\x1b]52;c;payload\x07"]) == 2
    error = capsys.readouterr().err
    assert "Choose zsh, bash, or fish" in error
    assert "\x1b" not in error
    assert "\\x1b" in error
