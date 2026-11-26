create procedure dbo.post_trades
    @trade_date datetime,
    @desk varchar(10) = 'ALL',
    @posted int output
as
begin
    declare @count int, @msg varchar(255)
    set chained off
    select @count = count(*) from trades holdlock where trade_date = @trade_date
    if @count = 0
    begin
        print "no trades for %1!", @trade_date
        return 1
    end
    begin tran
    insert into positions (desk, symbol, qty)
        select desk, symbol, sum(qty) from trades
        where trade_date = @trade_date and (@desk = 'ALL' or desk = @desk)
        group by desk, symbol
    if @@error != 0
    begin
        rollback tran
        raiserror 20001 "posting failed"
        return 2
    end
    update trades set posted = 1 where trade_date = @trade_date
    delete from trade_queue where trade_date = @trade_date
    exec @posted = dbo.count_posted @trade_date
    exec audit_db..log_event 'post_trades', @count
    commit tran
    while @count > 0
        select @count = @count - 1
    select symbol, qty from positions where desk = @desk
end
go
exec sp_procxmode 'dbo.post_trades', 'anymode'
go
create trigger dbo.trg_trades_ins on dbo.trades for insert, update as
    insert into trade_audit (trade_id, changed_at) select trade_id, getdate() from inserted
go
