#!/usr/bin/env bash
# run_doc_tests.sh — execute every fenced ```bash block in README.md and docs/*.md.
#
# Conventions (the docs are written to these rules):
#   * ```bash blocks run unconditionally — they must succeed offline.
#   * A block preceded immediately by `<!-- doctest: api -->` runs ONLY when
#     SENSIE_API_KEY is set; otherwise it is reported as SKIP. These blocks
#     may post metered reads against the live/local API.
#   * ```text / ```json blocks are illustrative output and never executed.
#   * `pip install sensie-eval` is shimmed to `pip install -e <repo-root>`
#     until the package is on PyPI, so the documented command still exercises
#     the real install path.
#
# A block passes when it exits 0 (blocks run under plain bash, not bash -e,
# so documented exit-code-handling examples work as written).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Local convenience: if pip isn't on PATH but the repo venv exists, use it.
# (CI provides pip via actions/setup-python; this branch never triggers there.)
if ! command -v pip >/dev/null 2>&1 && [ -x "$REPO_ROOT/venv/bin/pip" ]; then
  export PATH="$REPO_ROOT/venv/bin:$PATH"
fi

FILES=(README.md docs/*.md)
PASS=0; SKIP=0; FAILED=0

run_block() {
  local file="$1" line="$2" mode="$3" body="$4"
  local label="$file:$line"
  if [ "$mode" = "api" ] && [ -z "${SENSIE_API_KEY:-}" ]; then
    echo "SKIP  $label (api block; SENSIE_API_KEY not set)"
    SKIP=$((SKIP+1)); return
  fi
  if [ "$mode" = "macos" ]; then
    if [ "$(uname)" != "Darwin" ] || ! command -v pipx >/dev/null 2>&1; then
      echo "SKIP  $label (macos block; needs Darwin + pipx)"
      SKIP=$((SKIP+1)); return
    fi
    # brew presence is the block's precondition, not something to install here.
    body="${body//brew install pipx/command -v pipx >\/dev\/null}"
    body="${body//pipx install sensie-eval/pipx install --force -q -e \"$REPO_ROOT\" >\/dev\/null}"
  fi
  # Shim: package not yet on PyPI.
  body="${body//pip install sensie-eval/pip install -q -e \"$REPO_ROOT\"}"
  # Placeholder keys in docs must not clobber a real key from the environment.
  body="${body//export SENSIE_API_KEY=sk_sensie_your_key_here/: # key comes from the environment}"
  if out=$(bash -c "$body" 2>&1); then
    echo "PASS  $label"
    PASS=$((PASS+1))
  else
    echo "FAIL  $label (exit $?)"
    echo "$out" | sed 's/^/      /' | tail -15
    FAILED=$((FAILED+1))
  fi
}

for file in "${FILES[@]}"; do
  mode="plain"; in_block=0; body=""; start=0; lineno=0
  while IFS='' read -r line || [ -n "$line" ]; do
    lineno=$((lineno+1))
    if [ "$in_block" = 0 ]; then
      case "$line" in
        *"<!-- doctest: api -->"*) mode="api" ;;
        *"<!-- doctest: macos -->"*) mode="macos" ;;
        '```bash'*) in_block=1; body=""; start=$lineno ;;
        '```'*) mode="plain" ;;  # a non-bash fence consumes any pending marker
        "") : ;;                  # blank lines keep a pending marker alive
        *) [ "$mode" != "plain" ] && case "$line" in \#*|\<*) : ;; *) mode="plain";; esac ;;
      esac
    else
      if [ "$line" = '```' ]; then
        in_block=0
        run_block "$file" "$start" "$mode" "$body"
        mode="plain"
      else
        body+="$line"$'\n'
      fi
    fi
  done < "$file"
done

echo
echo "doc-tests: $PASS passed, $SKIP skipped, $FAILED failed"
[ "$FAILED" -eq 0 ]
