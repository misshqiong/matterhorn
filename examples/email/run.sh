#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mh_bin="${MATTERHORN_MH_BIN:-mh}"
provider="${MATTERHORN_PROVIDER:-openai-compatible}"
scope="${MATTERHORN_EMAIL_DEMO_SCOPE:-email-demo}"

fail() {
  echo "email demo: $*" >&2
  exit 2
}

command -v "$mh_bin" >/dev/null 2>&1 ||
  fail "cannot find '$mh_bin'; install Matterhorn or set MATTERHORN_MH_BIN"

case "$provider" in
  fixture)
    [[ -n "${MATTERHORN_FIXTURE_PATH:-}" ]] ||
      fail "fixture mode requires MATTERHORN_FIXTURE_PATH"
    ;;
  openai-compatible)
    [[ -n "${MATTERHORN_MODEL:-}" ]] ||
      fail "set MATTERHORN_MODEL before running the email demo"
    [[ -n "${MATTERHORN_BASE_URL:-}" ]] ||
      fail "set MATTERHORN_BASE_URL before running the email demo"
    [[ -n "${MATTERHORN_API_KEY:-${OPENAI_API_KEY:-}}" ]] ||
      fail "set MATTERHORN_API_KEY or OPENAI_API_KEY before running the email demo"
    ;;
  anthropic)
    [[ -n "${MATTERHORN_MODEL:-}" ]] ||
      fail "set MATTERHORN_MODEL before running the email demo"
    [[ -n "${MATTERHORN_API_KEY:-${ANTHROPIC_API_KEY:-}}" ]] ||
      fail "set MATTERHORN_API_KEY or ANTHROPIC_API_KEY before running the email demo"
    ;;
  *)
    fail "unsupported MATTERHORN_PROVIDER '$provider'"
    ;;
esac

work_dir="${MATTERHORN_EMAIL_DEMO_DIR:-$(mktemp -d)}"
mkdir -p "$work_dir"
db="$work_dir/email-demo.db"
html="${MATTERHORN_EMAIL_DEMO_HTML:-$work_dir/email-demo.html}"

(
  cd "$work_dir"
  "$mh_bin" init --db "$db"
  "$mh_bin" add "$script_dir/demo.mbox" \
    --adapter email \
    --scope "$scope" \
    --db "$db" \
    --provider "$provider"
  "$mh_bin" flush "$scope" \
    --db "$db" \
    --provider "$provider"
  "$mh_bin" export "$scope" \
    --format html \
    --out "$html" \
    --db "$db"
)

echo "email demo HTML: $html"
