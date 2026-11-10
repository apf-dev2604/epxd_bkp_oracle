-- Run on the intended PHCAFE source in the SAME AWS account; all statements read-only.
SET LINESIZE 220 PAGESIZE 500 TRIMSPOOL ON
SPOOL phcafe_oracle_inventory.txt
PROMPT === DATABASE ===
SELECT name,dbid,open_mode,database_role,controlfile_type,log_mode,checkpoint_change# FROM v$database;
PROMPT === INSTANCE ===
SELECT instance_name,status,version FROM v$instance;
PROMPT === DATAFILE HEALTH ===
SELECT file#,checkpoint_change#,fuzzy,recover,error FROM v$datafile_header ORDER BY file#;
PROMPT === RECOVERY FILES ===
SELECT * FROM v$recover_file;
PROMPT === DATAFILE PATHS ===
SELECT file#,name FROM v$datafile ORDER BY file#;
PROMPT === CONTROLFILES ===
SELECT name FROM v$controlfile;
PROMPT === REDO FILE PATHS (NOT necessarily present) ===
SELECT group#,member,status FROM v$logfile ORDER BY group#;
PROMPT === TZ ===
SELECT filename,version FROM v$timezone_file;
SELECT dbtimezone FROM dual;
SELECT property_name,property_value FROM database_properties WHERE property_name LIKE 'DST_%';
PROMPT === ORACLE SQL PATCH ===
SELECT patch_id,action,status,action_time,description FROM dba_registry_sqlpatch ORDER BY action_time;
SPOOL OFF
