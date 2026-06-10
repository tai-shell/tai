"""Module entrypoint — `python -m holo` runs the CLI.

Also serves as the PyInstaller entry script.
"""

from holo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
