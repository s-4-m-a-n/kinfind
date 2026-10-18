#!/bin/sh
# Container start-up: restore the database, prepare it, then run the app under Litestream,
# which copies every change to Cloud Storage and makes a final copy when the container stops.
set -e
DB="$DISASTERLENS_DATA/disasterlens.db"

if [ -n "$LITESTREAM_BUCKET" ]; then
  litestream restore -config /etc/litestream.yml -if-db-not-exists -if-replica-exists "$DB"
fi

# Tables, then the first admin (ADMIN_EMAIL / ADMIN_PASSWORD), then optional demo zones and
# found records. The admin exists before seeding, so the seed adds no demo sign-ins.
python -c "from app import accounts, db; db.init(); accounts.bootstrap_admin()"
if [ "$KINFIND_DEMO_DATA" = "1" ]; then
  python -m app.seed
fi

APP="uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"
if [ -n "$LITESTREAM_BUCKET" ]; then
  exec litestream replicate -config /etc/litestream.yml -exec "$APP"
fi
exec $APP
