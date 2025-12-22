-- sehwag_queries.sql
-- Useful SELECT queries for the Sehwag strategy database (db/sehwag.db)
-- Replace :param placeholders with actual values or use your DB client parameter binding.

-- ============================
-- 1) Basic table dumps
-- ============================
-- All sessions
SELECT * FROM sehwag_sessions;

-- All positions
SELECT * FROM sehwag_positions;

-- All position snapshots
SELECT * FROM sehwag_position_snapshots;

-- All orders
SELECT * FROM sehwag_orders;

-- All events
SELECT * FROM sehwag_events;


-- ============================
-- 2) Helpful single-table queries
-- ============================
-- Sessions: human-friendly columns, newest first
SELECT id, session_id, index_symbol, expiry_date, status, net_pnl, total_orders_placed, total_orders_executed, total_legs_opened, total_legs_closed, start_time, end_time, created_at
FROM sehwag_sessions
ORDER BY session_date DESC;

-- Get session by session_id (string)
-- :session_id -> replace with actual session id
SELECT * FROM sehwag_sessions WHERE session_id = :session_id;

-- Sessions by index and date range
SELECT * FROM sehwag_sessions
WHERE index_symbol = :index_symbol
  AND session_date BETWEEN :from_ts AND :to_ts
ORDER BY session_date DESC;

-- Positions for a given session (use DB id)
SELECT * FROM sehwag_positions WHERE session_id = :session_db_id ORDER BY leg_number, entry_time;

-- Active positions
SELECT * FROM sehwag_positions WHERE status = 'ACTIVE' ORDER BY entry_time DESC;

-- Position snapshots for a given position
SELECT * FROM sehwag_position_snapshots WHERE position_id = :position_id ORDER BY timestamp;

-- Orders for a given session
SELECT * FROM sehwag_orders WHERE session_id = :session_db_id ORDER BY order_time;

-- Orders for a given broker order id
SELECT * FROM sehwag_orders WHERE order_id = :broker_order_id;

-- Events for a session
SELECT * FROM sehwag_events WHERE session_id = :session_db_id ORDER BY event_time;


-- ============================
-- 3) Useful JOINs and combined views
-- ============================
-- A) Session summary with positions count
SELECT s.id AS db_id, s.session_id, s.index_symbol, s.expiry_date, s.status,
       COUNT(p.id) AS positions_count
FROM sehwag_sessions s
LEFT JOIN sehwag_positions p ON p.session_id = s.id
WHERE s.session_id = :session_id
GROUP BY s.id, s.session_id, s.index_symbol, s.expiry_date, s.status;

-- B) Positions + orders count per position for a session
SELECT p.*, COUNT(o.id) AS orders_count
FROM sehwag_positions p
LEFT JOIN sehwag_orders o ON o.session_id = p.session_id AND o.leg_number = p.leg_number
WHERE p.session_id = :session_db_id
GROUP BY p.id
ORDER BY p.leg_number, p.entry_time;

-- C) Full timeline (events + orders) for a session ordered by time
-- Note: SQLite uses COALESCE/IFNULL; adjust for other DBs as needed
SELECT 'EVENT' AS type, e.event_time AS time, e.event_type AS action, e.leg_number, e.symbol, e.description AS details
FROM sehwag_events e
WHERE e.session_id = :session_db_id
UNION ALL
SELECT 'ORDER' AS type, o.order_time AS time, o.order_type AS action, o.leg_number, o.symbol,
       'status=' || COALESCE(o.status,'') || '; exec_price=' || COALESCE(CAST(o.execution_price AS TEXT),'NULL') AS details
FROM sehwag_orders o
WHERE o.session_id = :session_db_id
ORDER BY time;

-- D) Executed orders for a session
SELECT id, order_id, symbol, side, quantity, execution_price, execution_time, status
FROM sehwag_orders
WHERE session_id = :session_db_id AND status IN ('EXECUTED','ACCEPTED')
ORDER BY execution_time;


-- ============================
-- 4) Reporting / Aggregates
-- ============================
-- 1) Session summaries (per session)
SELECT session_id, index_symbol, expiry_date, status, total_orders_placed, total_orders_executed, net_pnl, total_legs_opened, total_legs_closed
FROM sehwag_sessions
ORDER BY session_date DESC;

-- 2) Daily performance aggregated by index (for a given expiry)
SELECT s.index_symbol, COUNT(*) AS sessions, AVG(s.net_pnl) AS avg_pnl, SUM(s.net_pnl) AS total_pnl
FROM sehwag_sessions s
WHERE s.expiry_date = :expiry_date
GROUP BY s.index_symbol
ORDER BY total_pnl DESC;

