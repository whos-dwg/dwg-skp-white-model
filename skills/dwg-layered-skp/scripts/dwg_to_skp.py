"""Portable entry point for the installed skill; no project paths required."""
from pathlib import Path
import os
import sys

HERE = Path(__file__).resolve().parent
for directory in (HERE / "runtime", HERE / ".deps"):
    if directory.is_dir():
        sys.path.insert(0, str(directory))
for directory in reversed(os.environ.get("DWG_SKP_DEPENDENCIES", "").split(os.pathsep)):
    if directory:
        sys.path.insert(0, directory)

from dwg_semantic.pipeline import main

if __name__ == "__main__":
    raise SystemExit(main())
