#!/bin/sh
set -eu

: "${PATCHOULI_DEPLOY_ROOT:?PATCHOULI_DEPLOY_ROOT is required}"
: "${PATCHOULI_IMAGE_REPOSITORY:?PATCHOULI_IMAGE_REPOSITORY is required}"

compose_file="${PATCHOULI_COMPOSE_FILE:-$PATCHOULI_DEPLOY_ROOT/compose.yaml}"
runtime_env="${PATCHOULI_RUNTIME_ENV_FILE:-$PATCHOULI_DEPLOY_ROOT/runtime.env}"
state_file="${PATCHOULI_STATE_FILE:-$PATCHOULI_DEPLOY_ROOT/current-image}"
requested_image="${SSH_ORIGINAL_COMMAND:-${1:-}}"
digest_prefix="${PATCHOULI_IMAGE_REPOSITORY}@sha256:"

case "$requested_image" in
  "$digest_prefix"*) digest="${requested_image#"$digest_prefix"}" ;;
  *)
    echo "Deployment rejected: an image from the configured repository and digest is required." >&2
    exit 64
    ;;
esac

if ! printf '%s' "$digest" | grep -Eq '^[0-9a-f]{64}$'; then
  echo "Deployment rejected: invalid sha256 digest." >&2
  exit 64
fi

if [ ! -r "$compose_file" ] || [ ! -r "$runtime_env" ]; then
  echo "Deployment configuration is unavailable." >&2
  exit 78
fi

previous_image=""
if [ -r "$state_file" ]; then
  previous_image="$(sed -n '1p' "$state_file")"
fi

cd "$PATCHOULI_DEPLOY_ROOT"
export PATCHOULI_IMAGE="$requested_image"
docker compose --env-file "$runtime_env" --file "$compose_file" pull api

if docker compose \
  --env-file "$runtime_env" \
  --file "$compose_file" \
  up --detach --no-build --wait --wait-timeout 90 api; then
  state_temp="${state_file}.tmp.$$"
  printf '%s\n' "$requested_image" > "$state_temp"
  chmod 600 "$state_temp"
  mv -f "$state_temp" "$state_file"
  echo "Deployment completed and health checks passed."
  exit 0
fi

echo "Deployment failed; attempting image rollback." >&2
if [ -n "$previous_image" ]; then
  export PATCHOULI_IMAGE="$previous_image"
  docker compose \
    --env-file "$runtime_env" \
    --file "$compose_file" \
    up --detach --no-build --wait --wait-timeout 90 api
  echo "Previous image restored." >&2
else
  echo "No previous image is recorded; automatic rollback is unavailable." >&2
  docker compose \
    --env-file "$runtime_env" \
    --file "$compose_file" \
    stop api || true
fi

exit 1
