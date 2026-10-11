#!/bin/sh
# Seed the data volume with empty defaults the first time, then start the app.
set -e
mkdir -p /app/data
for f in vessels.csv contacts.yaml zones.yaml; do
  [ -e "/app/data/$f" ] || cp "/app/defaults/$f" "/app/data/$f"
done
exec "$@"
