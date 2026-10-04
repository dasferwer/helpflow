#!/bin/sh
set -eu

# Защита охватывает миграции и seed, которые выполняются раньше pytest.
python -m helpflow.test_safety
exec "$@"
