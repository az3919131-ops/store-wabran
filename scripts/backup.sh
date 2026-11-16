#!/bin/bash
# Daily backup: database dump + filestore (attachments = source documents). Usage: backup.sh /backups
set -euo pipefail
DEST=${1:-./backups}; STAMP=$(date +%Y%m%d_%H%M); mkdir -p "$DEST"
docker compose -f deploy/docker-compose.yml exec -T db pg_dump -U odoo -Fc alwabrn > "$DEST/alwabrn_$STAMP.dump"
docker compose -f deploy/docker-compose.yml exec -T odoo tar -C /var/lib/odoo -czf - filestore > "$DEST/filestore_$STAMP.tgz"
find "$DEST" -name "*.dump" -mtime +30 -delete; find "$DEST" -name "*.tgz" -mtime +30 -delete
echo "Backup written to $DEST"
