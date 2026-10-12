#!/usr/bin/env bash
# Run TrueNAS's own checks on the catalog package before submitting it:
# copy it into a checkout of github.com/truenas/apps, vendor their library
# (copy-lib), check ports, render, start it with Docker until it's healthy,
# and run the catalog validator. Needs git, python3 with PyYAML, and Docker.
#
#   deploy/truenas-catalog/check.sh [path to a truenas/apps checkout]
#
# Without a path it makes a shallow checkout in ./.truenas-apps (git-ignored).
# The result in ix-dev/community/mygarage is what goes into the pull request.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
apps="${1:-$here/../../.truenas-apps}"
validator=ghcr.io/truenas/apps_validation:latest

if [ ! -d "$apps/.git" ]; then
  git clone --quiet --depth 1 --filter=blob:none --sparse https://github.com/truenas/apps "$apps"
  git -C "$apps" sparse-checkout set --skip-checks ix-dev .github/scripts library devbox.json
fi
apps="$(cd "$apps" && pwd)"

rm -rf "$apps/ix-dev/community/mygarage"
cp -r "$here/mygarage" "$apps/ix-dev/community/mygarage"
cd "$apps"

echo "==> copy-lib (vendors the app library and fills in lib_version_hash)"
docker run --platform linux/amd64 --quiet --rm -e FAKE_ENV=1 -v "$PWD:/workspace" \
  "$validator" apps_catalog_hash_generate --path /workspace >/dev/null

echo "==> ports"
python3 .github/scripts/port_validation.py

echo "==> render, start and wait for healthy"
python3 .github/scripts/ci.py --app mygarage --train community --test-file basic-values.yaml

echo "==> catalog validator"
docker run --platform linux/amd64 --quiet --rm -e FAKE_ENV=1 -v "$PWD:/workspace" \
  "$validator" apps_dev_charts_validate validate --path /workspace

echo "==> metadata"
python3 .github/scripts/generate_metadata.py --app mygarage --train community

echo
echo "All checks passed. The package is in $apps/ix-dev/community/mygarage"
