#!/usr/bin/env bash
set -euo pipefail
source_root=$1
base=$2
default_branch=${3:-}
fetch_base=false

fetch_history() {
  if [ -n "${STYLE_CHECK_TOKEN:-}" ]; then
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
      GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$STYLE_CHECK_TOKEN" | base64 | tr -d '\n')" \
      git -C "$source_root" fetch --quiet --no-tags "$@"
  else
    git -C "$source_root" fetch --quiet --no-tags "$@"
  fi
}

if [[ -z "$base" || "$base" =~ ^0+$ ]]; then
  if [ -z "$default_branch" ]; then
    echo 'A comparison base or default branch is required' >&2
    exit 1
  fi
  base="refs/heads/$default_branch"
  git check-ref-format "$base"
  fetch_base=true
fi

if [ "$(git -C "$source_root" rev-parse --is-shallow-repository)" = true ]; then
  fetch_history --unshallow -- origin "$(git -C "$source_root" rev-parse HEAD)"
fi
if $fetch_base || ! git -C "$source_root" rev-parse --verify --quiet --end-of-options "$base^{commit}" >/dev/null; then
  fetch_history -- origin "$base"
  base=$(git -C "$source_root" rev-parse FETCH_HEAD)
fi
git -C "$source_root" merge-base "$base" HEAD
