"""
__main__.py - lets the package run as `python -m agentic`.

Kept to two lines on purpose. Everything real lives in cli.py, so the entry
point has nothing in it that could behave differently from an import.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
