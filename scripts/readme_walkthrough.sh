#!/usr/bin/env bash
# Fresh-machine walkthrough of the README.
#
# Extracts every fenced ```bash and ```python block from README.md, in
# order, and executes them in a fresh virtualenv inside a temp directory.
# The quickstart's `git clone` of the GitHub URL is redirected to the local
# checkout so the walkthrough is hermetic (no GitHub access needed); every
# other command runs verbatim.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "==> Creating fresh virtualenv"
python3 -m venv "$WORKDIR/venv"
# shellcheck disable=SC1091
source "$WORKDIR/venv/bin/activate"

echo "==> Extracting fenced bash/python blocks from README.md"
SCRIPT="$WORKDIR/walkthrough_commands.sh"
{
  echo "set -euxo pipefail"
  awk '
    BEGIN { mode = "" }
    /^```bash[[:space:]]*$/   { mode = "bash"; next }
    /^```python[[:space:]]*$/ { mode = "python"; print "python - <<'\''PYEOF'\''"; next }
    /^```[[:space:]]*$/ {
      if (mode == "python") print "PYEOF"
      mode = ""
      next
    }
    mode != "" { print }
  ' "$REPO_ROOT/README.md"
} > "$SCRIPT"

# Redirect the quickstart clone to the local checkout (hermetic walkthrough)
sed -i.bak \
  "s|git clone https://github.com/sensie-app/sensie-eval-harness|git clone \"$REPO_ROOT\" sensie-eval-harness|" \
  "$SCRIPT"

echo "==> Executing README commands in order"
cd "$WORKDIR"
bash "$SCRIPT"

echo "==> README walkthrough completed successfully"
