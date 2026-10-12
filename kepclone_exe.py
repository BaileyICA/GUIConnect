"""PyInstaller entry point. Double-clicked with no arguments: opens the window on config.yaml next to the exe."""
import sys
from pathlib import Path

from kepclone.__main__ import main

if __name__ == "__main__":
    args = sys.argv[1:] or ["gui", str(Path(sys.executable).parent / "config.yaml")]
    sys.exit(main(args))
