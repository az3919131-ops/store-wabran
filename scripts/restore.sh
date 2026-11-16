#!/bin/bash
# Restore: restore.sh backups/alwabrn_YYYYMMDD_HHMM.dump backups/filestore_YYYYMMDD_HHMM.tgz
set -euo pipefail
docker compose -f deploy/docker-compose.yml stop odoo
docker compose -f deploy/docker-compose.yml exec -T db dropdb -U odoo --if-exists alwabrn
docker compose -f deploy/docker-compose.yml exec -T db createdb -U odoo alwabrn
docker compose -f deploy/docker-compose.yml exec -T db pg_restore -U odoo -d alwabrn --no-owner < "$1"
docker compose -f deploy/docker-compose.yml run --rm -T --entrypoint bash odoo -c "tar -C /var/lib/odoo -xzf -" < "$2"
docker compose -f deploy/docker-compose.yml start odoo
