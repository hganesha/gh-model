create procedure dbo.settle_trades
    @trade_date datetime
as
begin
    declare @open int
    select @open = count(*) from positions holdlock where qty <> 0
    if @open > 0
    begin
        insert into settlements (desk, symbol, qty, settled_on)
            select desk, symbol, qty, @trade_date from positions where qty <> 0
        delete from positions where qty = 0
    end
    else
        print "nothing to settle"
    exec dbo.post_trades @trade_date, 'ALL', @open output
    return 0
end
go
