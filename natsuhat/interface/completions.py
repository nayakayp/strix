"""Shell completion scripts and candidates for the Natsuhat CLI."""

from __future__ import annotations

import sys
from typing import Any

from natsuhat.interface.terminal_text import has_terminal_control, sanitize_terminal_text


_ROOT_COMMANDS = ("auth", "view", "completions", "completion")


def run_completions(argv: list[str]) -> int:
    """Print a shell integration script or hidden completion candidates."""
    if argv and argv[0] == "--candidates":
        for candidate in completion_candidates(argv[1:]):
            sys.stdout.write(candidate + "\n")
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(
            "Usage: natsuhat completions <zsh|bash|fish>\n\n"
            "Enable tab completion for the current shell:\n"
            "  zsh:  source <(natsuhat completions zsh)\n"
            "  bash: source <(natsuhat completions bash)\n"
            "  fish: natsuhat completions fish | source\n"
        )
        return 0
    shell = argv[0].lower()
    scripts = {"zsh": _zsh_script, "bash": _bash_script, "fish": _fish_script}
    generator = scripts.get(shell)
    if generator is None:
        sys.stderr.write(
            f"Unknown shell: {sanitize_terminal_text(shell)}. Choose zsh, bash, or fish.\n"
        )
        return 2
    sys.stdout.write(generator())
    return 0


def completion_candidates(words: list[str]) -> list[str]:
    """Return candidates for words after the ``natsuhat`` executable."""
    prior, current = _split_cursor(words)
    candidates = _matching(_ROOT_COMMANDS, current) if not prior else []
    # The line-oriented shell protocol cannot represent these names safely.
    # Omitting them is preferable to returning a sanitized path that does not exist.
    return [candidate for candidate in candidates if not has_terminal_control(candidate)]


def _split_cursor(words: list[str]) -> tuple[list[str], str]:
    if not words:
        return [], ""
    return words[:-1], words[-1]


def _matching(candidates: Any, prefix: str) -> list[str]:
    return sorted({str(candidate) for candidate in candidates if str(candidate).startswith(prefix)})


def _zsh_script() -> str:
    return r"""#compdef natsuhat
_natsuhat() {
  local -a candidates
  candidates=("${(@f)$($words[1] completions --candidates "${words[@]:2}")}")
  _describe 'natsuhat' candidates
}
compdef _natsuhat natsuhat
"""


def _bash_script() -> str:
    return r"""_natsuhat_completion() {
  local -a candidates
  local candidate
  while IFS= read -r candidate; do
    candidates+=("$candidate")
  done < <(natsuhat completions --candidates "${COMP_WORDS[@]:1:$COMP_CWORD}")
  COMPREPLY=("${candidates[@]}")
  for candidate in "${COMPREPLY[@]}"; do
    if [[ $candidate == */ ]]; then
      if type compopt >/dev/null 2>&1; then
        compopt -o nospace
      fi
      break
    fi
  done
}
complete -F _natsuhat_completion natsuhat
"""


def _fish_script() -> str:
    return r"""function __natsuhat_candidates
  set -l words (commandline -opc)
  set -e words[1]
  command natsuhat completions --candidates $words (commandline -ct)
end
complete -c natsuhat -f -a '(__natsuhat_candidates)'
"""
