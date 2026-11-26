# Order, payroll and finance batch

Batch work spread over six schedulers while it is moved to one: the scripts and JCL
members in this repository, and under `.rkp/inputs/scheduler/` what each scheduler's
export says about when they run and what they wait for.

| Export | Scheduler | Runs |
|---|---|---|
| `controlm/orders.xml` | Control-M | the order scripts, and payroll JCL on z/OS |
| `autosys/reports.jil` | AutoSys | nightly reports, a CICS refresh, a payroll JCL member |
| `tws/finance.txt` | IBM Workload Scheduler | the finance close streams |
| `ca7/payroll.lst` | CA-7 | the payroll jobs (an `LJOB` listing) |
| `cron/batch.crontab` | cron | housekeeping on the batch host |
| `windows/NightlyInvoices.xml` | Windows Task Scheduler | the invoice run |
