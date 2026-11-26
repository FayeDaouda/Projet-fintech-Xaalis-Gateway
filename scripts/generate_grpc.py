"""Génère le code Python du contrat gRPC (proto/ → src/), avec des stubs typés (mypy --strict).

    uv run python scripts/generate_grpc.py           # régénère
    uv run python scripts/generate_grpc.py --check   # CI : échoue si le code versionné diffère

Le code généré est versionné : l'image Docker n'embarque pas grpcio-tools.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
import tempfile
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
PROTO_ROOT = ROOT / "proto"
PROTOS = sorted(PROTO_ROOT.rglob("*.proto"))
SUFFIXES = ("_pb2.py", "_pb2.pyi", "_pb2_grpc.py", "_pb2_grpc.pyi")
HEADER = "# Fichier généré par scripts/generate_grpc.py — ne pas modifier.\n"


def generate(out: Path) -> list[Path]:
    include = str(Path(protoc.__file__).parent / "_proto")
    for proto in PROTOS:
        code = protoc.main(
            [
                "grpc_tools.protoc",
                f"-I{PROTO_ROOT}",
                f"-I{include}",
                f"--python_out={out}",
                f"--mypy_out={out}",
                f"--grpc_python_out={out}",
                f"--mypy_grpc_out={out}",
                str(proto.relative_to(PROTO_ROOT)),
            ]
        )
        if code != 0:
            sys.exit(f"protoc a échoué pour {proto}")
    files = sorted(p for p in out.rglob("*") if p.name.endswith(SUFFIXES))
    for path in files:
        path.write_text(HEADER + path.read_text())
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        generated = generate(Path(tmp))
        mismatches = []
        for path in generated:
            relative = path.relative_to(tmp)
            target = ROOT / "src" / relative
            if args.check:
                if not target.exists() or not filecmp.cmp(path, target, shallow=False):
                    mismatches.append(str(relative))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                print(f"généré : src/{relative}")  # noqa: T201 — outil en ligne de commande
    if mismatches:
        print("code gRPC périmé, relancez scripts/generate_grpc.py :", *mismatches, sep="\n  ")  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
