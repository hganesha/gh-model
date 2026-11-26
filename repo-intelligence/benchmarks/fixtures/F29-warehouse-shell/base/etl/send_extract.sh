#!/bin/bash
# Export active customers from Teradata and send them to the partner.
SCRIPT_DIR="$(dirname "$0")"
OUT=/data/out/active_customers.dat

fexp < "$SCRIPT_DIR/../teradata/active_customers.fexp"
if [ $? -ne 0 ]; then
  echo "export failed" >&2
  exit 2
fi

scp "$OUT" etl@sftp.partner.example:/inbound/active_customers.dat

direct <<EOF
submit maxdelay=unlimited
copy01 process snode=PARTNER_NODE
  copy from (file=/data/out/active_customers.dat pnode)
       to (file=/inbound/active_customers.dat snode disp=new)
pend;
EOF

psql -h reports -d analytics -c "INSERT INTO audit.extract_log (extract, sent_at) VALUES ('active_customers', now())"

echo "sent $OUT" | mailx -s "extract sent" etl-team@example.com
