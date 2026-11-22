"""``python -m app [run|migrate|backup|check-config|new-migration "message"]``.

Running the bot goes through app/boot.py first (an update of the bot to finish or take back, or the console loop),
before anything else of the bot is imported: that step runs before pip has installed new dependencies, so this module
imports only the standard library and ``app.boot``. The commands themselves are app/cli.py."""

from __future__ import annotations

import sys

from app import boot

OPTIONS_WITH_VALUE = ("--env-file",)


def command(argv: list[str]) -> str:
    """The command ``argv`` names (``run`` when none), without argparse's help: only to know whether to boot."""
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg in OPTIONS_WITH_VALUE:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return "run"


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if command(argv) == "run" and not {"-h", "--help"} & set(argv):
        code = boot.start(argv)
        if code is not None:
            sys.exit(code)
    from app import cli

    cli.main(argv)


if __name__ == "__main__":
    main()
