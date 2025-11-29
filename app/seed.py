from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

# Support both `python -m app.seed` and `python app/seed.py`
try:
    from .db import get_db, _init_indexes  # type: ignore
    from .config import settings
except ImportError:  # pragma: no cover
    from db import get_db, _init_indexes  # type: ignore
    from config import settings


DEFAULT_RIDES = [
    {
        "id": "30-1",
        "title": "30‑min Rental (1 Jet‑Ski)",
        "priceZar": 1750,
        "durationMinutes": 30,
    },
    {
        "id": "60-1",
        "title": "60‑min Rental (1 Jet‑Ski)",
        "priceZar": 2600,
        "durationMinutes": 60,
    },
    {
        "id": "30-2",
        "title": "30‑min Rental (2 Jet‑Skis)",
        "priceZar": 3100,
        "durationMinutes": 30,
    },
    {
        "id": "60-2",
        "title": "60‑min Rental (2 Jet‑Skis)",
        "priceZar": 4800,
        "durationMinutes": 60,
    },
    {
        "id": "30-3",
        "title": "30‑min Rental (3 Jet‑Skis)",
        "priceZar": 4500,
        "durationMinutes": 30,
    },
    {
        "id": "60-3",
        "title": "60‑min Rental (3 Jet‑Skis)",
        "priceZar": 6900,
        "durationMinutes": 60,
    },
    {
        "id": "30-4",
        "title": "30‑min Rental (4 Jet‑Skis)",
        "priceZar": 5800,
        "durationMinutes": 30,
    },
    {
        "id": "60-4",
        "title": "60‑min Rental (4 Jet‑Skis)",
        "priceZar": 9000,
        "durationMinutes": 60,
    },
    {
        "id": "30-5",
        "title": "30‑min Rental (5 Jet‑Skis)",
        "priceZar": 7100,
        "durationMinutes": 30,
    },
    {
        "id": "60-5",
        "title": "60‑min Rental (5 Jet‑Skis)",
        "priceZar": 11000,
        "durationMinutes": 60,
    },
    {
        "id": "joy",
        "title": "Joy Ride (Instructed) • 10 min",
        "priceZar": 700,
        "durationMinutes": 10,
    },
    {
        "id": "group",
        "title": "Group Session • 2 hr 30 min",
        "priceZar": 7500,
        "durationMinutes": 150,
    },
]

DEFAULT_ADDON_PRICING = {
    "key": "addons",
    "DRONE_PRICE": 700,
    "WETSUIT_PRICE": 150,
    "BOAT_PRICE_PER_PERSON": 450,
    "EXTRA_PERSON_PRICE": 350,
    "updatedAt": datetime.utcnow(),
}


def seed(db=None) -> Dict[str, Any]:
    if db is None:
        # Ensure env loaded (supports JSM_MONGODB_DB and MONGODB_URI)
        _ = settings  # trigger settings load
        db = get_db()

    # Ensure indexes exist
    try:
        # _init_indexes is called by get_db(), but call defensively if imported directly
        from pymongo import MongoClient  # noqa: F401
        _init_indexes(db.client)  # type: ignore[attr-defined]
    except Exception:
        pass

    # Upsert rides
    rides_upserted = 0
    for r in DEFAULT_RIDES:
        res = db.rides.update_one({"id": r["id"]}, {"$set": {**r, "updatedAt": datetime.utcnow()}}, upsert=True)
        if res.upserted_id or res.modified_count:
            rides_upserted += 1

    # Upsert addon pricing
    pr = db.pricing.update_one({"key": "addons"}, {"$set": DEFAULT_ADDON_PRICING}, upsert=True)

    return {
        "rides_upserted": rides_upserted,
        "pricing_modified": bool(pr.upserted_id or pr.modified_count),
        "db": db.name,
    }


if __name__ == "__main__":
    out = seed()
    print(f"Seeded DB '{out['db']}' — rides upserted: {out['rides_upserted']}, pricing updated: {out['pricing_modified']}")
