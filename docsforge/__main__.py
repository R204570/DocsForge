"""`python -m docsforge <URL> [options]` — the extraction CLI."""

import sys

from docsforge.core.engine import main

if __name__ == "__main__":
    sys.exit(main())
