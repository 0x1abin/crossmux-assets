#!/usr/bin/env bash
set -euo pipefail

[ "$#" -ge 3 ] || { echo "usage: $0 <repo> <tag> <asset...>" >&2; exit 2; }
[ -n "${GITEE_TOKEN:-}" ] || { echo "GITEE_TOKEN is required" >&2; exit 1; }

repo="$1"; tag="$2"; shift 2
api="https://gitee.com/api/v5/repos/${repo}"
existing="$(curl -fsS "${api}/releases/tags/${tag}?access_token=${GITEE_TOKEN}" 2>/dev/null || true)"
[ -z "$(printf '%s' "$existing" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id", ""))' 2>/dev/null || true)" ] || {
  echo "release ${tag} already exists" >&2
  exit 1
}

created="$(curl -fsS -X POST "${api}/releases" \
  --data-urlencode "access_token=${GITEE_TOKEN}" \
  --data-urlencode "tag_name=${tag}" \
  --data-urlencode "name=CrossMux dictionaries ${tag}" \
  --data-urlencode "body=Immutable StarDict resources for CrossMux." \
  --data-urlencode "prerelease=true" \
  --data-urlencode "target_commitish=main")"
release_id="$(printf '%s' "$created" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id", ""))')"
[ -n "$release_id" ] || { echo "Gitee release creation failed" >&2; exit 1; }

manifest=""
verify_upload() {
  local source="$1" response="$2" url expected actual temp
  url="$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("browser_download_url", ""))')"
  [ -n "$url" ] || { echo "Gitee asset upload returned no download URL" >&2; exit 1; }
  temp="$(mktemp)"
  curl -fsSL --max-time 600 "$url" -o "$temp"
  expected="$(sha256sum "$source" | cut -d' ' -f1)"
  actual="$(sha256sum "$temp" | cut -d' ' -f1)"
  rm -f "$temp"
  [ "$expected" = "$actual" ] || { echo "SHA-256 mismatch: $(basename "$source")" >&2; exit 1; }
}

for asset in "$@"; do
  if [ "$(basename "$asset")" = dictionaries.json ]; then
    manifest="$asset"
    continue
  fi
  uploaded="$(curl -fsS --max-time 600 -X POST "${api}/releases/${release_id}/attach_files" \
    -F "access_token=${GITEE_TOKEN}" -F "file=@${asset}")"
  verify_upload "$asset" "$uploaded"
done
[ -n "$manifest" ] || { echo "dictionaries.json is required" >&2; exit 1; }
uploaded="$(curl -fsS --max-time 600 -X POST "${api}/releases/${release_id}/attach_files" \
  -F "access_token=${GITEE_TOKEN}" -F "file=@${manifest}")"
verify_upload "$manifest" "$uploaded"

curl -fsS -X PATCH "${api}/releases/${release_id}" \
  --data-urlencode "access_token=${GITEE_TOKEN}" \
  --data-urlencode "tag_name=${tag}" \
  --data-urlencode "name=CrossMux dictionaries ${tag}" \
  --data-urlencode "body=Immutable StarDict resources for CrossMux." \
  --data-urlencode "prerelease=false" \
  --data-urlencode "target_commitish=main" >/dev/null
