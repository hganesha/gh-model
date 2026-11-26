--#SET TERMINATOR @
CREATE OR REPLACE PROCEDURE fin.refresh_balances (IN p_run_date DATE, OUT p_rows INTEGER)
  SPECIFIC refresh_balances
  DYNAMIC RESULT SETS 1
  LANGUAGE SQL
  MODIFIES SQL DATA
P1: BEGIN
  DECLARE v_count INTEGER DEFAULT 0;
  DECLARE v_status CHAR(1);
  DECLARE SQLSTATE CHAR(5) DEFAULT '00000';
  DECLARE c_out CURSOR WITH RETURN FOR
    SELECT account_id, balance FROM fin.balances WHERE as_of = p_run_date;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
      ROLLBACK;
      SIGNAL SQLSTATE '75001' SET MESSAGE_TEXT = 'refresh failed';
    END;

  DELETE FROM fin.balances WHERE as_of = p_run_date;

  INSERT INTO fin.balances (account_id, as_of, balance)
    SELECT t.account_id, p_run_date, SUM(t.amount)
    FROM fin.transactions t
    WHERE t.posted_on <= p_run_date
    GROUP BY t.account_id;

  GET DIAGNOSTICS v_count = ROW_COUNT;
  SET p_rows = v_count;

  IF v_count = 0 THEN
    SET v_status = 'E';
  ELSEIF v_count < 100 THEN
    SET v_status = 'W';
  ELSE
    SET v_status = 'O';
  END IF;

  SELECT MAX(as_of) INTO v_last FROM fin.balances;

  FOR r AS c1 CURSOR FOR SELECT account_id FROM fin.accounts WHERE closed = 'Y' DO
    CALL fin.archive_account(r.account_id);
  END FOR;

  WHILE v_count > 0 DO
    SET v_count = v_count - 1;
  END WHILE;

  CASE v_status
    WHEN 'E' THEN CALL fin.notify('empty');
    ELSE SET v_status = 'O';
  END CASE;

  EXECUTE IMMEDIATE 'DELETE FROM fin.balance_log WHERE as_of < CURRENT DATE - 30 DAYS';
  EXECUTE IMMEDIATE v_sql;

  INSERT INTO fin.refresh_log (run_date, rows_loaded, status) VALUES (p_run_date, v_count, v_status);
  COMMIT;
  OPEN c_out;
END P1
@

CREATE TRIGGER fin.trg_balance_audit
  AFTER UPDATE OF balance ON fin.balances
  REFERENCING OLD AS o NEW AS n
  FOR EACH ROW MODE DB2SQL
  WHEN (n.balance < 0)
BEGIN ATOMIC
  INSERT INTO fin.balance_audit (account_id, old_balance, new_balance) VALUES (n.account_id, o.balance, n.balance);
END
@
