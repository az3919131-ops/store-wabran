# Al Wabran (root copy used by Render) Smart Inventory — Odoo 19 Community + alwabrn_warehouse
FROM odoo:19.0
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ara poppler-utils fonts-noto-core \
    && (apt-get install -y --no-install-recommends node-rtlcss || echo "rtlcss not available") \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages pytesseract pdf2image pillow-heif openpyxl xlsxwriter
COPY addons/alwabrn_warehouse /mnt/extra-addons/alwabrn_warehouse
COPY deploy/seed /opt/alwabrn-seed
COPY deploy/entrypoint-alw.sh /entrypoint-alw.sh
RUN chmod +x /entrypoint-alw.sh
USER odoo
ENTRYPOINT ["/entrypoint-alw.sh"]
