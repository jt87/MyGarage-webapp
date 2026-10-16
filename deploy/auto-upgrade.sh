#!/bin/sh
# Overnight automatic upgrades for MyGarage on TrueNAS SCALE.
#
# Run it from a TrueNAS Cron Job as root, e.g. daily at 3:00 AM:
#     /mnt/tank/scripts/mygarage-auto-upgrade.sh mygarage
# (the argument is the app's name in TrueNAS Apps; default "mygarage").
#
# What it does:
#   1. Pulls the image tag the app follows (ghcr.io/jt87/mygarage:1).
#   2. Nothing new: stops. A new major version (2.x) is never installed;
#      the app tells you about it instead.
#   3. Otherwise asks TrueNAS to redeploy the app, then waits for it to report
#      healthy. If it doesn't, the previous image is put back and redeployed.
#   4. Tells the app what happened, for Settings → About.
#
# An image pinned to one version (1.2.0, sha-…, or @sha256:…) is left alone.
# Errors go to stderr, so a Cron Job with "Hide Standard Output" ticked only
# emails you when something went wrong.

set -u

APP="${1:-mygarage}"
WAIT="${MYGARAGE_UPGRADE_WAIT:-600}"     # seconds to wait for the new version to be healthy
POLL="${MYGARAGE_UPGRADE_POLL:-10}"
LOCK="/tmp/mygarage-auto-upgrade-$APP.lock"
# Remembers an image that failed, so it isn't retried every night. Kept next to
# this script (on a pool, so it survives TrueNAS updates).
FAILED="${MYGARAGE_UPGRADE_STATE:-$(dirname "$0")}/.mygarage-auto-upgrade-$APP.failed"

say() { echo "mygarage-auto-upgrade: $*"; }
fail() { echo "mygarage-auto-upgrade: $*" >&2; }

if ! mkdir "$LOCK" 2>/dev/null; then
  fail "another upgrade of $APP is already running ($LOCK); skipping"
  exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

container() {
  # The app's MyGarage container (TrueNAS names compose projects ix-<app>).
  docker ps --filter "label=com.docker.compose.project=ix-$APP" --format '{{.Names}} {{.Image}}' \
    | awk '$2 ~ /mygarage/ { print $1; exit }'
}

label_version() {
  docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$1" 2>/dev/null
}

major() {
  # "1.2.3" -> "1"; anything else (edge, dev) -> ""
  echo "$1" | sed -n 's/^v\{0,1\}\([0-9][0-9]*\)\.[0-9][0-9]*\.[0-9][0-9]*$/\1/p'
}

report() {
  # Tell the app; versions before 1.2 can't hear it, which is fine.
  c="$(container)"
  [ -n "$c" ] && docker exec "$c" python -m app.releases record "$@" >/dev/null 2>&1
  return 0
}

redeploy() {
  midclt call -j app.redeploy "$APP" >/dev/null
}

wait_healthy() {
  waited=0
  while [ "$waited" -lt "$WAIT" ]; do
    sleep "$POLL"
    waited=$((waited + POLL))
    c="$(container)"
    [ -n "$c" ] || continue
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null)"
    case "$health" in
      healthy) return 0 ;;
      unhealthy) return 1 ;;
      none)
        # No health check: running for a minute and a half is good enough.
        [ "$waited" -ge 90 ] && [ "$(docker inspect -f '{{.State.Running}}' "$c")" = "true" ] && return 0 ;;
    esac
  done
  return 1
}

for tool in docker midclt; do
  command -v "$tool" >/dev/null 2>&1 || { fail "$tool not found; run this on TrueNAS SCALE as root"; exit 1; }
done

C="$(container)"
if [ -z "$C" ]; then
  fail "no running MyGarage container for app '$APP'. Is the app name right and the app started?"
  exit 1
fi

REF="$(docker inspect -f '{{.Config.Image}}' "$C")"
OLD_ID="$(docker inspect -f '{{.Image}}' "$C")"
OLD_VERSION="$(label_version "$OLD_ID")"
TAG="${REF##*:}"
case "$REF" in
  *@sha256:*) TAG="$REF" ;;
  *:*) ;;
  *) TAG="latest" ;;
esac

case "$TAG" in
  *@sha256:*|sha-*|[0-9]*.[0-9]*.[0-9]*)
    say "$REF is pinned to one version; nothing to do. Use ghcr.io/jt87/mygarage:1 to get upgrades."
    report --result pinned --from "$OLD_VERSION" --detail "$TAG"
    exit 0 ;;
esac

if ! docker pull -q "$REF" >/dev/null; then
  fail "could not pull $REF (offline, or GitHub's registry is down); will try again next run"
  exit 1
fi
NEW_ID="$(docker image inspect -f '{{.Id}}' "$REF")"

if [ "$NEW_ID" = "$OLD_ID" ]; then
  say "$REF is current ($OLD_VERSION)"
  report --result current --from "$OLD_VERSION"
  exit 0
fi

NEW_VERSION="$(label_version "$NEW_ID")"
if [ "$(cat "$FAILED" 2>/dev/null)" = "$NEW_ID" ]; then
  docker tag "$OLD_ID" "$REF"
  say "${NEW_VERSION:-the new version} failed before; staying on ${OLD_VERSION:-the current version} until a fixed release"
  report --result rolled_back --from "$OLD_VERSION" --to "$NEW_VERSION" --detail "failed before; waiting for the next release"
  exit 0
fi
OLD_MAJOR="$(major "$OLD_VERSION")"
NEW_MAJOR="$(major "$NEW_VERSION")"
if [ -n "$OLD_MAJOR" ] && [ -n "$NEW_MAJOR" ] && [ "$NEW_MAJOR" != "$OLD_MAJOR" ]; then
  # A tag like `latest` moved to a new major version. Put the old image back
  # under the tag so a restart doesn't pick the new one up by accident.
  docker tag "$OLD_ID" "$REF"
  say "$NEW_VERSION is available but needs you to update; staying on $OLD_VERSION"
  report --result major --from "$OLD_VERSION" --to "$NEW_VERSION"
  exit 0
fi

say "upgrading $APP from ${OLD_VERSION:-unknown} to ${NEW_VERSION:-unknown}"
if redeploy && wait_healthy; then
  say "upgraded to ${NEW_VERSION:-the new version}"
  report --result upgraded --from "$OLD_VERSION" --to "$NEW_VERSION"
  rm -f "$FAILED"
  docker rmi "$OLD_ID" >/dev/null 2>&1 || true
  exit 0
fi

fail "${NEW_VERSION:-the new version} did not become healthy within ${WAIT}s; rolling back to ${OLD_VERSION:-the previous version}"
echo "$NEW_ID" > "$FAILED" 2>/dev/null || fail "could not write $FAILED"
docker tag "$OLD_ID" "$REF"
if redeploy && wait_healthy; then
  report --result rolled_back --from "$OLD_VERSION" --to "$NEW_VERSION" --detail "not healthy after ${WAIT}s"
  fail "rolled back to ${OLD_VERSION:-the previous version}"
else
  fail "the rollback did not come up healthy either; open the app in TrueNAS Apps and check its logs"
fi
exit 1
