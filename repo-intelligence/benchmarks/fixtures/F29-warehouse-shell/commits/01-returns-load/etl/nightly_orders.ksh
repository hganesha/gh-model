#!/bin/ksh
# Nightly orders load: fetch the partner file, stage it in Oracle, load Teradata, score in SAS.
set -e
RUN_DATE=${1:-$(date +%Y%m%d)}
SCRIPT_DIR=$(dirname "$0")
STAGE=stg
DB_PASSWORD="fixture-not-a-real-password-0029"
LOG=/var/log/etl/nightly_orders.log

log() {
  echo "$(date) $*" >> $LOG
}

log "start $RUN_DATE"

# The partner drops the day's file on its SFTP server.
sftp -b - etl@files.partner.example <<EOF
cd /outbound
get orders.csv /data/in/orders.csv
bye
EOF

if [ ! -f /data/in/orders.csv ]; then
  log "no input"
  exit 1
fi

sqlplus -s etl/$DB_PASSWORD@WHPROD <<EOF
WHENEVER SQLERROR EXIT 1
INSERT INTO $STAGE.orders_hist SELECT * FROM stg.orders;
DELETE FROM stg.orders WHERE load_date < SYSDATE - 30;
EXEC stg.refresh_order_stats;
COMMIT;
EXIT
EOF

fastload < $SCRIPT_DIR/../teradata/orders.fastload
fastload < $SCRIPT_DIR/../teradata/returns.fastload

bteq <<EOF
.LOGON tdprod/etl,$DB_PASSWORD;
INSERT INTO mart.orders_daily (order_day, orders, amount)
SELECT order_date, COUNT(*), SUM(amount) FROM stg.orders GROUP BY order_date;
.QUIT
EOF

sas -sysin sas/score_orders.sas -log /var/log/etl/score.log
rc=$?
case $rc in
  0) log "scored" ;;
  *) log "scoring failed"; exit $rc ;;
esac

dsjob -run -jobstatus DWPROJ LoadCustomers
pmcmd startworkflow -sv INT_SVC -d DOM_ETL -f SALES wf_load_sales
spark-submit --class com.example.Enrich jobs/enrich.jar "$RUN_DATE"

for f in /data/in/*.csv; do
  mv "$f" /data/archive/
done
log "done"
