from datetime import datetime, timedelta
import os
from pathlib import Path

from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from .config import settings

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    import certifi  # type: ignore
except Exception:
    certifi = None  # type: ignore


_client: MongoClient | None = None


def get_db():
    global _client
    if _client is None:
        # Load env vars from .env if available (so os.getenv works for non JSM_ keys)
        try:
            if load_dotenv is not None:
                # Load project root .env (../.env relative to this file)
                root_env = Path(__file__).resolve().parent.parent / ".env"
                if root_env.exists():
                    load_dotenv(dotenv_path=root_env, override=False)
                # Also try CWD as a fallback
                load_dotenv(override=False)
        except Exception:
            pass
        uri = settings.model_dump().get('mongodb_uri') or os.getenv('JSM_MONGODB_URI') or None
        if not uri:
            # Backwards compat: try env var directly
            uri = os.getenv('MONGODB_URI')
        if not uri:
            raise RuntimeError('MONGODB_URI not configured')
        client_kwargs = {"tls": True}
        if certifi is not None:
            try:
                client_kwargs["tlsCAFile"] = certifi.where()
            except Exception:
                # If certifi is unavailable or misconfigured, fall back to default trust store.
                pass
        _client = MongoClient(uri, **client_kwargs)
        _init_indexes(_client)
    db_name = (
        settings.model_dump().get('mongodb_db')
        or os.getenv('JSM_MONGODB_DB')
        or 'jetskiandmore'
    )
    db_name = str(db_name).strip() or 'jetskiandmore'
    return _client[db_name]


def _init_indexes(client: MongoClient):
    db_name = (settings.model_dump().get('mongodb_db') or 'jetskiandmore').strip() or 'jetskiandmore'
    db = client[db_name]
    # Timeslots: unique key on ride/date/time, TTL on holdUntil
    db.timeslots.create_index([('key', ASCENDING)], unique=True, name='uniq_slot_key')
    db.timeslots.create_index('holdUntil', expireAfterSeconds=0, name='ttl_hold_until')
    # Bookings: secondary indexes
    db.bookings.create_index([('email', ASCENDING)], name='idx_email')
    db.bookings.create_index([('date', ASCENDING)], name='idx_date')
    db.bookings.create_index([('rideId', ASCENDING), ('date', ASCENDING), ('time', ASCENDING)], name='idx_ride_date_time')
    # Rides: id unique
    db.rides.create_index([('id', ASCENDING)], unique=True, name='uniq_ride_id')
    # Pricing config doc
    db.pricing.create_index([('key', ASCENDING)], unique=True, name='uniq_pricing_key')


def slot_key(ride_id: str, date: str | None, time_str: str | None) -> str:
    return f"{ride_id}|{date or ''}|{time_str or ''}"


def hold_slot(ride_id: str, date: str | None, time_str: str | None, minutes: int = 20) -> bool:
    db = get_db()
    key = slot_key(ride_id, date, time_str)
    doc = {
        'rideId': ride_id,
        'date': date,
        'time': time_str,
        'key': key,
        'status': 'hold',
        'holdUntil': datetime.utcnow() + timedelta(minutes=max(1, minutes)),
        'createdAt': datetime.utcnow(),
    }
    try:
        db.timeslots.insert_one(doc)
        return True
    except DuplicateKeyError:
        return False


def book_slot(ride_id: str, date: str | None, time_str: str | None) -> None:
    db = get_db()
    key = slot_key(ride_id, date, time_str)
    db.timeslots.update_one({'key': key}, {'$set': {'status': 'booked'}, '$unset': {'holdUntil': ''}}, upsert=True)


def save_booking(booking: dict, amount_in_cents: int, payment_ref: str, status: str = 'approved') -> str:
    db = get_db()
    doc = {
        **booking,
        'amountInCents': int(amount_in_cents),
        'paymentRef': payment_ref,
        'status': status,
        'createdAt': datetime.utcnow(),
    }
    res = db.bookings.insert_one(doc)
    return str(res.inserted_id)
