"""Centralised SQL queries used by Manager and Worker."""

# ---------------------------------------------------------------------------
# bot_commands (audit log — manager writes results, backend inserts commands)
# ---------------------------------------------------------------------------
INSERT_COMMAND = """
    INSERT INTO bot_commands (user_id, command, status)
    VALUES (%s, %s, 'processing')
"""
FINISH_COMMAND = """
    UPDATE bot_commands SET status = 'done', processed_at = NOW()
    WHERE id = %s
"""
FAIL_COMMAND = """
    UPDATE bot_commands SET status = 'failed',
        error_message = %s, processed_at = NOW()
    WHERE id = %s
"""

# ---------------------------------------------------------------------------
# bot_configs
# ---------------------------------------------------------------------------
SELECT_CONFIG = """
    SELECT * FROM bot_configs WHERE user_id = %s
"""
SELECT_RUNNING_BOTS = """
    SELECT user_id FROM bot_configs WHERE desired_state = 'running'
"""
SET_DESIRED_STATE = """
    UPDATE bot_configs SET desired_state = %s WHERE user_id = %s
"""

# ---------------------------------------------------------------------------
# user_settings (API keys)
# ---------------------------------------------------------------------------
SELECT_USER_KEYS = """
    SELECT okx_api_key, okx_secret_key, okx_passphrase
    FROM user_settings WHERE user_id = %s
"""

# ---------------------------------------------------------------------------
# bot_state
# ---------------------------------------------------------------------------
SELECT_STATE = """
    SELECT * FROM bot_state WHERE user_id = %s
"""
UPSERT_STATE_RUNNING = """
    INSERT INTO bot_state (user_id, actual_state, worker_pid, running_since)
    VALUES (%s, 'starting', %s, NOW())
    ON DUPLICATE KEY UPDATE
        actual_state = 'starting',
        worker_pid = %s,
        running_since = NOW()
"""
SET_STATE_STOPPED = """
    UPDATE bot_state SET actual_state = 'stopped', worker_pid = NULL
    WHERE user_id = %s
"""
SET_STATE_ERROR = """
    UPDATE bot_state SET actual_state = 'error'
    WHERE user_id = %s
"""
UPDATE_STATE_FULL = """
    UPDATE bot_state SET
        actual_state        = %s,
        current_spread_pct  = %s,
        buy_basket          = %s,
        sell_basket          = %s,
        pnl_long_pct        = %s,
        pnl_short_pct       = %s,
        pnl_total_pct       = %s,
        pnl_total_usdt      = %s,
        balance_usdt        = %s,
        available_usdt      = %s,
        okx_ping_ms         = %s,
        connection_status   = %s,
        position_open       = %s,
        entry_spread_pct    = %s,
        dca_count_current   = %s,
        long_basket         = %s,
        short_basket        = %s,
        reference_prices    = %s,
        positions_data      = %s,
        quotes_snapshot     = %s,
        worker_pid          = %s
    WHERE user_id = %s
"""
SAVE_STATE_ON_SHUTDOWN = """
    UPDATE bot_state SET
        reference_prices  = %s,
        positions_data    = %s,
        entry_spread_pct  = %s,
        dca_count_current = %s,
        position_open     = %s,
        long_basket       = %s,
        short_basket      = %s,
        actual_state      = 'stopped',
        worker_pid        = NULL
    WHERE user_id = %s
"""

# ---------------------------------------------------------------------------
# basket_pairs
# ---------------------------------------------------------------------------
SELECT_BASKET_PAIRS = """
    SELECT pair_index, symbol_basket1, symbol_basket2
    FROM basket_pairs
    WHERE bot_config_id = %s
    ORDER BY pair_index
"""

# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------
INSERT_TRADE = """
    INSERT INTO trades (
        user_id, opened_at, closed_at, duration_seconds,
        entry_spread_pct, exit_spread_pct,
        long_basket, short_basket,
        pnl_pct, pnl_usdt, total_volume_usdt,
        dca_count, close_reason, pairs_detail, total_commission_usdt
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""

# ---------------------------------------------------------------------------
# spread_log
# ---------------------------------------------------------------------------
INSERT_SPREAD_LOG = """
    INSERT INTO spread_log (user_id, spread_pct, r_basket1_pct, r_basket2_pct)
    VALUES (%s, %s, %s, %s)
"""

# ---------------------------------------------------------------------------
# events_log
# ---------------------------------------------------------------------------
INSERT_EVENT = """
    INSERT INTO events_log (user_id, level, message, details)
    VALUES (%s, %s, %s, %s)
"""
SELECT_EVENTS = """
    SELECT * FROM events_log
    WHERE user_id = %s
    ORDER BY created_at DESC
    LIMIT %s
"""

# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------
INSERT_NOTIFICATION = """
    INSERT INTO notifications (user_id, channel, message)
    VALUES (%s, 'telegram', %s)
"""

# ---------------------------------------------------------------------------
# manager_heartbeat
# ---------------------------------------------------------------------------
UPSERT_HEARTBEAT = """
    INSERT INTO manager_heartbeat (id, manager_pid, workers_count, updated_at)
    VALUES (1, %s, %s, NOW())
    ON DUPLICATE KEY UPDATE
        manager_pid = %s, workers_count = %s, updated_at = NOW()
"""
SELECT_HEARTBEAT = """
    SELECT * FROM manager_heartbeat WHERE id = 1
"""

# ---------------------------------------------------------------------------
# users (for validation)
# ---------------------------------------------------------------------------
SELECT_USER = """
    SELECT id, email, role, is_blocked FROM users WHERE id = %s
"""
