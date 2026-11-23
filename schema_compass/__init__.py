"""Schema Compass: semantic source-to-target matching for data migrations."""
import sys

__version__ = "1.0.0"

if sys.version_info < (3, 11):
    raise SystemExit(
        f"Schema Compass needs Python 3.11 or later; this is Python {sys.version.split()[0]}. "
        "Install a newer Python, delete the .venv folder and create it again (see Troubleshooting in README.md)."
    )
