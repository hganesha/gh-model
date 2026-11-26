/* Exported from the SAS server's autoexec.sas: library allocations for the sales jobs. */
options fmtsearch=(fmtlib.formats work library) mprint nosymbolgen;

libname raw    oracle path=salesdb schema=extract user=rpt_batch password="{SAS002}1D57933958C580064BD3DCA81A33DFB2";
libname mart   base "/sasdata/sales/mart";
libname fmtlib base "/sasdata/sales/formats";

filename rptdir "/sasdata/sales/reports";

%let rpt_env = %sysget(RPT_ENV);
