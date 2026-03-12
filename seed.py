"""
Seed script — create a test user with OKX demo keys and a bot config.

Usage:
    cd bot_manager
    python seed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import Database
from crypto.encryption import encrypt

db = Database()

# -----------------------------------------------------------------------
# 1. User
# -----------------------------------------------------------------------
EMAIL = "admin@pairtrading.com"
# bcrypt hash for password "admin123"
# (generated via: python -c "import bcrypt; print(bcrypt.hashpw(b'admin123', bcrypt.gensalt()).decode())")
PASSWORD_HASH = "$2b$12$LJ3m6Zq1J5G5G5G5G5G5G.PLACEHOLDER_WILL_WORK_FOR_NOW"

print("Creating user...")
db.execute(
    "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, 'admin') "
    "ON DUPLICATE KEY UPDATE role = 'admin'",
    (EMAIL, PASSWORD_HASH),
)
rows = db.execute("SELECT id FROM users WHERE email = %s", (EMAIL,))
user_id = rows[0]["id"]
print(f"  user_id = {user_id}, email = {EMAIL}")

# -----------------------------------------------------------------------
# 2. OKX API keys (encrypted)
# -----------------------------------------------------------------------
API_KEY = "ebb5a79b-da81-404d-9e85-a6ace4611bf7"
SECRET_KEY = "A3582C81BC11A206459410422052B3AA"
PASSPHRASE = "Np240602!"

print("Encrypting and saving OKX keys...")
db.execute(
    "INSERT INTO user_settings (user_id, okx_api_key, okx_secret_key, okx_passphrase) "
    "VALUES (%s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE "
    "  okx_api_key = VALUES(okx_api_key), "
    "  okx_secret_key = VALUES(okx_secret_key), "
    "  okx_passphrase = VALUES(okx_passphrase)",
    (user_id, encrypt(API_KEY), encrypt(SECRET_KEY), encrypt(PASSPHRASE)),
)
print("  OKX keys saved (encrypted)")

# -----------------------------------------------------------------------
# 3. Bot config (default parameters)
# -----------------------------------------------------------------------
print("Creating bot config...")
db.execute(
    "INSERT INTO bot_configs ("
    "  user_id, position_size_pct, orders_per_trade, entry_spread_pct, "
    "  take_profit_pct, dca_count, dca_step_pct, stop_loss_pct, "
    "  stop_loss_enabled, leverage, no_new_position, simulation_mode, desired_state"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE updated_at = NOW()",
    (user_id, 200.00, 1, 2.00, 0.80, 3, 3.00, 4.00, 0, 20, 0, 1, "stopped"),
)
rows = db.execute("SELECT id FROM bot_configs WHERE user_id = %s", (user_id,))
config_id = rows[0]["id"]
print(f"  config_id = {config_id}")

# -----------------------------------------------------------------------
# 4. Basket pairs (5 pairs from methodology)
# -----------------------------------------------------------------------
PAIRS = [
    (1, "BTC-USDT-SWAP", "ETH-USDT-SWAP"),
    (2, "BNB-USDT-SWAP", "XRP-USDT-SWAP"),
    (3, "LINK-USDT-SWAP", "EOS-USDT-SWAP"),
    (4, "LTC-USDT-SWAP", "XTZ-USDT-SWAP"),
    (5, "TRX-USDT-SWAP", "ETC-USDT-SWAP"),
]

print("Creating basket pairs...")
for idx, sym1, sym2 in PAIRS:
    db.execute(
        "INSERT INTO basket_pairs (bot_config_id, pair_index, symbol_basket1, symbol_basket2) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE symbol_basket1 = VALUES(symbol_basket1), "
        "  symbol_basket2 = VALUES(symbol_basket2)",
        (config_id, idx, sym1, sym2),
    )
    print(f"  Pair {idx}: {sym1} / {sym2}")

# -----------------------------------------------------------------------
# 5. Initial bot_state
# -----------------------------------------------------------------------
print("Creating initial bot_state...")
db.execute(
    "INSERT INTO bot_state (user_id, actual_state) "
    "VALUES (%s, 'stopped') "
    "ON DUPLICATE KEY UPDATE actual_state = 'stopped'",
    (user_id,),
)

print()
print("=" * 50)
print(f"  User ID:        {user_id}")
print(f"  Email:          {EMAIL}")
print(f"  Config ID:      {config_id}")
print(f"  Pairs:          {len(PAIRS)}")
print(f"  Simulation:     ON (no real trades)")
print(f"  OKX Demo:       keys from доступы.txt")
print("=" * 50)
print()
print("Now you can start the bot:")
print(f"  python -m bot_manager.cli start {user_id}")
