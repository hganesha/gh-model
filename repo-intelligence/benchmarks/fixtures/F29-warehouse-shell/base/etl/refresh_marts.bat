@echo off
rem Refresh the SQL Server marts and run the export package.
setlocal
set DB=marts
if "%1"=="" goto usage
sqlcmd -S %MART_SERVER% -d %DB% -b -Q "EXEC dbo.refresh_marts @day='%1'"
if errorlevel 1 goto fail
sqlcmd -S %MART_SERVER% -d %DB% -i %~dp0sql\rebuild_indexes.sql
dtexec /F "%~dp0packages\ExportMarts.dtsx"
goto :eof
:usage
echo usage: refresh_marts.bat day
exit /b 2
:fail
echo refresh failed
exit /b 1