-- 3) Performance by date range (per index + day)
SELECT s.index_symbol, DATE(s.session_date) AS session_day, COUNT(*) AS sessions, SUM(s.net_pnl) AS total_pnl, AVG(s.net_pnl) AS avg_pnl
FROM sehwag_sessions s
WHERE s.session_date BETWEEN :from_ts AND :to_ts
GROUP BY s.index_symbol, DATE(s.session_date)
ORDER BY session_day, total_pnl DESC;

-- 4) Top N winning sessions overall
SELECT session_id, index_symbol, expiry_date, net_pnl
FROM sehwag_sessions
ORDER BY net_pnl DESC
LIMIT :n;

-- 5) Count open positions
SELECT COUNT(*) AS open_positions FROM sehwag_positions WHERE status = 'ACTIVE';

-- 6) PnL by session (sum realized per session via positions)
SELECT p.session_id, SUM(COALESCE(p.realized_pnl,0.0)) AS session_realized_pnl
FROM sehwag_positions p
GROUP BY p.session_id
ORDER BY session_realized_pnl DESC;


-- ============================
-- 5) Convenience & lookups
-- ============================
-- Map session string -> DB id
SELECT id FROM sehwag_sessions WHERE session_id = :session_id;

-- Latest session for an index
SELECT * FROM sehwag_sessions WHERE index_symbol = :index_symbol ORDER BY session_date DESC LIMIT 1;

-- Orders for a symbol (limit)
SELECT * FROM sehwag_orders WHERE symbol = :symbol ORDER BY order_time DESC LIMIT :n;

-- Recent events across strategies (last N)
SELECT * FROM sehwag_events ORDER BY event_time DESC LIMIT :n;

-- Positions opened per leg (counts)
SELECT leg_number, COUNT(*) AS cnt, AVG(entry_price) AS avg_entry FROM sehwag_positions GROUP BY leg_number ORDER BY leg_number;

-- Sessions with manual notes
SELECT session_id, index_symbol, notes FROM sehwag_sessions WHERE notes IS NOT NULL AND notes <> '' ORDER BY session_date DESC;


-- ============================
-- 6) Optional: views to simplify reporting (SQLite syntax)
-- Run these once to create views for convenience
-- ============================
-- Session timeline view (events + orders)
CREATE VIEW IF NOT EXISTS vw_session_timeline AS
SELECT 'EVENT' AS type, session_id, event_time AS time, event_type AS action, leg_number, symbol, description AS details FROM sehwag_events
UNION ALL
SELECT 'ORDER' AS type, session_id, order_time AS time, order_type AS action, leg_number, symbol, status AS details FROM sehwag_orders;

-- Example usage: SELECT * FROM vw_session_timeline WHERE session_id = :session_db_id ORDER BY time;


-- ============================
-- 7) Example sqlite3 commands
-- ============================
-- Open DB and run a query (Windows PowerShell example):
-- sqlite3 "db/sehwag.db" "SELECT session_id, net_pnl FROM sehwag_sessions ORDER BY session_date DESC LIMIT 10;"

-- Export a session timeline to CSV (sqlite3):
-- sqlite3 -header -csv "db/sehwag.db" "SELECT * FROM vw_session_timeline WHERE session_id = 'your-session-id' ORDER BY time;" > session_timeline.csv

-- ============================
-- 8) CLEANUP / PURGE STATEMENTS (USE WITH CAUTION)
-- ============================
-- IMPORTANT: Always back up `db/sehwag.db` before running destructive statements.
-- Example backup (PowerShell):
-- Copy-Item -Path "db\sehwag.db" -Destination "db\sehwag.db.bak_$(Get-Date -Format yyyyMMdd_HHmmss)"

-- A) Purge by date (delete ALL data for sessions older than :before_date)
-- Replace :before_date with an ISO timestamp or use DATETIME() functions, e.g. '2025-11-01'
BEGIN TRANSACTION;

-- Create a small temp table with session ids to delete
CREATE TEMP TABLE tmp_sessions_to_delete(id INTEGER PRIMARY KEY);
INSERT INTO tmp_sessions_to_delete(id)
  SELECT id FROM sehwag_sessions WHERE session_date < :before_date;

-- Delete child records in correct order
DELETE FROM sehwag_position_snapshots
 WHERE position_id IN (SELECT id FROM sehwag_positions WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete));

DELETE FROM sehwag_orders WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete);
DELETE FROM sehwag_events WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete);
DELETE FROM sehwag_positions WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete);

