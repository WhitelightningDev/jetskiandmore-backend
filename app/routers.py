from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import jwt
from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, status

from .config import settings
from .db import book_slot, get_db, hold_slot, save_booking
from .emailer import (
    format_booking_email,
    format_contact_email,
    format_payment_admin_email,
    format_payment_client_email,
    send_email,
)
from .pricing import compute_amount_cents
from .schemas import (
    AnalyticsSummaryResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    BookingAdminResponse,
    BookingRequest,
    BookingResponse,
    BookingUpdateRequest,
    ChargeBookingRequest,
    ChargeRequest,
    ChargeResponse,
    ContactRequest,
    ContactResponse,
    PaymentQuoteRequest,
    PaymentQuoteResponse,
    TimeslotAvailabilityResponse,
    VerifyCheckoutRequest,
    VerifyCheckoutResponse,
    VerifyPaymentByIdRequest,
    VerifyPaymentByIdResponse,
    VerifyPaymentRequest,
    VerifyPaymentResponse,
)
from .yoco import YocoError, _get_oauth_token, create_charge
import uuid
from urllib.parse import urlencode


router = APIRouter(prefix="/api")

# In-memory mapping from order_id -> booking dict for webhook/verification
ORDER_BOOKINGS: dict[str, dict] = {}
CHECKOUT_BOOKINGS: dict[str, dict] = {}


# --- Admin auth helpers ---

JWT_ALG = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


def _create_admin_token(subject: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm=JWT_ALG)


