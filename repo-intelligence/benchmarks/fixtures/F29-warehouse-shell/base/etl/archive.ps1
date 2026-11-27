param([string]$Day = (Get-Date -Format yyyyMMdd))
# Archive the day's orders into the archive mart.
$Server = "sql01.corp.example"
$Query = @"
INSERT INTO mart.orders_archive (order_id, order_date, amount)
SELECT order_id, order_date, amount FROM stg.orders WHERE order_date < '$Day'
"@
Invoke-Sqlcmd -ServerInstance $Server -Database "marts" -Query $Query
Write-Host "archived"
