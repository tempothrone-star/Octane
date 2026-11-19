"""Octane command-line entry point."""
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "native":
        from .cli import main as native_main

        del sys.argv[1]
        return native_main()

    from .ledger.cli import main as ledger_main

    return ledger_main()


if __name__ == "__main__":
    raise SystemExit(main())
