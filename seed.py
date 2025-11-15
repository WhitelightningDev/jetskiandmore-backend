#!/usr/bin/env python3
"""
Seed a MongoDB database with demo data (users, orders, rides, pricing, timeslots, bookings)
using pymongo and python-dotenv.

Quick start:
  python seed.py --fresh --seed-rides --seed-timeslots --timeslot-days 7 --verbose

Environment (.env example):
  # Connection string (SRV or standard)
  MONGODB_URI=mongodb+srv://user:pass@cluster.example.mongodb.net/
  # Database name
  MONGODB_DB=my_database

Requirements (requirements.txt example):
  pymongo==4.10.1
  python-dotenv==1.0.1
  certifi==2025.6.2

This script is idempotent: it uses upserts and applies JSON Schema validators.
It will create collections, validators, and indexes if missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore

try:
    import certifi  # type: ignore
except Exception:  # pragma: no cover
    certifi = None  # type: ignore

from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError


def jlog(level: str, message: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "level": level.lower(),
        "msg": message,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed MongoDB with demo data (users, orders, rides, pricing, timeslots, bookings)")
    p.add_argument("--fresh", action="store_true", help="Drop the database before seeding")
    p.add_argument(
        "--drop-collections",
        action="store_true",
        help="Drop the target collections (users, orders) before seeding",
    )
    p.add_argument("--env", default=".env", help="Path to .env file (default: ./.env)")
    p.add_argument("--verbose", action="store_true", help="Verbose logging")
    # Additional seeding for rides/pricing/timeslots
    p.add_argument("--seed-rides", action="store_true", help="Seed default rides & add-on pricing")
    p.add_argument("--seed-timeslots", action="store_true", help="Seed open time slots schedule")
    p.add_argument("--timeslot-days", type=int, default=0, help="Number of days ahead to generate time slots (0 = skip unless --seed-timeslots)")
    p.add_argument("--timeslot-start", default="08:00", help="Daily start time for slots (HH:MM)")
    p.add_argument("--timeslot-end", default="17:00", help="Daily end time for slots (HH:MM, exclusive)")
    p.add_argument("--timeslot-interval", type=int, default=30, help="Minutes between slots")
    return p.parse_args()


def getenvs(env_path: str) -> tuple[str, str]:
    # Load env file if present
    if load_dotenv is not None:
        try:
            load_dotenv(dotenv_path=env_path, override=False)
        except Exception:
            pass
    uri = os.getenv("MONGODB_URI") or os.getenv("JSM_MONGODB_URI")
    db = os.getenv("MONGODB_DB") or os.getenv("JSM_MONGODB_DB") or "jetskiandmore"
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (check your .env or environment)")
    return uri, db


def connect(uri: str) -> MongoClient:
    kwargs: Dict[str, Any] = {}
    if certifi is not None:
        try:
            kwargs["tlsCAFile"] = certifi.where()
        except Exception:
            pass
    return MongoClient(uri, serverSelectionTimeoutMS=8000, **kwargs)


def ensure_collection_with_validator(db, name: str, schema: Dict[str, Any]) -> None:
    try:
        if name not in db.list_collection_names():
            db.create_collection(name, validator={"$jsonSchema": schema})
            return
        # Collection exists: apply validator via collMod
        db.command({
            "collMod": name,
            "validator": {"$jsonSchema": schema},
            "validationLevel": "moderate",
        })
    except PyMongoError as e:
        jlog("error", "validator_apply_failed", collection=name, error=str(e))


def ensure_indexes(db) -> None:
    # Users indexes
    db.users.create_index([("email", ASCENDING)], unique=True, name="uniq_email")
    db.users.create_index([("username", ASCENDING)], unique=True, name="uniq_username", sparse=True)
    db.users.create_index([("createdAt", ASCENDING)], name="idx_created")
    # Orders indexes
    db.orders.create_index([("orderNumber", ASCENDING)], unique=True, name="uniq_order_number")
    db.orders.create_index([("userId", ASCENDING)], name="idx_user")
    db.orders.create_index([("createdAt", ASCENDING)], name="idx_order_created")
    # Rides indexes
    db.rides.create_index([("id", ASCENDING)], unique=True, name="uniq_ride_id")
    # Pricing indexes
    db.pricing.create_index([("key", ASCENDING)], unique=True, name="uniq_pricing_key")
    # Bookings indexes
    db.bookings.create_index([("email", ASCENDING)], name="idx_email")
    db.bookings.create_index([("date", ASCENDING)], name="idx_date")
    db.bookings.create_index(
        [("rideId", ASCENDING), ("date", ASCENDING), ("time", ASCENDING)],
        name="idx_ride_date_time",
    )
    # Timeslots indexes
    db.timeslots.create_index([("key", ASCENDING)], unique=True, name="uniq_slot_key")
    # TTL on holdUntil to auto-expire holds (0s after holdUntil)
    try:
        db.timeslots.create_index("holdUntil", expireAfterSeconds=0, name="ttl_hold_until")
    except Exception:
        # Some MongoDBs require special privileges for TTL; ignore failures
        pass


def users_schema() -> Dict[str, Any]:
    return {
        "bsonType": "object",
        "required": ["email", "name", "createdAt"],
        "properties": {
            "email": {"bsonType": "string", "description": "User email", "pattern": "@"},
            "username": {"bsonType": ["string", "null"]},
            "name": {"bsonType": "string"},
            "roles": {
                "bsonType": "array",
                "items": {"bsonType": "string"},
                "description": "User roles",
            },
            "createdAt": {"bsonType": "date"},
        },
        "additionalProperties": True,
    }


def orders_schema() -> Dict[str, Any]:
    return {
        "bsonType": "object",
        "required": ["orderNumber", "userId", "amount", "currency", "status", "createdAt"],
        "properties": {
            "orderNumber": {"bsonType": "string"},
            "userId": {"bsonType": "objectId"},
            "amount": {"bsonType": "int"},
            "currency": {"bsonType": "string", "minLength": 3, "maxLength": 3},
            "status": {"enum": ["created", "processing", "completed", "failed", "cancelled"]},
            "items": {"bsonType": "array", "items": {"bsonType": "object"}},
            "metadata": {"bsonType": ["object", "null"]},
            "createdAt": {"bsonType": "date"},
        },
        "additionalProperties": True,
    }


def rides_schema() -> Dict[str, Any]:
    return {
        "bsonType": "object",
        "required": ["id", "title", "priceZar"],
        "properties": {
            "id": {"bsonType": "string", "minLength": 2},
            "title": {"bsonType": "string"},
            "priceZar": {"bsonType": "int"},
            "durationMinutes": {"bsonType": ["int", "null"]},
            "updatedAt": {"bsonType": ["date", "null"]},
        },
        "additionalProperties": True,
    }


def bookings_schema() -> Dict[str, Any]:
    return {
        "bsonType": "object",
        "required": ["rideId", "fullName", "email", "status", "amountInCents", "createdAt"],
        "properties": {
            "rideId": {"bsonType": "string"},
            "date": {"bsonType": ["string", "null"], "description": "YYYY-MM-DD"},
            "time": {"bsonType": ["string", "null"], "description": "HH:MM"},
            "fullName": {"bsonType": "string"},
            "email": {"bsonType": "string", "pattern": "@"},
            "phone": {"bsonType": ["string", "null"]},
            "notes": {"bsonType": ["string", "null"]},
            "addons": {"bsonType": ["object", "null"]},
            "passengers": {
                "bsonType": ["array", "null"],
                "items": {"bsonType": "object"},
            },
            "status": {"bsonType": "string"},
            "amountInCents": {"bsonType": "int"},
            "paymentRef": {"bsonType": ["string", "null"]},
            "createdAt": {"bsonType": "date"},
        },
        "additionalProperties": True,
    }


def pricing_schema() -> Dict[str, Any]:
    return {
        "bsonType": "object",
        "required": ["key"],
        "properties": {
            "key": {"bsonType": "string"},
            "DRONE_PRICE": {"bsonType": ["int", "null"]},
            "WETSUIT_PRICE": {"bsonType": ["int", "null"]},
            "BOAT_PRICE_PER_PERSON": {"bsonType": ["int", "null"]},
            "EXTRA_PERSON_PRICE": {"bsonType": ["int", "null"]},
            "updatedAt": {"bsonType": ["date", "null"]},
        },
        "additionalProperties": True,
    }


def timeslots_schema() -> Dict[str, Any]:
    return {
        "bsonType": "object",
        "required": ["key", "rideId", "date", "time"],
        "properties": {
            "key": {"bsonType": "string"},
            "rideId": {"bsonType": "string"},
            "date": {"bsonType": "string", "description": "YYYY-MM-DD"},
            "time": {"bsonType": "string", "description": "HH:MM"},
            "status": {"enum": ["open", "hold", "booked"]},
            "holdUntil": {"bsonType": ["date", "null"]},
            "createdAt": {"bsonType": ["date", "null"]},
        },
        "additionalProperties": True,
    }


def upsert_users(db, verbose: bool = False) -> List[Any]:
    now = datetime.utcnow()
    seed_users = [
        {
            "email": "admin@example.com",
            "username": "admin",
            "name": "Admin User",
            "roles": ["admin"],
            "createdAt": now,
        },
        {
            "email": "user@example.com",
            "username": "daniel",
            "name": "Daniel Mommsen",
            "roles": ["customer"],
            "createdAt": now,
        },
    ]
    ids: List[Any] = []
    for u in seed_users:
        res = db.users.update_one({"email": u["email"]}, {"$set": u}, upsert=True)
        if res.upserted_id:
            ids.append(res.upserted_id)
        if verbose:
            jlog("info", "user_upsert", email=u["email"], upserted=bool(res.upserted_id), modified=res.modified_count)
    return ids


def upsert_orders(db, verbose: bool = False) -> None:
    # Fetch users to link by email
    users = {d["email"]: d for d in db.users.find({}, {"_id": 1, "email": 1})}
    now = datetime.utcnow()
    seed_orders = [
        {
            "orderNumber": "ORD-1001",
            "userEmail": "admin@example.com",
            "amount": 260000,
            "currency": "ZAR",
            "status": "completed",
            "items": [{"sku": "60-2", "qty": 1, "label": "60‑min Rental (2 Jet‑Skis)"}],
            "metadata": {"source": "seed"},
            "createdAt": now,
        },
        {
            "orderNumber": "ORD-1002",
            "userEmail": "user@example.com",
            "amount": 175000,
            "currency": "ZAR",
            "status": "created",
            "items": [{"sku": "30-1", "qty": 1, "label": "30‑min Rental (1 Jet‑Ski)"}],
            "metadata": {"source": "seed"},
            "createdAt": now,
        },
    ]
    for o in seed_orders:
        user = users.get(o["userEmail"]) or {}
        doc = {k: v for k, v in o.items() if k != "userEmail"}
        if user:
            doc["userId"] = user["_id"]
        res = db.orders.update_one({"orderNumber": doc["orderNumber"]}, {"$set": doc}, upsert=True)
        if verbose:
            jlog("info", "order_upsert", orderNumber=doc["orderNumber"], upserted=bool(res.upserted_id), modified=res.modified_count)


DEFAULT_RIDES = [
    {"id": "30-1", "title": "30‑min Rental (1 Jet‑Ski)", "priceZar": 1750, "durationMinutes": 30},
    {"id": "60-1", "title": "60‑min Rental (1 Jet‑Ski)", "priceZar": 2600, "durationMinutes": 60},
    {"id": "30-2", "title": "30‑min Rental (2 Jet‑Skis)", "priceZar": 3100, "durationMinutes": 30},
    {"id": "60-2", "title": "60‑min Rental (2 Jet‑Skis)", "priceZar": 4800, "durationMinutes": 60},
    {"id": "joy", "title": "Joy Ride (Instructed) • 10 min", "priceZar": 700, "durationMinutes": 10},
    {"id": "group", "title": "Group Session • 2 hr 30 min", "priceZar": 7500, "durationMinutes": 150},
]

DEFAULT_ADDON_PRICING = {
    "key": "addons",
    "DRONE_PRICE": 700,
    "WETSUIT_PRICE": 150,
    "BOAT_PRICE_PER_PERSON": 450,
    "EXTRA_PERSON_PRICE": 350,
    "updatedAt": datetime.utcnow(),
}


def upsert_rides_and_pricing(db, verbose: bool = False) -> None:
    # Validators
    ensure_collection_with_validator(db, "rides", rides_schema())
    ensure_collection_with_validator(db, "pricing", pricing_schema())
    # Upserts
    modified = 0
    for r in DEFAULT_RIDES:
        doc = {**r, "updatedAt": datetime.utcnow()}
        res = db.rides.update_one({"id": r["id"]}, {"$set": doc}, upsert=True)
        modified += int(bool(res.upserted_id or res.modified_count))
        if verbose:
            jlog("info", "ride_upsert", id=r["id"], upserted=bool(res.upserted_id), modified=res.modified_count)
    pr = db.pricing.update_one({"key": "addons"}, {"$set": DEFAULT_ADDON_PRICING}, upsert=True)
    if verbose:
        jlog("info", "pricing_upsert", upserted=bool(pr.upserted_id), modified=pr.modified_count)


def slot_key(ride_id: str, date: str, time: str) -> str:
    return f"{ride_id}|{date}|{time}"


def seed_timeslots(db, days: int, start: str, end: str, interval_min: int, verbose: bool = False) -> int:
    from datetime import timedelta, date

    # Ensure validator exists
    ensure_collection_with_validator(db, "timeslots", timeslots_schema())

    try:
        start_h, start_m = map(int, start.split(":", 1))
        end_h, end_m = map(int, end.split(":", 1))
    except Exception:
        raise ValueError("Invalid --timeslot-start or --timeslot-end format, expected HH:MM")
    if interval_min <= 0:
        raise ValueError("--timeslot-interval must be > 0")

    rides = list(db.rides.find({}, {"id": 1}))
    ride_ids = [r["id"] for r in rides] or [r["id"] for r in DEFAULT_RIDES]
    count = 0
    today = date.today()

    for d in range(days):
        dt = today + timedelta(days=d)
        date_str = dt.isoformat()
        # Sweep through times
        t_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        while t_minutes < end_minutes:
            hh = t_minutes // 60
            mm = t_minutes % 60
            time_str = f"{hh:02d}:{mm:02d}"
            for ride_id in ride_ids:
                key = slot_key(ride_id, date_str, time_str)
                doc = {
                    "key": key,
                    "rideId": ride_id,
                    "date": date_str,
                    "time": time_str,
                    "status": "open",
                    "createdAt": datetime.utcnow(),
                }
                res = db.timeslots.update_one({"key": key}, {"$setOnInsert": doc}, upsert=True)
                # Only count new slots
                if res.upserted_id:
                    count += 1
                if verbose and (res.upserted_id or res.modified_count):
                    jlog("info", "slot_seed", rideId=ride_id, date=date_str, time=time_str, upserted=bool(res.upserted_id))
            t_minutes += interval_min
    return count


def drop_collections(db, names: List[str]) -> None:
    for name in names:
        try:
            db.drop_collection(name)
            jlog("info", "collection_dropped", collection=name)
        except PyMongoError as e:
            jlog("error", "collection_drop_failed", collection=name, error=str(e))


def main() -> int:
    args = parse_args()
    try:
        uri, db_name = getenvs(args.env)
    except Exception as e:
        jlog("error", "env_error", error=str(e))
        return 2

    # Connect
    try:
        client = connect(uri)
        # Touch the server to validate connection
        client.admin.command("ping")
        jlog("info", "connected", host=str(client.address))
    except Exception as e:
        jlog("error", "connect_failed", error=str(e))
        return 2

    db = client[db_name]

    if args.fresh:
        try:
            client.drop_database(db_name)
            jlog("warn", "database_dropped", db=db_name)
        except PyMongoError as e:
            jlog("error", "drop_db_failed", db=db_name, error=str(e))
            return 2

    if args.drop_collections and not args.fresh:
        drop_collections(db, ["users", "orders", "rides", "pricing", "timeslots", "bookings"])

    # Ensure validators
    ensure_collection_with_validator(db, "users", users_schema())
    ensure_collection_with_validator(db, "orders", orders_schema())
    ensure_collection_with_validator(db, "rides", rides_schema())
    ensure_collection_with_validator(db, "pricing", pricing_schema())
    ensure_collection_with_validator(db, "timeslots", timeslots_schema())
    ensure_collection_with_validator(db, "bookings", bookings_schema())

    # Indexes
    ensure_indexes(db)

    # Seed data (idempotent)
    upsert_users(db, verbose=args.verbose)
    upsert_orders(db, verbose=args.verbose)
    if args.seed_rides or args.seed_timeslots:
        upsert_rides_and_pricing(db, verbose=args.verbose)
    new_slots = 0
    if args.seed_timeslots and args.timeslot_days > 0:
        new_slots = seed_timeslots(db, args.timeslot_days, args.timeslot_start, args.timeslot_end, args.timeslot_interval, verbose=args.verbose)

    # Summary
    summary_cols = ["users", "orders", "rides", "pricing", "timeslots", "bookings"]
    summary = {
        "db": db_name,
        "collections": {name: (db[name].count_documents({}) if name in db.list_collection_names() else 0) for name in summary_cols},
        "newSlots": new_slots,
    }
    jlog("info", "seed_complete", **summary)
    # Also print a friendly summary line
    print(
        "Seeded '" + db_name + "': "
        + ", ".join([f"{k}={v}" for k, v in summary["collections"].items()])
        + (f", newSlots={new_slots}" if new_slots else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
