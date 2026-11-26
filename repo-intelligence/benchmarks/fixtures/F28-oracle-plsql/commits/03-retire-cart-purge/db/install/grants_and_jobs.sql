-- Order desk deployment: the CRM link, synonyms, grants and scheduled jobs.
SET DEFINE OFF
PROMPT Creating the CRM database link and synonyms

CREATE DATABASE LINK crm CONNECT TO CURRENT_USER USING 'crm_service';
CREATE OR REPLACE SYNONYM crm_customers FOR customers@crm;

PROMPT Granting the order desk roles
GRANT EXECUTE ON order_api TO order_desk_role;
GRANT SELECT, INSERT, UPDATE ON orders TO order_desk_role;
GRANT SELECT ON order_events TO auditor_role;
GRANT order_desk_role TO desk_app;
GRANT CREATE SESSION TO desk_app;

PROMPT Scheduling jobs
BEGIN
  DBMS_SCHEDULER.CREATE_JOB(
    job_name        => 'NIGHTLY_CUSTOMER_SYNC',
    job_type        => 'STORED_PROCEDURE',
    job_action      => 'ORDER_API.LOAD_REMOTE_CUSTOMERS',
    repeat_interval => 'FREQ=DAILY;BYHOUR=2',
    enabled         => TRUE);
END;
/

DECLARE
  v_job BINARY_INTEGER;
BEGIN
  DBMS_JOB.SUBMIT(v_job, 'order_api.archive_orders(ADD_MONTHS(SYSDATE, -12));', SYSDATE, 'TRUNC(SYSDATE) + 1');
  COMMIT;
END;
/
