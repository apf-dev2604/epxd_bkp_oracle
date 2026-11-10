-- Run ONLY after the database is successfully opened on the isolated RESTORE TEST VM.
-- Read-only validation. Does NOT perform recovery, shutdown, or RESETLOGS.
SET LINESIZE 220 PAGESIZE 500 TRIMSPOOL ON
SPOOL phcafe_dr_restore_validation.log
PROMPT ==== TARGET DATABASE / OPEN MODE ====
SELECT name,dbid,open_mode,database_role,controlfile_type,log_mode,resetlogs_change# FROM v$database;
PROMPT ==== DATAFILE HEADER STATUS ====
SELECT file#,checkpoint_change#,fuzzy,recover,error FROM v$datafile_header ORDER BY file#;
PROMPT ==== RECOVERY NEEDS ====
SELECT * FROM v$recover_file;
PROMPT ==== ONLINE DATAFILES ====
SELECT file#,status,name FROM v$datafile ORDER BY file#;
PROMPT ==== TABLESPACES ====
SELECT tablespace_name,status,contents FROM dba_tablespaces ORDER BY tablespace_name;
PROMPT ==== TIMEZONE FILE / DATABASE ====
SELECT filename,version FROM v$timezone_file;
SELECT dbtimezone FROM dual;
PROMPT ==== CHARACTER SET ====
SELECT parameter,value FROM nls_database_parameters WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET');
PROMPT ==== INVALID OBJECTS BY OWNER ====
SELECT owner,COUNT(*) invalid_objects FROM dba_objects WHERE status='INVALID' GROUP BY owner ORDER BY owner;
SPOOL OFF
