#!/usr/bin/env python3
"""Write the OpenAPI description of the FastAPI app to standard output.

`make swagger` redirects it to backend/swagger.json and `make swagger-check`
diffs it against the committed file. The script builds the app and calls its
`openapi()` method: no server starts, no settings are read and no database is
needed. Keys are sorted and the output is indented with two spaces, so the file
only changes when the API changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from app.main import create_app

    schema = create_app().openapi()
    sys.stdout.write(json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
