# Sales analytics

Monthly sales reporting in SAS 9.4. `autoexec.sas` (exported to `.rkp/inputs/sas/`)
allocates the libraries; the summary program cleans the raw extract, builds the regional
summary, prints it to PDF and exports a CSV; a stored process lets analysts view one region.
