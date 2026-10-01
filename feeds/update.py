"""Build credit.json and oil.json. Protected feeds are never opened for writing."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import traceback
from pathlib import Path

from feeds.credit import collect_credit
from feeds.oil import collect_oil
from feeds.serialize import FORBIDDEN_OUTPUTS, write_if_changed
from feeds.validate import validate_credit, validate_oil

PROTECTED = (
    "macro.json",
    "yields.json",
    "durability.json",
    "gpu_waterfall.json",
)


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _guard(root: Path) -> dict[str, str | None]:
    for name in FORBIDDEN_OUTPUTS:
        if name not in PROTECTED:
            raise RuntimeError(f"protected list drifted for {name}")
    return {name: _sha256(root / name) for name in PROTECTED}


def _publish(path: Path, payload: dict, validate) -> bool:
    validate(payload)
    return write_if_changed(path, payload)


def build(root: Path, *, only: str | None = None) -> int:
    """Write the feeds. Returns 0 when every requested feed was built."""
    root = root.resolve()
    before = _guard(root)
    fred_key = os.environ.get("FRED_API_KEY") or None
    eia_key = os.environ.get("EIA_API_KEY") or None
    if fred_key == "":
        fred_key = None
    if eia_key == "":
        eia_key = None

    failures: list[str] = []
    wrote: list[str] = []

    if only in (None, "credit"):
        try:
            payload = collect_credit(fred_key)
            changed = _publish(root / "credit.json", payload, validate_credit)
            wrote.append("credit.json")
            regime = payload["regime"]["label"]
            hy = payload["series"]["hy_oas"]
            print(
                f"credit.json {'wrote' if changed else 'unchanged'} "
                f"regime={regime} hy={hy['value']} {hy['unit']} as_of={hy['as_of']} stale={hy['stale']}"
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"credit: {exc}")
            traceback.print_exc()

    if only in (None, "oil"):
        try:
            payload = collect_oil(fred_api_key=fred_key, eia_api_key=eia_key)
            changed = _publish(root / "oil.json", payload, validate_oil)
            wrote.append("oil.json")
            brent = payload["brent"]
            print(
                f"oil.json {'wrote' if changed else 'unchanged'} "
                f"brent={brent['value']} {brent['unit']} as_of={brent['as_of']} "
                f"alert={payload['alert']['flag']}"
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"oil: {exc}")
            traceback.print_exc()

    after = _guard(root)
    if after != before:
        drifted = [name for name in PROTECTED if after[name] != before[name]]
        print("ERROR: protected files changed: " + ", ".join(drifted), file=sys.stderr)
        return 1

    if failures:
        print("FEED FAILURES:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        print("Previous copies of the failed files were left in place.", file=sys.stderr)
        return 1

    if not wrote:
        print("Nothing was built.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update credit.json and oil.json")
    parser.add_argument(
        "--only",
        choices=("credit", "oil"),
        help="Build one feed. The default builds both.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for the JSON files. Defaults to the repository root.",
    )
    args = parser.parse_args(argv)
    root = args.output_dir
    if root is None:
        root = Path(__file__).resolve().parents[1]
    return build(root, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
