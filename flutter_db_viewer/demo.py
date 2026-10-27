"""A fake "shop" app database that keeps writing, for `flutterdb --demo`.

Mimics what a real offline-first app does on a device: a WAL-mode SQLite file
held open by the app, new orders arriving, statuses changing, a sync queue
draining. All names and data are made up.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path

PRODUCTS = [
    ("Basmati Rice 5kg", "Grocery", 640), ("Toor Dal 1kg", "Grocery", 165), ("Atta 10kg", "Grocery", 455),
    ("Mustard Oil 1L", "Grocery", 189), ("Ghee 500ml", "Dairy", 330), ("Paneer 200g", "Dairy", 95),
    ("Curd 400g", "Dairy", 45), ("Milk 1L", "Dairy", 66), ("Masala Chai 250g", "Beverages", 140),
    ("Filter Coffee 200g", "Beverages", 210), ("Mango Juice 1L", "Beverages", 110), ("Jaggery 1kg", "Grocery", 90),
    ("Turmeric 100g", "Spices", 38), ("Garam Masala 100g", "Spices", 72), ("Red Chilli 200g", "Spices", 64),
    ("Cumin 100g", "Spices", 55), ("Poha 500g", "Breakfast", 48), ("Oats 1kg", "Breakfast", 199),
    ("Rava 1kg", "Breakfast", 58), ("Biscuits Family Pack", "Snacks", 120), ("Namkeen 400g", "Snacks", 95),
    ("Peanuts 500g", "Snacks", 85), ("Dish Soap 500ml", "Home", 99), ("Detergent 1kg", "Home", 140),
    ("Toothpaste 150g", "Personal care", 92), ("Bath Soap x4", "Personal care", 160), ("Shampoo 340ml", "Personal care", 230),
    ("Onion 1kg", "Vegetables", 40), ("Potato 1kg", "Vegetables", 32), ("Tomato 1kg", "Vegetables", 36),
]
FIRST = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Saanvi", "Arjun", "Ira", "Vihaan", "Anaya",
         "Dev", "Kiara", "Ishaan", "Myra", "Reyansh", "Tara", "Aditya", "Zoya", "Neel", "Riya"]
LAST = ["Sharma", "Verma", "Iyer", "Khan", "Patel", "Reddy", "Das", "Nair", "Singh", "Gupta",
        "Joshi", "Menon", "Bose", "Kulkarni", "Chopra"]
CITIES = ["Pune", "Jaipur", "Kochi", "Lucknow", "Indore", "Bhopal", "Mysuru", "Nagpur", "Surat", "Patna"]
STATUSES = ["placed", "packed", "shipped", "delivered"]

SCHEMA = """
CREATE TABLE products (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT, price_inr INTEGER, in_stock INTEGER DEFAULT 1);
CREATE TABLE customers (
  id INTEGER PRIMARY KEY, uuid TEXT UNIQUE, name TEXT, phone TEXT, city TEXT, created_at TEXT);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY, uuid TEXT UNIQUE, customer_id INTEGER REFERENCES customers(id),
  status TEXT, total_inr INTEGER, notes TEXT, created_at TEXT, updated_at TEXT,
  sync_status TEXT NOT NULL DEFAULT 'dirty');
CREATE TABLE order_items (
  id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), product_id INTEGER,
  qty INTEGER, price_inr INTEGER);
CREATE TABLE sync_queue (
  id INTEGER PRIMARY KEY, entity TEXT, entity_uuid TEXT, op TEXT, payload TEXT,
  sync_status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, event TEXT, at TEXT);
