"""Entry point for `python -m phonehome` (run from the parent folder).

The modules import each other flatly (`from geo import ...`) so that
`python server.py` still works from inside this folder. Putting this folder on
sys.path lets both entry points resolve without duplicating imports.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import main  # noqa: E402

if __name__ == "__main__":
    main()
