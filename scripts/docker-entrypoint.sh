#!/bin/sh
set -eu

alembic upgrade head
python -m helpflow.seed

exec "$@"
