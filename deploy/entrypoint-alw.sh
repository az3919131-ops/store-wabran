#!/bin/bash
# Starts Odoo for Al Wabran. Works with docker-compose and with Render (which sets PORT).
# All secrets come from environment variables, never from the repository.
set -e
: "${DB_HOST:=db}" "${DB_PORT:=5432}" "${DB_USER:=odoo}" "${DB_NAME:=alwabrn}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
HTTP_PORT="${PORT:-8069}"
ARGS=(--db_host "$DB_HOST" --db_port "$DB_PORT" --db_user "$DB_USER" --db_password "$DB_PASSWORD"
      -d "$DB_NAME" --db-filter "^${DB_NAME}$" --no-database-list --without-demo=all --proxy-mode
      --data-dir /var/lib/odoo --limit-memory-soft 400000000 --limit-memory-hard 480000000)
[ -n "$DB_SSLMODE" ] && ARGS+=(--db_sslmode "$DB_SSLMODE")

installed=$(python3 - <<PY
import os, psycopg2
try:
    c = psycopg2.connect(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], user=os.environ["DB_USER"],
                         password=os.environ["DB_PASSWORD"], dbname=os.environ["DB_NAME"],
                         sslmode=os.environ.get("DB_SSLMODE") or "prefer")
    cur = c.cursor()
    cur.execute("select state from ir_module_module where name='alwabrn_warehouse'")
    r = cur.fetchone(); print(r[0] if r else "none")
except Exception:
    print("none")
PY
)
if [ "$installed" != "installed" ]; then
    echo "First run: installing alwabrn_warehouse into $DB_NAME"
    odoo "${ARGS[@]}" -i alwabrn_warehouse --stop-after-init --no-http
    if [ -n "$ADMIN_PASSWORD" ]; then
        echo "Setting administrator password from ADMIN_PASSWORD"
        odoo shell "${ARGS[@]}" --no-http <<PY
env.ref("base.user_admin").password = """$ADMIN_PASSWORD"""
env.cr.commit()
PY
    fi
fi
exec odoo "${ARGS[@]}" --http-port "$HTTP_PORT"
