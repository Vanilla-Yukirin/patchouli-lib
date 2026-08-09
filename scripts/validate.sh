#!/bin/sh
set -eu

exec python scripts/validate.py "$@"
