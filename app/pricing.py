from dataclasses import dataclass
from typing import Optional
import re
try:
    from .db import get_db
except Exception:
    get_db = None  # fallback if DB not configured


RIDES_ZAR = {
    '30-1': 1750,
    '60-1': 2600,
    '30-2': 3100,
    '60-2': 4800,
    '30-3': 4500,
    '60-3': 6900,
    '30-4': 5800,
    '60-4': 9000,
    '30-5': 7100,
    '60-5': 11000,
    'joy': 700,
    'group': 7500,
}

DRONE_PRICE = 700
WETSUIT_PRICE = 150
BOAT_PRICE_PER_PERSON = 450
EXTRA_PERSON_PRICE = 350
FREE_DRONE_RIDE_ID = '60-2'


@dataclass
class Addons:
    drone: bool = False
    gopro: bool = False
    wetsuit: bool = False
    boat: bool = False
    boatCount: int = 1
    extraPeople: int = 0


def max_extra_people(ride_id: str) -> int:
    if ride_id in ('joy', 'group'):
        return 0
    match = re.match(r'^(?:30|60)-(\d+)$', ride_id)
    if match:
        try:
            skis = int(match.group(1))
            return max(0, min(5, skis))
        except Exception:
            return 0
    if ride_id in ('30-1', '60-1'):
        return 1
    if ride_id in ('30-2', '60-2'):
        return 2
    return 0


def compute_amount_cents(ride_id: str, addons: dict) -> int:
    # Try DB-backed pricing first
    base_zar: Optional[int] = None
    drone_price = DRONE_PRICE
    wetsuit_price = WETSUIT_PRICE
    boat_price = BOAT_PRICE_PER_PERSON
    extra_person_price = EXTRA_PERSON_PRICE
    if get_db is not None:
        try:
            db = get_db()
            ride = db.rides.find_one({'id': ride_id}, projection={'priceZar': 1})
            if ride and isinstance(ride.get('priceZar'), (int, float)):
                base_zar = int(ride['priceZar'])
            cfg = db.pricing.find_one({'key': 'addons'}) or {}
            drone_price = int(cfg.get('DRONE_PRICE', drone_price))
            wetsuit_price = int(cfg.get('WETSUIT_PRICE', wetsuit_price))
            boat_price = int(cfg.get('BOAT_PRICE_PER_PERSON', boat_price))
            extra_person_price = int(cfg.get('EXTRA_PERSON_PRICE', extra_person_price))
        except Exception:
            base_zar = None

    # Fallback to constants
    base_zar = base_zar if base_zar is not None else RIDES_ZAR.get(ride_id)
    if base_zar is None:
        raise ValueError('Unknown ride')

    a = Addons(
        drone=bool(addons.get('drone')),
        gopro=bool(addons.get('gopro')),
        wetsuit=bool(addons.get('wetsuit')),
        boat=bool(addons.get('boat')),
        boatCount=max(1, int(addons.get('boatCount') or 1)),
        extraPeople=max(0, int(addons.get('extraPeople') or 0)),
    )

    # Clamp extras to ride constraints
    a.extraPeople = min(a.extraPeople, max_extra_people(ride_id))
    a.boatCount = min(a.boatCount, 10)

    drone_cost = 0
    if a.drone:
        drone_cost = 0 if ride_id == FREE_DRONE_RIDE_ID else drone_price

    wetsuit_cost = wetsuit_price if a.wetsuit else 0
    boat_cost = boat_price * (a.boatCount if a.boat else 0)
    extra_people_cost = extra_person_price * a.extraPeople

    total_zar = base_zar + drone_cost + wetsuit_cost + boat_cost + extra_people_cost
    return int(total_zar * 100)
