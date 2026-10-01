#!/usr/bin/env bash
# Commit credit.json and oil.json only when their contents changed.
# Refuses to commit if any of the legacy feeds were touched.
set -euo pipefail

protected=(macro.json yields.json durability.json gpu_waterfall.json)
for file in "${protected[@]}"; do
  if ! git diff --quiet -- "$file" || ! git diff --cached --quiet -- "$file"; then
    echo "Refusing to commit because ${file} changed. This workflow must not touch it."
    exit 1
  fi
done

python3 - <<'PY'
import json
import sys
from pathlib import Path

for name in ("credit.json", "oil.json"):
    path = Path(name)
    if not path.exists():
        continue
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{name} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
PY

git add -- credit.json oil.json

cached="$(git diff --cached --name-only)"
if [ -z "$cached" ]; then
  echo "No changes to commit."
  exit 0
fi

while IFS= read -r name; do
  case "$name" in
    credit.json|oil.json) ;;
    *)
      echo "Refusing to commit unexpected file: ${name}"
      exit 1
      ;;
  esac
done <<< "$cached"

if git diff --cached --quiet; then
  echo "No changes to commit."
  exit 0
fi

git commit -m "Update credit and oil feeds"
git pull --rebase origin "$(git rev-parse --abbrev-ref HEAD)"
git push origin HEAD
echo "Pushed updated feeds."
