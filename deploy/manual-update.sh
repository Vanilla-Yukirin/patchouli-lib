#!/bin/sh
set -eu

usage() {
  echo "Usage: manual-update.sh IMAGE@sha256:DIGEST" >&2
  exit 64
}

if [ "$#" -ne 1 ]; then
  usage
fi

: "${PATCHOULI_DEPLOY_ROOT:?PATCHOULI_DEPLOY_ROOT is required}"
: "${PATCHOULI_IMAGE_REPOSITORY:?PATCHOULI_IMAGE_REPOSITORY is required}"

requested_image="$1"
digest_prefix="$PATCHOULI_IMAGE_REPOSITORY@sha256:"

case "$requested_image" in
  "$digest_prefix"*) digest="${requested_image#"$digest_prefix"}" ;;
  *)
    echo "Manual update rejected: an image from the configured repository and digest is required." >&2
    exit 64
    ;;
esac

case "$digest" in
  *[!0-9a-f]*)
    echo "Manual update rejected: invalid sha256 digest." >&2
    exit 64
    ;;
esac

if [ "${#digest}" -ne 64 ]; then
  echo "Manual update rejected: invalid sha256 digest." >&2
  exit 64
fi

if ! cd "$PATCHOULI_DEPLOY_ROOT" 2>/dev/null; then
  echo "Manual update configuration is unavailable." >&2
  exit 78
fi

if ! deploy_root="$(pwd -P)"; then
  echo "Manual update configuration is unavailable." >&2
  exit 78
fi

compose_file="${PATCHOULI_COMPOSE_FILE:-compose.yaml}"
runtime_env="${PATCHOULI_RUNTIME_ENV_FILE:-runtime.env}"
state_file="${PATCHOULI_STATE_FILE:-current-image}"

case "$compose_file" in
  /*) ;;
  *) compose_file="$deploy_root/$compose_file" ;;
esac
case "$runtime_env" in
  /*) ;;
  *) runtime_env="$deploy_root/$runtime_env" ;;
esac
case "$state_file" in
  /*) ;;
  *) state_file="$deploy_root/$state_file" ;;
esac

if [ ! -r "$compose_file" ] || [ ! -r "$runtime_env" ]; then
  echo "Manual update configuration is unavailable." >&2
  exit 78
fi

export PATCHOULI_IMAGE="$requested_image"

if ! docker compose \
  --env-file "$runtime_env" \
  --file "$compose_file" \
  config --quiet; then
  echo "Manual update failed while validating local Compose configuration." >&2
  exit 78
fi

if ! docker compose \
  --env-file "$runtime_env" \
  --file "$compose_file" \
  pull api; then
  echo "Manual update failed while pulling the requested image." >&2
  exit 1
fi

if ! docker compose \
  --env-file "$runtime_env" \
  --file "$compose_file" \
  up --detach --no-build --wait --wait-timeout 90 api; then
  echo "Manual update failed health checks." >&2
  echo "Automatic image rollback is disabled; inspect the service and database state before choosing a recovery action." >&2
  exit 1
fi

state_temp="${state_file}.tmp.$$"
trap 'rm -f "$state_temp"' EXIT HUP INT TERM
printf '%s\n' "$requested_image" > "$state_temp"
chmod 600 "$state_temp"
mv -f "$state_temp" "$state_file"
trap - EXIT HUP INT TERM

echo "Manual update completed and health checks passed."
