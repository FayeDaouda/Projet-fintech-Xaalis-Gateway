"""Exporte le contrat OpenAPI dans ``openapi.json`` (livrable versionné).

    uv run python scripts/export_openapi.py          # régénère le fichier
    uv run python scripts/export_openapi.py --check  # échoue s'il n'est pas à jour (CI)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from xaalis.api.app import create_app

TARGET = Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text() != content:
            sys.stderr.write("openapi.json n'est pas à jour : uv run python scripts/export_openapi.py\n")
            return 1
        return 0
    TARGET.write_text(content)
    sys.stdout.write(f"{TARGET.name} : {len(json.loads(content)['paths'])} chemins exportés\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
