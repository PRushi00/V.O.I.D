"""Entry point so `python -m void ...` works."""
import sys

from void.cli import main

if __name__ == "__main__":
    sys.exit(main())
