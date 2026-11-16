#!/bin/bash
# Starts Odoo; installs the Al Wabran addon on first run. All secrets come from the environment.
set -e
: "${DB_NAME:=alwabrn}"
ARGS=(--db_host "${HOST:-db}" --db_port "${PORT:-5432}" --db_user "${USER:-odoo}" --db_password "${PASSWORD:?PASSWORD is required}"
      -d "$DB_NAME" --db-filter "^${DB_NAME}$" --no-database-list --without-demo=all --proxy-mode
      --http-port "${HTTP_PORT:-8069}")
if ! odoo "${ARGS[@]}" shell --no-http <<< "exit(0 if env['ir.module.module'].search([('name','=','alwabrn_warehouse'),('state','=','installed')]) else 1)" >/dev/null 2>&1; then
    echo "First run: installing alwabrn_warehouse into $DB_NAME"
    odoo "${ARGS[@]}" -i alwabrn_warehouse --stop-after-init
fi
exec odoo "${ARGS[@]}"