-- Finally delete sessions
DELETE FROM sehwag_sessions WHERE id IN (SELECT id FROM tmp_sessions_to_delete);

-- Drop the temp table and commit
DROP TABLE IF EXISTS tmp_sessions_to_delete;
COMMIT;

-- VACUUM to reclaim space (runs faster on an offline DB)
-- sqlite3 "db/sehwag.db" "VACUUM;"


-- B) Purge keeping last N sessions per index (keep :keep sessions per :index_symbol)
-- This deletes ALL sessions for the given index except the most recent :keep sessions.
-- Steps: compute sessions to keep, then delete others (child rows first)
BEGIN TRANSACTION;

-- Create a temp table holding ids to keep
CREATE TEMP TABLE tmp_sessions_keep(id INTEGER PRIMARY KEY);
INSERT INTO tmp_sessions_keep(id)
  SELECT id FROM sehwag_sessions
  WHERE index_symbol = :index_symbol
  ORDER BY session_date DESC
  LIMIT :keep;

-- Now delete sessions for the index that are NOT in the keep list
CREATE TEMP TABLE tmp_sessions_to_delete2(id INTEGER PRIMARY KEY);
INSERT INTO tmp_sessions_to_delete2(id)
  SELECT id FROM sehwag_sessions
  WHERE index_symbol = :index_symbol
    AND id NOT IN (SELECT id FROM tmp_sessions_keep);

-- Delete dependent rows
DELETE FROM sehwag_position_snapshots
 WHERE position_id IN (SELECT id FROM sehwag_positions WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete2));

DELETE FROM sehwag_orders WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete2);
DELETE FROM sehwag_events WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete2);
DELETE FROM sehwag_positions WHERE session_id IN (SELECT id FROM tmp_sessions_to_delete2);
DELETE FROM sehwag_sessions WHERE id IN (SELECT id FROM tmp_sessions_to_delete2);

DROP TABLE IF EXISTS tmp_sessions_keep;
DROP TABLE IF EXISTS tmp_sessions_to_delete2;
COMMIT;

-- C) Delete a single session by session_id (string)
-- Safe procedure: find DB id, then delete children then session
BEGIN TRANSACTION;
-- Replace :session_id_str
CREATE TEMP TABLE tmp_single_session(id INTEGER PRIMARY KEY);
INSERT INTO tmp_single_session(id)
  SELECT id FROM sehwag_sessions WHERE session_id = :session_id_str LIMIT 1;

DELETE FROM sehwag_position_snapshots
 WHERE position_id IN (SELECT id FROM sehwag_positions WHERE session_id IN (SELECT id FROM tmp_single_session));
DELETE FROM sehwag_orders WHERE session_id IN (SELECT id FROM tmp_single_session);
DELETE FROM sehwag_events WHERE session_id IN (SELECT id FROM tmp_single_session);
DELETE FROM sehwag_positions WHERE session_id IN (SELECT id FROM tmp_single_session);
DELETE FROM sehwag_sessions WHERE id IN (SELECT id FROM tmp_single_session);
DROP TABLE IF EXISTS tmp_single_session;
COMMIT;

-- D) Truncate entire schema (DANGER: destructive) - useful for resets/test/dev only
-- Backup first!
BEGIN TRANSACTION;
DELETE FROM sehwag_position_snapshots;
DELETE FROM sehwag_orders;
DELETE FROM sehwag_events;
DELETE FROM sehwag_positions;
DELETE FROM sehwag_sessions;
COMMIT;
-- Run VACUUM afterwards to keep DB file small
-- sqlite3 "db/sehwag.db" "VACUUM;"

-- ============================
-- 9) Maintenance & optimization
-- ============================
-- Rebuild indexes and analyze for query planner
-- Run ANALYZE to update statistics
-- sqlite3 "db/sehwag.db" "ANALYZE;"

-- Optionally rebuild indexes if you suspect corruption (rare)
-- sqlite3 "db/sehwag.db" "REINDEX;"

-- ============================
-- Safety notes
-- ============================
-- 1) Always backup before running delete/truncate operations
--    PowerShell example:
--    Copy-Item -Path "db\sehwag.db" -Destination "db\sehwag.db.bak_$(Get-Date -Format yyyyMMdd_HHmmss)"
-- 2) Prefer testing cleanup commands on a copy of the DB first
-- 3) Consider adding an archival flow instead of deleting if you need historical data

-- End of cleanup section

-- ============================
-- End of file
-- Save this file and run queries with your DB client or sqlite3 CLI.