def _decode_admin_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, settings.admin_jwt_secret, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def get_current_admin(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.split(" ", 1)[1].strip()
    data = _decode_admin_token(token)
    subject = data.get("sub")
    expected = settings.admin_email or "admin"
    if subject != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid subject")
    return subject


@router.post("/contact", response_model=ContactResponse)
def contact(req: ContactRequest):
    if not settings.email_to:
        raise HTTPException(status_code=500, detail="Email recipient not configured")
    # Send to admin; set Reply-To to user
    body = format_contact_email(req.model_dump())
    try:
        send_email(subject="New contact message", body=body, to_address=settings.email_to, reply_to=req.email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email send failed: {e}")
    return ContactResponse(ok=True, id=str(uuid.uuid4()))


@router.post("/bookings", response_model=BookingResponse)
def bookings(req: BookingRequest):
    if not settings.email_to:
        raise HTTPException(status_code=500, detail="Email recipient not configured")
    # Send booking request email to admin; Reply-To to user
    body = format_booking_email(req.model_dump())
    try:
        send_email(subject="New booking request", body=body, to_address=settings.email_to, reply_to=req.email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email send failed: {e}")
    return BookingResponse(ok=True, id=str(uuid.uuid4()))


DAY_START_MINUTES = 8 * 60   # 08:00
DAY_END_MINUTES = 17 * 60    # 17:00
TIMESLOT_STEP_MINUTES = 15   # candidate granularity
BOOKING_BUFFER_MINUTES = 10  # buffer before & after each booking


def _ride_duration_minutes(db, ride_id: str) -> int:
    duration: Optional[int] = None
    try:
        ride = db.rides.find_one({"id": ride_id}, {"durationMinutes": 1})
        if ride is not None:
            d = ride.get("durationMinutes")
            if isinstance(d, int) and d > 0:
                duration = d
    except Exception:
        duration = None
    if duration is not None:
        return duration
    # Fallback mapping aligned with DEFAULT_RIDES in seed.py
    if ride_id in ("30-1", "30-2"):
        return 30
    if ride_id in ("60-1", "60-2"):
        return 60
    if ride_id == "joy":
        return 10
    if ride_id == "group":
        return 150
    return 30


def _parse_time_str_to_minutes(s: Any) -> Optional[int]:
    if not isinstance(s, str):
        return None
    try:
        hh, mm = map(int, s.split(":", 1))
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh * 60 + mm
    except Exception:
        return None
    return None


@router.get("/timeslots", response_model=TimeslotAvailabilityResponse)
def timeslots(rideId: str, date: str):
    if not rideId or not date:
        raise HTTPException(status_code=400, detail="rideId and date are required")
    # Basic date validation (expects YYYY-MM-DD)
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")

    db = get_db()
    duration = _ride_duration_minutes(db, rideId)
    buffer = BOOKING_BUFFER_MINUTES

    # Collect blocked intervals in minutes since midnight, expanded by buffer on both sides
    blocked: List[tuple[int, int]] = []

    # From timeslots collection: holds + booked
    try:
        slot_cursor = db.timeslots.find(
            {"rideId": rideId, "date": date, "status": {"$in": ["hold", "booked"]}},
            {"time": 1, "_id": 0},
        )
        for doc in slot_cursor:
            m = _parse_time_str_to_minutes(doc.get("time"))
            if m is None:
                continue
            start = m
            start_block = max(0, start - buffer)
            end_block = min(24 * 60, start + duration + buffer)
            blocked.append((start_block, end_block))
    except Exception:
        blocked = []

    # From bookings collection (in case timeslots missed a record)
    try:
        booking_cursor = db.bookings.find(
            {
                "rideId": rideId,
                "date": date,
                "status": {"$in": ["approved", "processing", "created"]},
            },
            {"time": 1, "_id": 0},
        )
        for doc in booking_cursor:
            m = _parse_time_str_to_minutes(doc.get("time"))
            if m is None:
                continue
            start = m
            start_block = max(0, start - buffer)
            end_block = min(24 * 60, start + duration + buffer)
            blocked.append((start_block, end_block))
    except Exception:
        pass

    # Generate candidate start times within the operating window
    available: List[str] = []
    latest_start = DAY_END_MINUTES - duration
    step = TIMESLOT_STEP_MINUTES

    t = DAY_START_MINUTES
    while t <= latest_start:
        candidate_start = t
        candidate_end = t + duration
        conflict = False
        for b_start, b_end in blocked:
            # Overlap check between [candidate_start, candidate_end) and [b_start, b_end)
            if not (candidate_end <= b_start or candidate_start >= b_end):
                conflict = True
                break
        if not conflict:
            hh = candidate_start // 60
            mm = candidate_start % 60
            available.append(f"{hh:02d}:{mm:02d}")
        t += step

    return TimeslotAvailabilityResponse(rideId=rideId, date=date, times=available)


# --- Admin: bookings CRUD & analytics ---


def _serialize_booking(doc: Dict[str, Any]) -> BookingAdminResponse:
    return BookingAdminResponse(
        id=str(doc.get("_id")),
        rideId=str(doc.get("rideId") or ""),
        date=doc.get("date"),
        time=doc.get("time"),
        fullName=str(doc.get("fullName") or ""),
        email=str(doc.get("email") or ""),
        phone=str(doc.get("phone") or ""),
        notes=doc.get("notes"),
        addons=doc.get("addons") or None,
        status=str(doc.get("status") or "unknown"),
        amountInCents=int(doc.get("amountInCents") or 0),
        paymentRef=doc.get("paymentRef"),
        createdAt=doc.get("createdAt"),
    )


@router.get("/admin/bookings", response_model=List[BookingAdminResponse])
def admin_list_bookings(
    limit: int = 100,
    skip: int = 0,
    status_filter: Optional[str] = None,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    query: Dict[str, Any] = {}
    if status_filter:
        query["status"] = status_filter
    cursor = (
        db.bookings.find(query)
        .sort("createdAt", -1)
        .skip(max(skip, 0))
        .limit(max(limit, 1))
    )
    return [_serialize_booking(doc) for doc in cursor]


@router.get("/admin/bookings/{booking_id}", response_model=BookingAdminResponse)
def admin_get_booking(booking_id: str, admin: str = Depends(get_current_admin)):
    db = get_db()
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Booking not found")
    doc = db.bookings.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Booking not found")
    return _serialize_booking(doc)


@router.patch("/admin/bookings/{booking_id}", response_model=BookingAdminResponse)
def admin_update_booking(
    booking_id: str,
    payload: BookingUpdateRequest,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Booking not found")
    updates: Dict[str, Any] = {}
    if payload.status is not None:
        updates["status"] = payload.status
    if payload.date is not None:
        updates["date"] = payload.date
    if payload.time is not None:
        updates["time"] = payload.time
    if payload.notes is not None:
        updates["notes"] = payload.notes
    if not updates:
        doc = db.bookings.find_one({"_id": oid})
        if not doc:
            raise HTTPException(status_code=404, detail="Booking not found")
        return _serialize_booking(doc)
    res = db.bookings.update_one({"_id": oid}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    doc = db.bookings.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Booking not found")
    return _serialize_booking(doc)


@router.delete("/admin/bookings/{booking_id}")
def admin_delete_booking(booking_id: str, admin: str = Depends(get_current_admin)):
    db = get_db()
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Booking not found")
    res = db.bookings.delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"ok": True}


@router.get("/admin/analytics/summary", response_model=AnalyticsSummaryResponse)
def admin_analytics_summary(admin: str = Depends(get_current_admin)):
    db = get_db()
    pipeline = [
        {
            "$group": {
                "_id": "$rideId",
                "bookings": {"$sum": 1},
                "revenueInCents": {"$sum": {"$toInt": {"$ifNull": ["$amountInCents", 0]}}},
            }
        }
    ]
    try:
        results = list(db.bookings.aggregate(pipeline))
    except Exception:
        results = []
    rides = []
    total_bookings = 0
    total_revenue_cents = 0
    for r in results:
        ride_id = str(r.get("_id") or "")
        bookings = int(r.get("bookings") or 0)
        revenue_cents = int(r.get("revenueInCents") or 0)
        total_bookings += bookings
        total_revenue_cents += revenue_cents
        rides.append(
            {
                "rideId": ride_id,
                "bookings": bookings,
                "revenueInCents": revenue_cents,
            }
        )
    return AnalyticsSummaryResponse(
        totalBookings=total_bookings,
        totalRevenueInCents=total_revenue_cents,
        totalRevenueZar=total_revenue_cents / 100.0,
        rides=rides,
    )


# --- Admin: authentication ---


@router.post("/admin/login", response_model=AdminLoginResponse)
def admin_login(payload: AdminLoginRequest):
    if not settings.admin_email or not settings.admin_password:
        raise HTTPException(status_code=500, detail="Admin credentials not configured")
    if payload.email != settings.admin_email or payload.password != settings.admin_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = _create_admin_token(subject=settings.admin_email)
    return AdminLoginResponse(token=token)


@router.post("/payments/charge", response_model=ChargeResponse)
def payments_charge(req: ChargeBookingRequest):
    # Compute authoritative amount server-side
    amount = compute_amount_cents(req.booking.rideId, req.booking.addons.model_dump())
    try:
        raw = create_charge(
            token=req.token,
            amount=amount,
            currency='ZAR',
            email=req.booking.email,
            reference=f"{req.booking.rideId}-{req.booking.date or ''}-{req.booking.time or ''}",
        )
    except YocoError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    charge_id = str(raw.get("id") or raw.get("chargeId") or "unknown")
    status = str(raw.get("status") or raw.get("outcome") or "unknown")

    # Best-effort email notifications on success
    try:
        success = status.lower() in ("successful", "succeeded", "paid", "approved", "captured") or raw.get("success") is True
        if success and settings.email_to:
            # Admin email
            admin_body = format_payment_admin_email(req.booking.model_dump(), amount, charge_id, status)
            send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=req.booking.email)
            # Client email
            client_body = format_payment_client_email(req.booking.model_dump(), amount, charge_id)
            send_email(subject="Booking confirmed — payment received", body=client_body, to_address=req.booking.email)
    except Exception:
        # Do not fail the charge response if emails fail
        pass
    # Persist booking + finalize slot
    try:
        book_slot(req.booking.rideId, req.booking.date, req.booking.time)
        save_booking(req.booking.model_dump(), amount, charge_id, status='approved' if success else status)
    except Exception:
        pass

    return ChargeResponse(ok=True, id=charge_id, status=status, raw=raw)


@router.post("/payments/quote", response_model=PaymentQuoteResponse)
def payments_quote(req: PaymentQuoteRequest):
    amount = compute_amount_cents(req.rideId, req.addons.model_dump())
    return PaymentQuoteResponse(amountInCents=amount)


@router.get("/payments/config")
def payments_config():
    if not settings.yoco_public_key:
        raise HTTPException(status_code=500, detail="Yoco public key not configured")
    return {"publicKey": settings.yoco_public_key, "currency": "ZAR"}


@router.post("/payments/initiate")
def payments_initiate(req: ChargeBookingRequest):
    # Accepts booking + placeholder token ignored; returns authoritative payment info for the UI
    amount = compute_amount_cents(req.booking.rideId, req.booking.addons.model_dump())
    if not settings.yoco_public_key:
        raise HTTPException(status_code=500, detail="Yoco public key not configured")
    # Soft-hold the slot to avoid races during payment (expires via TTL). Ignore DB errors.
    if req.booking.date and req.booking.time:
        try:
            ok = hold_slot(req.booking.rideId, req.booking.date, req.booking.time)
            if ok is False:
                raise HTTPException(status_code=409, detail="Selected time slot is no longer available")
        except Exception:
            # If DB is unavailable, proceed without holding to avoid 500s.
            pass
    reference = f"{req.booking.rideId}-{req.booking.date or ''}-{req.booking.time or ''}"
    return {"currency": "ZAR", "amountInCents": amount, "publicKey": settings.yoco_public_key, "reference": reference}


def _site_base() -> str:
    if settings.site_base_url:
        return settings.site_base_url.rstrip('/')
    if settings.allowed_origins:
        return str(settings.allowed_origins[0]).rstrip('/')
    return "https://jetskiandmore-frontend.vercel.app"


@router.post("/payments/checkout")
def payments_checkout(req: ChargeBookingRequest):
    # Build a hosted checkout session via Yoco Checkout API
    amount = compute_amount_cents(req.booking.rideId, req.booking.addons.model_dump())
    token = settings.yoco_checkout_token or settings.yoco_secret_key
    if not token:
        raise HTTPException(status_code=500, detail="Yoco checkout token not configured")
    if req.booking.date and req.booking.time:
        try:
            ok = hold_slot(req.booking.rideId, req.booking.date, req.booking.time)
            if ok is False:
                raise HTTPException(status_code=409, detail="Selected time slot is no longer available")
        except Exception:
            pass

    base = _site_base()
    # Append a hint param for result handling
    payload = {
        "amount": int(amount),
        "currency": "ZAR",
        # Dedicated result pages
        "successUrl": f"{base}/payments/success",
        "cancelUrl": f"{base}/payments/cancelled",
        "failureUrl": f"{base}/payments/failed",
        "metadata": {
            "rideId": req.booking.rideId,
            "date": req.booking.date,
            "time": req.booking.time,
            "name": req.booking.fullName,
            "email": req.booking.email,
            "phone": req.booking.phone,
        },
    }

    try:
        r = httpx.post(
            "https://payments.yoco.com/api/checkouts",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error contacting Yoco: {e}")

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        raise HTTPException(status_code=400, detail=f"Yoco checkout failed: {err}")

    data = r.json()
    checkout_id = str(data.get("id") or "")
    if checkout_id:
        try:
            CHECKOUT_BOOKINGS[checkout_id] = req.booking.model_dump()
        except Exception:
            pass
    return {"ok": True, "id": checkout_id, "redirectUrl": data.get("redirectUrl"), "raw": data}


@router.post("/payments/verify-checkout", response_model=VerifyCheckoutResponse)
def payments_verify_checkout(req: VerifyCheckoutRequest):
    token = settings.yoco_checkout_token or settings.yoco_secret_key
    if not token:
        raise HTTPException(status_code=500, detail="Yoco checkout token not configured")
    checkout_id = req.checkoutId
    try:
        r = httpx.get(
            f"https://payments.yoco.com/api/checkouts/{checkout_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error contacting Yoco: {e}")

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        raise HTTPException(status_code=400, detail=f"Yoco checkout verify failed: {err}")

    data = r.json()
    status = str(data.get("status") or "").lower() or "pending"
    payment_id = data.get("paymentId") or data.get("payment_id")

    # If completed, send emails immediately using booking context (no OAuth required)
    if status == "completed":
        try:
            booking = req.booking.model_dump()
            amount = compute_amount_cents(booking.get("rideId"), (booking.get("addons") or {}))
            charge_id = str(payment_id or checkout_id)
            # Persist booking and finalize slot
            try:
                book_slot(booking.get('rideId'), booking.get('date'), booking.get('time'))
                save_booking(booking, amount, charge_id, status='approved')
            except Exception:
                pass
            if settings.email_to:
                admin_body = format_payment_admin_email(booking, amount, charge_id, "approved")
                send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=booking.get("email"))
            client_body = format_payment_client_email(booking, amount, charge_id)
            send_email(subject="Booking confirmed — payment received", body=client_body, to_address=booking.get("email"))
        except Exception:
            pass

    return VerifyCheckoutResponse(ok=(status == "completed"), checkoutId=checkout_id, status=status, paymentId=payment_id)


@router.post("/payments/link")
def payments_link(req: ChargeBookingRequest):
    # Create a hosted Yoco Payment Link for this booking
    amount = compute_amount_cents(req.booking.rideId, req.booking.addons.model_dump())
    if not settings.yoco_client_id or not settings.yoco_client_secret:
        raise HTTPException(status_code=500, detail="Yoco OAuth not configured. Set JSM_YOCO_CLIENT_ID and JSM_YOCO_CLIENT_SECRET")
    if req.booking.date and req.booking.time:
        try:
            ok = hold_slot(req.booking.rideId, req.booking.date, req.booking.time)
            if ok is False:
                raise HTTPException(status_code=409, detail="Selected time slot is no longer available")
        except Exception:
            pass

    # Construct human-friendly reference/description
    reference = f"{req.booking.rideId}-{req.booking.date or ''}-{req.booking.time or ''}"
    customer_ref = (req.booking.fullName or "Customer").strip()[:100]
    description = (reference or "").strip()[:255]

    payload = {
        "amount": {"amount": int(amount), "currency": "ZAR"},
        "customer_reference": customer_ref,
        "customer_description": description,
    }

    try:
        token = _get_oauth_token("business/payment-links:write")
        r = httpx.post(
            "https://api.yoco.com/v1/payment_links/",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except YocoError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error contacting Yoco: {e}")

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        raise HTTPException(status_code=400, detail=f"Yoco link failed: {err}")

    data = r.json()
    # Try common url fields
    link_url = (
        data.get("short_url")
        or data.get("url")
        or data.get("redirect_url")
        or data.get("payment_page_url")
        or data.get("payment_link")
        or ""
    )
    order_id = data.get("order_id") or data.get("orderId")
    # Store booking context mapped by order for webhook / verify
    if order_id:
        try:
            ORDER_BOOKINGS[order_id] = req.booking.model_dump()
        except Exception:
            pass
    return {"ok": True, "linkUrl": link_url, "id": data.get("id") or data.get("payment_link_id"), "orderId": order_id, "raw": data}


@router.post("/payments/verify", response_model=VerifyPaymentResponse)
def payments_verify(req: VerifyPaymentRequest):
    if not settings.yoco_client_id or not settings.yoco_client_secret:
        raise HTTPException(status_code=500, detail="Yoco OAuth not configured. Set JSM_YOCO_CLIENT_ID and JSM_YOCO_CLIENT_SECRET")
    order_id = req.orderId
    # Stash latest booking context
    try:
        ORDER_BOOKINGS[order_id] = req.booking.model_dump()
    except Exception:
        pass

    # Fetch order and check payments
    try:
        token = _get_oauth_token("business/orders:read")
        resp = httpx.get(
            f"https://api.yoco.com/v1/orders/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except YocoError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error contacting Yoco: {e}")

    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"error": resp.text}
        raise HTTPException(status_code=400, detail=f"Yoco verify failed: {err}")

    order = resp.json()
    payments = order.get("payments") or []
    status = "pending"
    for p in payments:
        st = str(p.get("status") or "").lower()
        if st in ("approved", "captured", "succeeded", "successful"):
            status = "approved"
            break
        elif st in ("failed", "cancelled"):
            status = st

    # On approved, send emails
    if status == "approved" and settings.email_to:
        try:
            booking = ORDER_BOOKINGS.get(order_id) or req.booking.model_dump()
            amount = compute_amount_cents(booking.get("rideId"), (booking.get("addons") or {}))
            charge_id = payments[0].get("id") if payments else order.get("id") or order_id
            # Persist booking and finalize slot
            try:
                book_slot(booking.get('rideId'), booking.get('date'), booking.get('time'))
                save_booking(booking, amount, charge_id, status='approved')
            except Exception:
                pass
            admin_body = format_payment_admin_email(booking, amount, charge_id, status)
            send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=booking.get("email"))
            client_body = format_payment_client_email(booking, amount, charge_id)
            send_email(subject="Booking confirmed — payment received", body=client_body, to_address=booking.get("email"))
        except Exception:
            pass

    return VerifyPaymentResponse(ok=(status == "approved"), orderId=order_id, status=status)


@router.post("/payments/webhook/yoco")
def payments_webhook(payload: dict):
    """Best-effort webhook receiver for Yoco events.
    In production, verify signatures if provided by Yoco.
    """
    # Try extract order/payment identifiers
    order_id = (
        payload.get("order_id")
        or payload.get("orderId")
        or (payload.get("payment") or {}).get("order_id")
        or (payload.get("data") or {}).get("order_id")
    )
    payment_id = (
        payload.get("payment_id")
        or payload.get("paymentId")
        or (payload.get("payment") or {}).get("id")
    )
    # If we have an order id, reuse verify flow
    if order_id:
        try:
            booking = ORDER_BOOKINGS.get(order_id)
            if booking:
                req = VerifyPaymentRequest(orderId=str(order_id), booking=BookingRequest(**booking))
                return payments_verify(req)
        except Exception:
            pass
    # Fallback: resolve from payment id to order, if available
    if payment_id:
        try:
            token = _get_oauth_token("business/orders:read")
            pr = httpx.get(
                f"https://api.yoco.com/v1/payments/{payment_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if pr.status_code < 400:
                p = pr.json()
                oid = p.get("order_id")
                if oid:
                    booking = ORDER_BOOKINGS.get(oid)
                    if booking:
                        req = VerifyPaymentRequest(orderId=str(oid), booking=BookingRequest(**booking))
                        return payments_verify(req)
        except Exception:
            pass
    # Acknowledge webhook
    return {"ok": True}


@router.post("/payments/verify-by-payment", response_model=VerifyPaymentByIdResponse)
def payments_verify_by_payment(req: VerifyPaymentByIdRequest):
    if not settings.yoco_client_id or not settings.yoco_client_secret:
        raise HTTPException(status_code=500, detail="Yoco OAuth not configured. Set JSM_YOCO_CLIENT_ID and JSM_YOCO_CLIENT_SECRET")
    payment_id = req.paymentId
    try:
        token = _get_oauth_token("business/orders:read")
        pr = httpx.get(
            f"https://api.yoco.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error contacting Yoco: {e}")

    if pr.status_code >= 400:
        try:
            err = pr.json()
        except Exception:
            err = {"error": pr.text}
        raise HTTPException(status_code=400, detail=f"Yoco verify failed: {err}")

    p = pr.json()
    order_id = p.get("order_id")
    status = str(p.get("status") or "").lower() or "pending"

    # Stash booking under order id for emails
    try:
        if order_id:
            ORDER_BOOKINGS[order_id] = req.booking.model_dump()
    except Exception:
        pass

    # Delegate to existing verify if we have an order id to trigger emails
    if order_id:
        return payments_verify(VerifyPaymentRequest(orderId=str(order_id), booking=req.booking))

    return VerifyPaymentByIdResponse(ok=(status == "approved"), paymentId=payment_id, orderId=None, status=status)
