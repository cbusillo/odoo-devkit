#!/usr/bin/env bash
set -euo pipefail
source_root=$1
base=${2:-}
default_branch=''
fetch_base=false

source_git() {
  if [ -n "${STYLE_CHECK_TOKEN:-}" ]; then
    GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=http.https://github.com/.extraheader GIT_CONFIG_VALUE_0='' \
      GIT_CONFIG_KEY_1=http.https://github.com/.extraheader \
      GIT_CONFIG_VALUE_1="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$STYLE_CHECK_TOKEN" | base64 | tr -d '\n')" \
      git -C "$source_root" "$@"
  else
    git -C "$source_root" "$@"
  fi
}

if [ -n "${GITHUB_EVENT_PATH:-}" ]; then
  default_branch=$(jq -r '.repository.default_branch // ""' "$GITHUB_EVENT_PATH")
  if [ -z "$base" ]; then
    base=$(jq -r --arg event "${GITHUB_EVENT_NAME:-}" --arg ref "${GITHUB_REF:-}" '
      if $event == "pull_request" then .pull_request.base.sha
      elif $event == "push" and $ref == ("refs/heads/" + (.repository.default_branch // "")) then .before
      else "" end // ""
    ' "$GITHUB_EVENT_PATH")
  fi
fi

if [[ -z "$base" || "$base" =~ ^0+$ ]]; then
  if [ -z "$default_branch" ]; then
    default_branch=$(source_git ls-remote --symref origin HEAD | sed -n 's|^ref: refs/heads/\(.*\)[[:space:]]HEAD$|\1|p')
  fi
  test -n "$default_branch"
  base="refs/heads/$default_branch"
  git check-ref-format "$base"
  fetch_base=true
fi

if [ "$(git -C "$source_root" rev-parse --is-shallow-repository)" = true ]; then
  source_git fetch --quiet --no-tags --unshallow -- origin "$(git -C "$source_root" rev-parse HEAD)"
fi
if $fetch_base || ! git -C "$source_root" rev-parse --verify --quiet --end-of-options "$base^{commit}" >/dev/null; then
  source_git fetch --quiet --no-tags -- origin "$base"
  base=$(git -C "$source_root" rev-parse FETCH_HEAD)
fi
git -C "$source_root" rev-parse --verify --end-of-options "$base^{commit}"
