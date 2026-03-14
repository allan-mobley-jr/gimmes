"""SQL DDL for paper trading tables.

These are separate from the main trades/positions/snapshots tables
to avoid contamination between paper and real trading data.
"""

PAPER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS paper_balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL,
    starting_balance REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'buy',
    side TEXT NOT NULL DEFAULT 'yes',
    count INTEGER NOT NULL,
    remaining_count INTEGER NOT NULL,
    yes_price INTEGER NOT NULL DEFAULT 0,
    no_price INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'resting',
    post_only INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_fills (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'buy',
    side TEXT NOT NULL DEFAULT 'yes',
    count INTEGER NOT NULL,
    yes_price INTEGER NOT NULL DEFAULT 0,
    no_price INTEGER NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    is_taker INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (order_id) REFERENCES paper_orders(order_id)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    ticker TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'yes',
    count INTEGER NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL DEFAULT 0,
    cost_basis REAL NOT NULL DEFAULT 0,
    market_price REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, side)
);
"""
