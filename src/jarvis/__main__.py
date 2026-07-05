"""Entry point for `python -m jarvis` and the `jarvis` console script.

The real REPL is wired up in task 8. For now this is a liveness placeholder so
the package is runnable and the console-script entry point is exercised.
"""

from jarvis import __version__


def main() -> None:
    print(f"Jarvis v{__version__} - scaffold is alive. The REPL arrives in task 8.")


if __name__ == "__main__":
    main()
