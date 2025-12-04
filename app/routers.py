from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import secrets
import string

import httpx
import re
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
    format_booking_confirmation_email,
    format_participant_notification,
    build_indemnity_link,
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
    InterimSkipperQuizAdminResponse,
    InterimSkipperQuizRequest,
    InterimSkipperQuizResponse,
    PaymentQuoteRequest,
    PaymentQuoteResponse,
    TimeslotAvailabilityResponse,
    VerifyCheckoutRequest,
    VerifyCheckoutResponse,
    VerifyPaymentByIdRequest,
    VerifyPaymentByIdResponse,
    VerifyPaymentRequest,
    VerifyPaymentResponse,
    PageViewRequest,
    IndemnitySubmitRequest,
    IndemnityStatusResponse,
    IndemnityStatusItem,
)
from .yoco import YocoError, _get_oauth_token, create_charge
import uuid
from urllib.parse import urlencode


router = APIRouter(prefix="/api")

# In-memory mapping from order_id -> booking dict for webhook/verification
ORDER_BOOKINGS: dict[str, dict] = {}
CHECKOUT_BOOKINGS: dict[str, dict] = {}
INDEMNITY_PATH = "/indemnity"


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


# --- Booking helpers ---


def _number_of_jet_skis(ride_id: str) -> int:
    try:
        match = re.match(r"^(?:30|60)-(\d+)", ride_id or "")
        if match:
            n = int(match.group(1))
            return max(1, min(10, n))
        if ride_id == "group":
            return 5
        return 1
    except Exception:
        return 1


def _generate_booking_reference(now: Optional[datetime] = None) -> str:
    now = now or datetime.utcnow()
    return f"JSM-{now.year}-{now.strftime('%m%d%H%M%S')}"


def _generate_booking_group_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    token = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"JSM-BOOK-{token}"


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


@router.post("/interim-skipper-quiz", response_model=InterimSkipperQuizResponse)
def interim_skipper_quiz(req: InterimSkipperQuizRequest):
    errors: list[str] = []
    text_fields = [
        ("email", req.email),
        ("name", req.name),
        ("surname", req.surname),
        ("idNumber", req.idNumber),
    ]
    for key, value in text_fields:
        if not str(value).strip():
            errors.append(f"{key} is required")

    if not req.hasWatchedTutorial:
        errors.append("hasWatchedTutorial must be true")
    if not req.hasAcceptedIndemnity:
        errors.append("hasAcceptedIndemnity must be true")

    quiz = req.quizAnswers
    quiz_fields = [
        ("q1_distance_from_shore", quiz.q1_distance_from_shore),
        ("q2_kill_switch", quiz.q2_kill_switch),
        ("q3_what_to_wear", quiz.q3_what_to_wear),
        ("q4_kill_switch_connection", quiz.q4_kill_switch_connection),
        ("q5_harbour_passing_rule", quiz.q5_harbour_passing_rule),
        ("q7_max_distance", quiz.q7_max_distance),
    ]
    for key, value in quiz_fields:
        if not str(value).strip():
            errors.append(f"{key} is required")

    list_quiz_fields = [
        ("q6_harbour_rules", quiz.q6_harbour_rules),
        ("q8_connect_kill_switch_two_places", quiz.q8_connect_kill_switch_two_places),
        ("q9_deposit_loss_reasons", quiz.q9_deposit_loss_reasons),
        ("q10_emergency_items_onboard", quiz.q10_emergency_items_onboard),
    ]
    for key, value in list_quiz_fields:
        if not value or len(value) == 0:
            errors.append(f"{key} must have at least one selection")

    if errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=errors)

    db = get_db()
    doc = {
        "email": req.email,
        "name": req.name,
        "surname": req.surname,
        "id_number": req.idNumber,
        "passenger_name": req.passengerName.strip() if req.passengerName else None,
        "passenger_surname": req.passengerSurname.strip() if req.passengerSurname else None,
        "passenger_email": str(req.passengerEmail).strip() if req.passengerEmail else None,
        "passenger_id_number": req.passengerIdNumber.strip() if req.passengerIdNumber else None,
        "has_watched_tutorial": req.hasWatchedTutorial,
        "has_accepted_indemnity": req.hasAcceptedIndemnity,
        "quiz_answers": req.quizAnswers.model_dump(),
        "created_at": datetime.utcnow(),
    }
    res = db.interim_skipper_quiz_submission.insert_one(doc)
    return InterimSkipperQuizResponse(ok=True, success=True, id=str(res.inserted_id))


