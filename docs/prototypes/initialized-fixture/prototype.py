"""Interactive shell for the throwaway initialized-fixture prototype."""

from __future__ import annotations

import sys

from fixture_model import PrototypeState, fixture_files, init_message, reduce_state

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def clear() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def render(state: PrototypeState) -> None:
    files = fixture_files(state.sandbox)
    selected = files[state.selected]
    clear()
    print(f"{BOLD}PROTOTYPE — pycastle init fixture{RESET}")
    print(f"{DIM}Sandbox toggle changes only .pycastle/sandbox.{RESET}\n")
    print(f"{BOLD}State{RESET}")
    print(f"  sandbox: {state.sandbox}")
    print(f"  files:   {len(files)}")
    print(f"  selected: .pycastle/{selected.path} ({selected.mode})")
    print(f"  purpose:  {selected.purpose}\n")
    print(f"{BOLD}Generated tree{RESET}")
    for index, file in enumerate(files):
        marker = ">" if index == state.selected else " "
        print(
            f" {marker} [{index + 1:02}] .pycastle/{file.path}  {DIM}{file.mode}{RESET}"
        )
    print(f"\n{BOLD}.pycastle/{selected.path}{RESET}\n")
    print(selected.content.rstrip())
    print(f"\n{BOLD}Proposed init completion message{RESET}\n")
    print(init_message(state.sandbox))
    print(
        f"\n{BOLD}[n]{RESET} next  {BOLD}[p]{RESET} previous  "
        f"{BOLD}[s]{RESET} toggle sandbox  {BOLD}[number]{RESET} open  "
        f"{BOLD}[q]{RESET} quit"
    )


def parse_action(raw: str) -> str:
    value = raw.strip().lower()
    if value == "n":
        return "next"
    if value == "p":
        return "previous"
    if value == "s":
        return "sandbox"
    if value.isdigit():
        return f"select:{int(value) - 1}"
    return value


def main() -> int:
    state = PrototypeState()
    while True:
        render(state)
        try:
            raw = input("\nchoice> ")
        except EOFError:
            return 0
        action = parse_action(raw)
        if action == "q":
            return 0
        state = reduce_state(state, action)


if __name__ == "__main__":
    raise SystemExit(main())
