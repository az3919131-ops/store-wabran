#!/bin/bash
# Starts Odoo for Al Wabran. Works with docker-compose and with Render (which sets PORT).
# All secrets come from environment variables, never from the repository.
set -e
: "${DB_HOST:=db}" "${DB_PORT:=5432}" "${DB_USER:=odoo}" "${DB_NAME:=alwabrn}"
: "${DB_PASSWORD:?DB_PASSWORD is required}"
HTTP_PORT="${PORT:-8069}"
ARGS=(--db_host "$DB_HOST" --db_port "$DB_PORT" --db_user "$DB_USER" --db_password "$DB_PASSWORD"
      -d "$DB_NAME" --db-filter "^${DB_NAME}$" --no-database-list --without-demo=True --proxy-mode
      --data-dir /var/lib/odoo --limit-memory-soft 400000000 --limit-memory-hard 480000000)
[ -n "$DB_SSLMODE" ] && ARGS+=(--db_sslmode "$DB_SSLMODE")

installed=$(python3 - <<PY
import os, psycopg2
try:
    c = psycopg2.connect(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], user=os.environ["DB_USER"],
                         password=os.environ["DB_PASSWORD"], dbname=os.environ["DB_NAME"],
                         sslmode=os.environ.get("DB_SSLMODE") or "prefer")
    cur = c.cursor()
    cur.execute("select state, latest_version from ir_module_module where name='alwabrn_warehouse'")
    r = cur.fetchone()
    if not r:
        print("none")
    else:
        import ast
        manifest = ast.literal_eval(open("/mnt/extra-addons/alwabrn_warehouse/__manifest__.py").read())
        print(r[0] if r[0] != "installed" or (r[1] or "") == manifest["version"] else "outdated")
except Exception:
    print("none")
PY
)
if [ "$installed" = "outdated" ]; then
    echo "Upgrading alwabrn_warehouse in $DB_NAME"
    odoo "${ARGS[@]}" -u alwabrn_warehouse --stop-after-init --no-http
elif [ "$installed" != "installed" ]; then
    echo "First run: installing alwabrn_warehouse into $DB_NAME"
    odoo "${ARGS[@]}" -i alwabrn_warehouse --stop-after-init --no-http
fi

# The administrator password always follows the ADMIN_PASSWORD variable (Render: Environment page).
# Change it there, not inside Odoo, otherwise it is reset on the next restart.
if [ -n "$ADMIN_PASSWORD" ]; then
    echo "Applying ADMIN_PASSWORD to the admin user"
    ALW_PW="$ADMIN_PASSWORD" odoo shell "${ARGS[@]}" --no-http <<'PY'
import os
pw = os.environ["ALW_PW"].strip()
admin = env.ref("base.user_admin")
admin.write({"login": "admin", "password": pw, "active": True})
# Hosts without a persistent disk (Render free plan): keep every file (photos, Excel, PDF,
# web assets) inside PostgreSQL so nothing is lost when the container restarts.
if os.environ.get("ALW_ATTACHMENTS_IN_DB", "1") == "1":
    env["ir.config_parameter"].sudo().set_param("ir_attachment.location", "db")
    Att = env["ir.attachment"].sudo().with_context(active_test=False)
    Att.search([("url", "=like", "/web/assets/%")]).unlink()   # regenerated on demand
    env.cr.execute("SELECT id, store_fname FROM ir_attachment WHERE store_fname IS NOT NULL AND db_datas IS NULL")
    lost = 0
    for att_id, fname in env.cr.fetchall():
        att = Att.browse(att_id)
        if os.path.exists(Att._full_path(fname)):
            att.write({"raw": att.raw})                          # move file into the database
        else:
            lost += 1
            att.unlink()                                         # file is gone: drop the dead reference
    if lost:
        for menu in env["ir.ui.menu"].sudo().with_context(active_test=False).search([("web_icon", "!=", False)]):
            menu.write({"web_icon": menu.web_icon})              # rebuild app icons from the addon files
        env["res.company"].sudo().search([])._update_asset_style()
    print("ATTACHMENTS_IN_DB=ON missing_files_cleaned=%s" % lost)
env.cr.commit()
# Encrypted seed data (catalog + opening stock count). Loaded once; safe to re-run.
if os.environ.get("ALWABRN_DATA_KEY") and os.path.isdir("/opt/alwabrn-seed"):
    try:
        from odoo.addons.alwabrn_warehouse.hooks import load_seed_files
        print("SEED_RESULT %s" % load_seed_files(env, "/opt/alwabrn-seed", os.environ["ALWABRN_DATA_KEY"]))
        env.cr.commit()
    except Exception as e:
        env.cr.rollback()
        print("SEED_FAILED %s: %s" % (type(e).__name__, e))
try:
    env["res.users"].sudo()._login({"type": "password", "login": "admin", "password": pw}, {"interactive": False})
    print("ADMIN_PASSWORD_CHECK=OK")
except Exception as e:
    print("ADMIN_PASSWORD_CHECK=FAILED %s" % type(e).__name__)
PY
fi
exec odoo "${ARGS[@]}" --http-port "$HTTP_PORT"