def _serialize_quiz_submission(doc: Dict[str, Any]) -> InterimSkipperQuizAdminResponse:
    return InterimSkipperQuizAdminResponse(
        id=str(doc.get("_id")),
        email=str(doc.get("email") or ""),
        name=str(doc.get("name") or ""),
        surname=str(doc.get("surname") or ""),
        idNumber=str(doc.get("id_number") or ""),
        passengerName=doc.get("passenger_name"),
        passengerSurname=doc.get("passenger_surname"),
        passengerEmail=doc.get("passenger_email"),
        passengerIdNumber=doc.get("passenger_id_number"),
        hasWatchedTutorial=bool(doc.get("has_watched_tutorial")),
        hasAcceptedIndemnity=bool(doc.get("has_accepted_indemnity")),
        quizAnswers=doc.get("quiz_answers") or {},
        createdAt=doc.get("created_at"),
    )


@router.get("/admin/interim-skipper-quiz", response_model=List[InterimSkipperQuizAdminResponse])
def admin_list_interim_skipper_quiz(
    limit: int = 100,
    skip: int = 0,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    cursor = (
        db.interim_skipper_quiz_submission.find()
        .sort("created_at", -1)
        .skip(max(skip, 0))
        .limit(max(limit, 1))
    )
    return [_serialize_quiz_submission(doc) for doc in cursor]


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
    if re.match(r"^30-\d+$", ride_id):
        return 30
    if re.match(r"^60-\d+$", ride_id):
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
        passengers=doc.get("passengers") or None,
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

    # If status changed and we have a message + customer email, notify client
    try:
        if payload.status is not None and payload.message:
            try:
                from .emailer import format_booking_status_update_email
            except Exception:
                format_booking_status_update_email = None  # type: ignore
            if format_booking_status_update_email is not None:
                body = format_booking_status_update_email(
                    _serialize_booking(doc).model_dump(),
                    payload.status,
                    payload.message,
                )
                send_email(
                    subject=f"Booking update — {payload.status}",
                    body=body,
                    to_address=str(doc.get("email") or ""),
                )
    except Exception:
        # Do not break admin flow if email fails
        pass

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
    try:
        total_page_views = db.page_views.count_documents({})
    except Exception:
        total_page_views = 0
    return AnalyticsSummaryResponse(
        totalBookings=total_bookings,
        totalRevenueInCents=total_revenue_cents,
        totalRevenueZar=total_revenue_cents / 100.0,
        totalPageViews=total_page_views,
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


@router.post("/metrics/pageview")
def track_page_view(payload: PageViewRequest, user_agent: Optional[str] = Header(None)):
    db = get_db()
    doc = {
        "path": (payload.path or "").strip(),
        "referrer": (payload.referrer or "").strip() or None,
        "user_agent": (payload.userAgent or "").strip() or (user_agent or None),
        "created_at": datetime.utcnow(),
    }
    try:
        db.page_views.insert_one(doc)
    except Exception:
        # Don't fail the client if analytics storage has an issue
        pass
    return {"ok": True}


@router.post("/indemnities/submit")
def submit_indemnity(payload: IndemnitySubmitRequest):
    if not payload.token:
        raise HTTPException(status_code=400, detail="Missing token")
    db = get_db()
    participant = db.participants.find_one({"indemnityToken": payload.token})
    if not participant:
        raise HTTPException(status_code=404, detail="Invalid token")
    booking_id = participant.get("bookingId")
    booking_group_id = participant.get("bookingGroupId")
    try:
        pid = participant.get("_id")
        db.indemnities.update_one(
            {"participantId": pid},
            {
                "$set": {
                    "bookingId": booking_id,
                    "bookingGroupId": booking_group_id,
                    "participantId": pid,
                    "fullName": payload.fullName or participant.get("fullName") or "",
                    "email": payload.email or participant.get("email"),
                    "role": participant.get("role"),
                    "signedAt": datetime.utcnow(),
                    "hasWatchedVideo": bool(payload.hasWatchedVideo),
                }
            },
            upsert=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save indemnity: {e}")
    return {"ok": True}


@router.get("/bookings/{booking_id}/indemnities", response_model=IndemnityStatusResponse)
def get_indemnities_for_booking(booking_id: str, admin: str = Depends(get_current_admin)):
    db = get_db()
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking = db.bookings.find_one({"_id": oid})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    participants = list(db.participants.find({"bookingId": oid}))
    indemnities = {str(i.get("participantId")): i for i in db.indemnities.find({"bookingId": oid})}

    resp_items: list[IndemnityStatusItem] = []
    for p in participants:
        pid = str(p.get("_id"))
        ind = indemnities.get(pid)
        status = "PENDING"
        signed_at = None
        if ind and ind.get("signedAt"):
            status = "SIGNED"
            signed_at = ind.get("signedAt")
        resp_items.append(
            IndemnityStatusItem(
                participantId=pid,
                fullName=str(p.get("fullName") or ""),
                email=p.get("email"),
                role=str(p.get("role") or ""),
                isRider=bool(p.get("isRider")),
                positionNumber=int(p.get("positionNumber") or 0),
                indemnityStatus=status,
                signedAt=signed_at,
            )
        )

    return IndemnityStatusResponse(
        bookingId=str(booking.get("_id")),
        bookingGroupId=str(booking.get("bookingGroupId") or ""),
        rideId=booking.get("rideId"),
        date=booking.get("date"),
        time=booking.get("time"),
        participants=resp_items,
    )


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

    success = status.lower() in ("successful", "succeeded", "paid", "approved", "captured") or raw.get("success") is True
    # Best-effort admin email on success
    try:
        if success and settings.email_to:
            admin_body = format_payment_admin_email(req.booking.model_dump(), amount, charge_id, status)
            send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=req.booking.email)
    except Exception:
        pass
    # Persist booking + finalize slot + notify participants
    try:
        book_slot(req.booking.rideId, req.booking.date, req.booking.time)
    except Exception:
        pass
    try:
        _persist_booking_and_notify(req.booking.model_dump(), amount, charge_id, status='approved' if success else status)
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
        return settings.site_base_url.rstrip("/")
    # Prefer a non-localhost origin if configured
    if settings.allowed_origins:
        for origin in settings.allowed_origins:
            o = str(origin or "").strip()
            if not o:
                continue
            if o.startswith("http://localhost") or o.startswith("http://127.0.0.1"):
                continue
            return o.rstrip("/")
        # Fallback: first origin, even if local, for explicit setups
        return str(settings.allowed_origins[0]).rstrip("/")
    # Hard-coded live frontend as final fallback
    return "https://jetskiandmore-frontend.vercel.app"


def _prepare_booking_doc(raw_booking: dict) -> dict:
    now = datetime.utcnow()
    doc = dict(raw_booking)
    if not doc.get("bookingReference"):
        doc["bookingReference"] = _generate_booking_reference(now)
    if not doc.get("bookingGroupId"):
        doc["bookingGroupId"] = _generate_booking_group_id()
    if not doc.get("numberOfJetSkis"):
        doc["numberOfJetSkis"] = _number_of_jet_skis(str(doc.get("rideId") or ""))
    if not doc.get("createdByCustomerId"):
        doc["createdByCustomerId"] = doc.get("email") or doc.get("fullName")
    doc["createdAt"] = now
    return doc


def _participant_role_label(role: str, position: int, is_rider: bool) -> str:
    base = "Rider" if is_rider else "Passenger"
    if role.upper() == "PRIMARY_RIDER":
        return "Primary Rider"
    return f"{base} #{position}"


def _create_participants(db, booking_doc: dict, booking_id: str) -> list[dict]:
    booking_group_id = booking_doc.get("bookingGroupId")
    number_of_skis = int(booking_doc.get("numberOfJetSkis") or 1)
    passengers = booking_doc.get("passengers") or []
    addons = booking_doc.get("addons") or {}
    try:
        extra_people = int(addons.get("extraPeople") or 0)
    except Exception:
        extra_people = 0

    participants: list[dict] = []
    # Primary rider
    participants.append(
        {
            "bookingId": ObjectId(booking_id),
            "bookingGroupId": booking_group_id,
            "fullName": booking_doc.get("fullName") or "Primary rider",
            "email": booking_doc.get("email"),
            "role": "PRIMARY_RIDER",
            "isRider": True,
            "positionNumber": 1,
            "indemnityToken": secrets.token_urlsafe(16),
            "createdAt": datetime.utcnow(),
        }
    )
    # Additional riders (if more jet skis)
    for idx in range(2, number_of_skis + 1):
        participants.append(
            {
                "bookingId": ObjectId(booking_id),
                "bookingGroupId": booking_group_id,
                "fullName": "",
                "email": None,
                "role": f"RIDER_{idx}",
                "isRider": True,
                "positionNumber": idx,
                "indemnityToken": secrets.token_urlsafe(16),
                "createdAt": datetime.utcnow(),
            }
        )

    # Passengers provided
    for idx, p in enumerate(passengers):
        name = ""
        email = None
        try:
            if isinstance(p, dict):
                name = str(p.get("name") or "").strip()
                email = p.get("email")
        except Exception:
            name = ""
        participants.append(
            {
                "bookingId": ObjectId(booking_id),
                "bookingGroupId": booking_group_id,
                "fullName": name or f"Passenger {idx + 1}",
                "email": email,
                "role": f"PASSENGER_{idx + 1}",
                "isRider": False,
                "positionNumber": idx + 1,
                "indemnityToken": secrets.token_urlsafe(16),
                "createdAt": datetime.utcnow(),
            }
        )

    # Extra unnamed passengers (from add-ons)
    current_passengers = max(len(passengers), 0)
    for extra_idx in range(current_passengers + 1, current_passengers + extra_people + 1):
        participants.append(
            {
                "bookingId": ObjectId(booking_id),
                "bookingGroupId": booking_group_id,
                "fullName": f"Passenger {extra_idx}",
                "email": None,
                "role": f"PASSENGER_{extra_idx}",
                "isRider": False,
                "positionNumber": extra_idx,
                "indemnityToken": secrets.token_urlsafe(16),
                "createdAt": datetime.utcnow(),
            }
        )

    if participants:
        res = db.participants.insert_many(participants)
        for i, oid in enumerate(res.inserted_ids):
            participants[i]["_id"] = oid
    return participants


def _send_booking_notifications(booking_doc: dict, participants: list[dict]):
    base = _site_base()
    booking_reference = booking_doc.get("bookingReference") or ""
    booking_group_id = booking_doc.get("bookingGroupId") or ""
    ride_label = _ride_label(booking_doc.get("rideId"), include_code=True)
    indemnity_links: dict[str, str] = {}
    for p in participants:
        token = p.get("indemnityToken")
        pid = str(p.get("_id") or p.get("id") or "")
        if token and pid:
            indemnity_links[pid] = build_indemnity_link(f"{base}{INDEMNITY_PATH}", token)

    try:
        if booking_doc.get("email"):
            body = format_booking_confirmation_email(
                booking_doc,
                participants,
                booking_reference,
                booking_group_id,
                indemnity_links,
            )
            sent = send_email(
                subject=f"Booking confirmed — {booking_reference}",
                body=body,
                body_html=body,
                to_address=booking_doc["email"],
                reply_to=booking_doc.get("email"),
            )
            if not sent:
                print(f"[email] Failed sending primary confirmation to {booking_doc.get('email')}")
    except Exception:
        pass

    # Skip additional participant notifications per request


def _persist_booking_and_notify(booking: dict, amount: int, charge_id: str, status: str) -> Optional[str]:
    db = get_db()
    doc = _prepare_booking_doc(booking)
    try:
        booking_id = save_booking(doc, amount, charge_id, status=status)
    except Exception:
        return None
    try:
        participants = _create_participants(db, doc, booking_id)
    except Exception:
        participants = []
    # Always ensure we email the primary booker even if participant creation fails
    if not participants:
        participants = []
    participants = participants or []
    # If primary is missing, add one for emailing
    if not any((p.get("role") or "").upper() == "PRIMARY_RIDER" for p in participants):
        participants.insert(
            0,
            {
                "_id": ObjectId(),
                "bookingId": booking_id,
                "bookingGroupId": doc.get("bookingGroupId"),
                "fullName": doc.get("fullName") or "Primary rider",
                "email": doc.get("email"),
                "role": "PRIMARY_RIDER",
                "isRider": True,
                "positionNumber": 1,
                "indemnityToken": secrets.token_urlsafe(16),
            },
        )
    try:
        _send_booking_notifications(doc | {"_id": booking_id}, participants)
    except Exception as e:
        print(f"[email] Notification send failed: {e}")
    return booking_id


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

    success_states = {"completed", "approved", "captured", "succeeded", "successful", "paid"}
    is_success = status in success_states

    # On success, send emails immediately using booking context (no OAuth required)
    if is_success:
        try:
            booking = req.booking.model_dump()
            amount = compute_amount_cents(booking.get("rideId"), (booking.get("addons") or {}))
            charge_id = str(payment_id or checkout_id)
            # Persist booking and finalize slot
            try:
                book_slot(booking.get('rideId'), booking.get('date'), booking.get('time'))
                _persist_booking_and_notify(booking, amount, charge_id, status='approved')
            except Exception:
                pass
            if settings.email_to:
                admin_body = format_payment_admin_email(booking, amount, charge_id, "approved")
                send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=booking.get("email"))
        except Exception:
            pass

    return VerifyCheckoutResponse(ok=is_success, checkoutId=checkout_id, status=status, paymentId=payment_id)


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

    # On approved, send emails/persist
    if status == "approved":
        try:
            booking = ORDER_BOOKINGS.get(order_id) or req.booking.model_dump()
            amount = compute_amount_cents(booking.get("rideId"), (booking.get("addons") or {}))
            charge_id = payments[0].get("id") if payments else order.get("id") or order_id
            # Persist booking and finalize slot
            try:
                book_slot(booking.get('rideId'), booking.get('date'), booking.get('time'))
            except Exception:
                pass
            try:
                _persist_booking_and_notify(booking, amount, charge_id, status='approved')
            except Exception:
                pass
            if settings.email_to:
                admin_body = format_payment_admin_email(booking, amount, charge_id, status)
                send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=booking.get("email"))
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