"""


def _now(offset_s: float = 0) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_s))


class DemoApp:
    PACKAGE = "com.example.shop"
    DB = "shop_app.db"

    def __init__(self, directory: Path, seed: int = 7, tick: float = 2.5):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / self.DB
        self.tick = tick
        self.rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.con = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        self._seed()

    # setup ------------------------------------------------------------------

    def _seed(self) -> None:
        c, r = self.con, self.rng
        c.executescript(SCHEMA)
        c.execute("BEGIN")
        c.executemany(
            "INSERT INTO products(name, category, price_inr, in_stock) VALUES (?,?,?,?)",
            [(n, cat, p, int(r.random() > 0.1)) for n, cat, p in PRODUCTS],
        )
        for i in range(120):
            c.execute(
                "INSERT INTO customers(uuid, name, phone, city, created_at) VALUES (?,?,?,?,?)",
                (str(uuid.UUID(int=r.getrandbits(128))), f"{r.choice(FIRST)} {r.choice(LAST)}",
                 f"9{r.randint(100000000, 999999999)}", r.choice(CITIES), _now(-86400 * r.randint(1, 300))),
            )
        for _ in range(300):
            self._new_order(status=r.choice(STATUSES), synced=True, age=r.randint(3600, 86400 * 90))
        c.executemany(
            "INSERT INTO settings VALUES (?,?)",
            [("app_version", "2.4.1"), ("last_full_sync", _now(-600)), ("theme", "light"), ("locale", "en_IN")],
        )
        c.execute("COMMIT")

    def _new_order(self, status="placed", synced=False, age=0) -> int:
        c, r = self.con, self.rng
        customer = r.randint(1, 120)
        ouuid = str(uuid.UUID(int=r.getrandbits(128)))
        ts = _now(-age)
        cur = c.execute(
            "INSERT INTO orders(uuid, customer_id, status, total_inr, notes, created_at, updated_at, sync_status)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ouuid, customer, status, 0, r.choice([None, None, "Leave at gate", "Call on arrival", "Gift wrap"]),
             ts, ts, "synced" if synced else "dirty"),
        )
        oid = cur.lastrowid
        total = 0
        for _ in range(r.randint(1, 4)):
            pid = r.randint(1, len(PRODUCTS))
            qty = r.randint(1, 3)
            price = PRODUCTS[pid - 1][2]
            total += qty * price
            c.execute("INSERT INTO order_items(order_id, product_id, qty, price_inr) VALUES (?,?,?,?)",
                      (oid, pid, qty, price))
        c.execute("UPDATE orders SET total_inr=? WHERE id=?", (total, oid))
        if not synced:
            c.execute(
                "INSERT INTO sync_queue(entity, entity_uuid, op, payload, created_at) VALUES ('orders',?,'insert',?,?)",
                (ouuid, f'{{"customer_id": {customer}, "total_inr": {total}}}', ts),
            )
        return oid

    # live writes ------------------------------------------------------------

    def step(self) -> str:
        """One app action, like a user tapping something. Returns what it did."""
        c, r = self.con, self.rng
        roll = r.random()
        c.execute("BEGIN")
        try:
            if roll < 0.45:
                self._new_order()
                what = "new order"
            elif roll < 0.75:
                row = c.execute(
                    "SELECT id, status FROM orders WHERE status != 'delivered' ORDER BY random() LIMIT 1"
                ).fetchone()
                if row:
                    nxt = STATUSES[min(STATUSES.index(row[1]) + 1, len(STATUSES) - 1)] if row[1] in STATUSES else "packed"
                    c.execute("UPDATE orders SET status=?, updated_at=?, sync_status='dirty' WHERE id=?",
                              (nxt, _now(), row[0]))
                what = "status change"
            elif roll < 0.95:
                c.execute("UPDATE sync_queue SET sync_status='synced', attempts=attempts+1 "
                          "WHERE id IN (SELECT id FROM sync_queue WHERE sync_status='pending' LIMIT 2)")
                c.execute("UPDATE orders SET sync_status='synced' WHERE sync_status='dirty' "
                          "AND id IN (SELECT id FROM orders WHERE sync_status='dirty' LIMIT 2)")
                c.execute("UPDATE settings SET value=? WHERE key='last_full_sync'", (_now(),))
                what = "sync"
            else:
                c.execute("DELETE FROM sync_queue WHERE sync_status='synced' "
                          "AND id IN (SELECT id FROM sync_queue WHERE sync_status='synced' LIMIT 3)")
                what = "queue cleanup"
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return what

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.tick):
                self.step()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.con.close()
