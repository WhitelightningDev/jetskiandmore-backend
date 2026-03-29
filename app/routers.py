from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import secrets
import string
import time
import csv
import io
from zoneinfo import ZoneInfo
import hashlib

import httpx
import re
import jwt
from bson import ObjectId
from bson.binary import Binary
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from fastapi.responses import Response

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
    format_boat_ride_email,
    BOAT_RIDE_EMAIL,
)
from .pricing import compute_amount_cents
from .marketing_advisor import send_advisor_email
from .partner_pack_pdf import generate_partner_pack_pdf
from .schemas import (
    AnalyticsSummaryResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    BookingAdminResponse,
    BookingControlsResponse,
    BookingControlsUpdateRequest,
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
    PageViewAnalyticsResponse,
    PageViewAnalyticsItem,
    CountStat,
    PageViewBreakdowns,
    TimeOfDayStat,
    ReturningStat,
    MarketingAudience,
    MarketingCampaignCreateRequest,
    MarketingCampaignUpdateRequest,
    MarketingCampaignResponse,
    MarketingCampaignListResponse,
    MarketingRecipientsExportRequest,
    MarketingRecipientsExportResponse,
    MarketingRecipientsPreviewResponse,
    MarketingSendTestRequest,
    MarketingAudienceSummaryResponse,
    MarketingEmailEventListResponse,
    MarketingEmailEventResponse,
	    MarketingSendStatsResponse,
	    MarketingInsightsResponse,
	    MarketingAdvisorStatusResponse,
	    MarketingAdvisorSendTestRequest,
	    HolidayItem,
	    CampaignIdea,
    HourStat,
    DayOfWeekStat,
    MarketingManualRecipientsUploadResponse,
    MarketingManualRecipientsListResponse,
    MarketingAssetResponse,
    MarketingAssetListResponse,
)
from .yoco import YocoError, _get_oauth_token, create_charge
import uuid
from urllib.parse import urlencode


router = APIRouter(prefix="/api")

# In-memory mapping from order_id -> booking dict for webhook/verification
ORDER_BOOKINGS: dict[str, dict] = {}
CHECKOUT_BOOKINGS: dict[str, dict] = {}
INDEMNITY_PATH = "/indemnity"

DEFAULT_BOOKING_CONTROLS = {
    "jetSkiBookingsEnabled": False,
    "boatRideBookingsEnabled": True,
    "fishingChartersBookingsEnabled": True,
}


def _load_booking_controls() -> tuple[dict, Optional[datetime]]:
    """Load booking toggle flags from MongoDB (best-effort).

    Falls back to DEFAULT_BOOKING_CONTROLS if DB is unavailable or unset.
    """
    try:
        db = get_db()
        doc = db.site_settings.find_one({"key": "booking_controls"}) or {}
        controls = {
            "jetSkiBookingsEnabled": bool(doc.get("jetSkiBookingsEnabled", DEFAULT_BOOKING_CONTROLS["jetSkiBookingsEnabled"])),
            "boatRideBookingsEnabled": bool(doc.get("boatRideBookingsEnabled", DEFAULT_BOOKING_CONTROLS["boatRideBookingsEnabled"])),
            "fishingChartersBookingsEnabled": bool(doc.get("fishingChartersBookingsEnabled", DEFAULT_BOOKING_CONTROLS["fishingChartersBookingsEnabled"])),
        }
        return controls, doc.get("updatedAt")
    except Exception:
        return dict(DEFAULT_BOOKING_CONTROLS), None


def _require_enabled(flag: str, message: str) -> None:
    controls, _ = _load_booking_controls()
    if not bool(controls.get(flag)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)


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


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return None


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_user_agent(ua: Optional[str]) -> dict[str, Optional[str]]:
    ua = ua or ""
    ua_lower = ua.lower()
    device_type = "Desktop"
    if any(k in ua_lower for k in ["mobi", "android", "iphone"]):
        device_type = "Mobile"
    if "ipad" in ua_lower or "tablet" in ua_lower:
        device_type = "Tablet"

    os = None
    if "windows" in ua_lower:
        os = "Windows"
    elif "mac os" in ua_lower or "macintosh" in ua_lower:
        os = "Mac"
    elif "android" in ua_lower:
        os = "Android"
    elif "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
        os = "iOS"
    elif "linux" in ua_lower:
        os = "Linux"

    browser = None
    if "edg" in ua_lower:
        browser = "Edge"
    elif "chrome" in ua_lower and "safari" in ua_lower:
        browser = "Chrome"
    elif "safari" in ua_lower and "chrome" not in ua_lower:
        browser = "Safari"
    elif "firefox" in ua_lower:
        browser = "Firefox"
    elif "brave" in ua_lower:
        browser = "Brave"

    return {
        "device_type": device_type,
        "os": os,
        "browser": browser,
    }


# --- Public/admin booking toggle controls ---


@router.get("/booking-controls", response_model=BookingControlsResponse)
def booking_controls_public():
    controls, updated_at = _load_booking_controls()
    return BookingControlsResponse(**controls, updatedAt=updated_at)


@router.get("/admin/booking-controls", response_model=BookingControlsResponse)
def admin_get_booking_controls(admin: str = Depends(get_current_admin)):
    controls, updated_at = _load_booking_controls()
    return BookingControlsResponse(**controls, updatedAt=updated_at)


@router.patch("/admin/booking-controls", response_model=BookingControlsResponse)
def admin_update_booking_controls(req: BookingControlsUpdateRequest, admin: str = Depends(get_current_admin)):
    update: dict[str, Any] = {}
    data = req.model_dump(exclude_unset=True)
    for key in ("jetSkiBookingsEnabled", "boatRideBookingsEnabled", "fishingChartersBookingsEnabled"):
        if key in data and data[key] is not None:
            update[key] = bool(data[key])

    if not update:
        controls, updated_at = _load_booking_controls()
        return BookingControlsResponse(**controls, updatedAt=updated_at)

    now = datetime.utcnow()
    try:
        db = get_db()
        db.site_settings.update_one(
            {"key": "booking_controls"},
            {
                "$set": {**update, "updatedAt": now, "updatedBy": admin},
                "$setOnInsert": {"key": "booking_controls", "createdAt": now},
            },
            upsert=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update booking controls: {e}")

    controls, updated_at = _load_booking_controls()
    return BookingControlsResponse(**controls, updatedAt=updated_at)


# --- Public partner pack PDF (brochure) ---


@router.get("/partner-pack.pdf")
def partner_pack_pdf(partnerCode: Optional[str] = None, property: Optional[str] = None):
    pdf = generate_partner_pack_pdf(
        site_base_url=(settings.site_base_url or "https://www.jetskiandmore.com"),
        partner_code=partnerCode,
        property_name=property,
        commission_percent=20,
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="jet-ski-and-more-partner-pack.pdf"',
            "Cache-Control": "no-store",
        },
    )


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
    data = req.model_dump()
    is_boat = str(req.type or "").strip().lower() == "boat-ride"

    if is_boat:
        _require_enabled("boatRideBookingsEnabled", "Boat ride bookings are currently closed")
        to_address = req.targetEmail or BOAT_RIDE_EMAIL
        subject = req.subject or "Boat ride request"
        body_html = format_boat_ride_email(data)
    else:
        if not settings.email_to:
            raise HTTPException(status_code=500, detail="Email recipient not configured")
        to_address = settings.email_to
        subject = req.subject or "New contact message"
        body_html = format_contact_email(data)

    try:
        ok = send_email(subject=subject, body=body_html, body_html=body_html, to_address=to_address, reply_to=req.email)
        if not ok:
            raise RuntimeError("SMTP send returned False")
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
    _require_enabled("jetSkiBookingsEnabled", "Jet ski bookings are currently closed")
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
    _require_enabled("jetSkiBookingsEnabled", "Jet ski bookings are currently closed")
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


@router.get("/admin/analytics/pageviews", response_model=PageViewAnalyticsResponse)
def admin_page_view_analytics(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 50,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    start_dt = _parse_iso_datetime(start)
    end_dt = _parse_iso_datetime(end)

    match_filter: Dict[str, Any] = {}
    created_range: Dict[str, Any] = {}
    if start_dt:
        created_range["$gte"] = start_dt
    if end_dt:
        created_range["$lte"] = end_dt
    if created_range:
        match_filter["created_at"] = created_range

    limit_val = max(1, min(limit, 200))
    pipeline: List[Dict[str, Any]] = []
    if match_filter:
        pipeline.append({"$match": match_filter})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$path",
                    "views": {"$sum": 1},
                    "uniqueSessions": {"$addToSet": "$session_id"},
                    "totalDurationSeconds": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$duration_seconds", None]},
                                        {"$gte": ["$duration_seconds", 0]},
                                    ]
                                },
                                "$duration_seconds",
                                0,
                            ]
                        }
                    },
                    "durationSamples": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$duration_seconds", None]},
                                        {"$gte": ["$duration_seconds", 0]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "firstSeen": {"$min": "$created_at"},
                    "lastSeen": {"$max": "$created_at"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "path": "$_id",
                    "views": 1,
                    "uniqueSessions": "$uniqueSessions",
                    "totalDurationSeconds": 1,
                    "durationSamples": 1,
                    "firstSeen": 1,
                    "lastSeen": 1,
                }
            },
            {"$sort": {"views": -1}},
            {"$limit": limit_val},
        ]
    )

    try:
        rows = list(db.page_views.aggregate(pipeline))
    except Exception:
        rows = []

    items: List[PageViewAnalyticsItem] = []
    for r in rows:
        views = int(r.get("views") or 0)
        unique_sessions_raw = r.get("uniqueSessions") or []
        unique_sessions = [s for s in unique_sessions_raw if s]
        duration_samples = int(r.get("durationSamples") or 0)
        total_duration = float(r.get("totalDurationSeconds") or 0.0)
        avg_duration = total_duration / duration_samples if duration_samples else None
        items.append(
            PageViewAnalyticsItem(
                path=str(r.get("path") or ""),
                views=views,
                uniqueSessions=len(set(unique_sessions)),
                totalDurationSeconds=total_duration,
                avgDurationSeconds=avg_duration,
                firstSeen=r.get("firstSeen"),
                lastSeen=r.get("lastSeen"),
            )
        )

    try:
        total_views = db.page_views.count_documents(match_filter or {})
    except Exception:
        total_views = 0

    try:
        session_filter = dict(match_filter)
        session_filter["session_id"] = {"$nin": [None, ""]}
        total_unique_sessions = len(db.page_views.distinct("session_id", session_filter))
    except Exception:
        total_unique_sessions = 0

    # Top-N breakdown helper
    def _top(field: str, limit_count: int = 10) -> list[CountStat]:
        agg: list[dict] = []
        if match_filter:
            agg.append({"$match": match_filter})
        agg.extend(
            [
                {"$match": {field: {"$nin": [None, ""]}}},
                {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": limit_count},
            ]
        )
        try:
            res = list(db.page_views.aggregate(agg))
        except Exception:
            res = []
        return [CountStat(key=str(r.get("_id") or ""), count=int(r.get("count") or 0)) for r in res]

    # Time of day (hour of day, 0-23) in local time (SAST)
    tod_pipeline: list[dict] = []
    if match_filter:
        tod_pipeline.append({"$match": match_filter})
    tod_pipeline.extend(
        [
            {
                "$addFields": {
                    "_local_parts": {
                        "$dateToParts": {
                            "date": "$created_at",
                            "timezone": "Africa/Johannesburg",
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": "$_local_parts.hour",
                    "views": {"$sum": 1},
                }
            },
            {"$sort": {"_id": 1}},
        ]
    )
    try:
        tod_rows = list(db.page_views.aggregate(tod_pipeline))
    except Exception:
        tod_rows = []
    time_of_day = [
        TimeOfDayStat(hour=int(r.get("_id") or 0), views=int(r.get("views") or 0)) for r in tod_rows
    ]

    # Returning vs new visitors (based on visitor_id)
    ret_pipeline: list[dict] = []
    visitor_match = dict(match_filter)
    visitor_match["visitor_id"] = {"$nin": [None, ""]}
    ret_pipeline.append({"$match": visitor_match})
    ret_pipeline.extend(
        [
            {"$group": {"_id": "$visitor_id", "views": {"$sum": 1}}},
            {
                "$group": {
                    "_id": None,
                    "newVisitors": {"$sum": {"$cond": [{"$eq": ["$views", 1]}, 1, 0]}},
                    "returningVisitors": {"$sum": {"$cond": [{"$gt": ["$views", 1]}, 1, 0]}},
                }
            },
        ]
    )
    try:
        ret_rows = list(db.page_views.aggregate(ret_pipeline))
    except Exception:
        ret_rows = []
    returning_stats = ReturningStat(newVisitors=0, returningVisitors=0, totalVisitors=0)
    if ret_rows:
        row = ret_rows[0]
        new_v = int(row.get("newVisitors") or 0)
        ret_v = int(row.get("returningVisitors") or 0)
        returning_stats = ReturningStat(
            newVisitors=new_v, returningVisitors=ret_v, totalVisitors=new_v + ret_v
        )

    try:
        visitor_filter = dict(match_filter)
        visitor_filter["visitor_id"] = {"$nin": [None, ""]}
        total_unique_visitors = len(db.page_views.distinct("visitor_id", visitor_filter))
    except Exception:
        total_unique_visitors = 0

    breakdowns = PageViewBreakdowns(
        countries=_top("country"),
        cities=_top("city"),
        deviceTypes=_top("device_type"),
        os=_top("os"),
        browsers=_top("browser"),
        languages=_top("language"),
        timeOfDay=time_of_day,
        returning=returning_stats,
    )

    return PageViewAnalyticsResponse(
        items=items,
        totalViews=total_views,
        totalUniqueSessions=total_unique_sessions,
        totalUniqueVisitors=total_unique_visitors,
        breakdowns=breakdowns,
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


# --- Admin marketing / campaigns ---


# Basic email sanity check (avoid whitespace and require a dot in the domain)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


try:
    _SAST = ZoneInfo("Africa/Johannesburg")
except Exception:
    _SAST = timezone.utc


def _localize(dt: datetime) -> datetime:
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_SAST)
    except Exception:
        return dt


def _dow_sun0(dt: datetime) -> int:
    # Python weekday(): Mon=0..Sun=6. We want Sun=0..Sat=6.
    return (dt.weekday() + 1) % 7


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm (Meeus/Jones/Butcher)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _upcoming_sa_holidays(from_dt: date, days: int = 120) -> list[HolidayItem]:
    end_dt = from_dt + timedelta(days=max(1, days))
    years = sorted({from_dt.year, end_dt.year})
    items: list[HolidayItem] = []

    def add(d: date, name: str):
        if from_dt <= d <= end_dt:
            items.append(HolidayItem(date=d.isoformat(), name=name))

    for y in years:
        add(date(y, 1, 1), "New Year's Day")
        add(date(y, 3, 21), "Human Rights Day")
        add(date(y, 4, 27), "Freedom Day")
        add(date(y, 5, 1), "Workers' Day")
        add(date(y, 6, 16), "Youth Day")
        add(date(y, 8, 9), "National Women's Day")
        add(date(y, 9, 24), "Heritage Day")
        add(date(y, 12, 16), "Day of Reconciliation")
        add(date(y, 12, 25), "Christmas Day")
        add(date(y, 12, 26), "Day of Goodwill")

        easter = _easter_sunday(y)
        add(easter - timedelta(days=2), "Good Friday")
        add(easter + timedelta(days=1), "Family Day (Easter Monday)")

    items.sort(key=lambda x: x.date)
    return items[:12]


def _log_marketing_email_event(
    *,
    campaign_id: str,
    email: str,
    kind: str,
    ok: bool,
    subject: Optional[str],
    sent_at: datetime,
    admin: str,
    error: Optional[str] = None,
    run_id: Optional[str] = None,
):
    try:
        db = get_db()
        db.marketing_email_events.insert_one(
            {
                "campaignId": campaign_id,
                "email": email,
                "kind": kind,
                "ok": bool(ok),
                "error": error,
                "subject": subject,
                "sentAt": sent_at,
                "createdBy": admin,
                "runId": run_id,
            }
        )
    except Exception:
        return


ASSET_ID_RE = re.compile(r"/api/marketing/assets/([0-9a-fA-F]{24})")


def _prepare_inline_assets(html: str) -> tuple[str, list[dict]]:
    """Replace our hosted asset URLs in HTML with cid: references and return inline image payloads."""
    db = get_db()
    raw_html = str(html or "")
    ids = list({m.group(1) for m in ASSET_ID_RE.finditer(raw_html)})
    if not ids:
        return raw_html, []
    inline: list[dict] = []

    out_html = raw_html
    for asset_id in ids:
        try:
            oid = ObjectId(asset_id)
        except Exception:
            continue
        doc = db.marketing_assets.find_one({"_id": oid})
        if not doc:
            continue
        data = doc.get("data")
        content_type = str(doc.get("contentType") or "application/octet-stream")
        if not data or not content_type.startswith("image/"):
            continue
        cid = f"asset-{asset_id}"
        inline.append({"cid": cid, "contentType": content_type, "data": bytes(data)})

        asset_path = f"/api/marketing/assets/{asset_id}"
        # Replace src="https://host/.../api/marketing/assets/<id>" OR src="/api/marketing/assets/<id>"
        out_html = re.sub(
            r'(src=["\'])(?:https?://[^"\']+)?' + re.escape(asset_path) + r'(["\'])',
            rf"\1cid:{cid}\2",
            out_html,
            flags=re.IGNORECASE,
        )
    return out_html, inline


def _serialize_campaign(doc: Dict[str, Any]) -> MarketingCampaignResponse:
    stats = doc.get("stats") or None
    return MarketingCampaignResponse(
        id=str(doc.get("_id")),
        name=str(doc.get("name") or ""),
        subject=str(doc.get("subject") or ""),
        preheader=doc.get("preheader"),
        content=doc.get("content"),
        ctaLabel=doc.get("ctaLabel"),
        ctaUrl=doc.get("ctaUrl"),
        audience=MarketingAudience(**(doc.get("audience") or {})) if doc.get("audience") else None,
        html=doc.get("html"),
        status=str(doc.get("status") or "draft"),
        createdAt=doc.get("createdAt"),
        updatedAt=doc.get("updatedAt"),
        sentAt=doc.get("sentAt"),
        stats=stats,
    )


def _build_recipients(audience: Optional[Dict[str, Any]] = None) -> list[str]:
    db = get_db()
    aud = audience or {}
    ride_id = str(aud.get("rideId") or "").strip() or None
    status_filter = str(aud.get("status") or "").strip() or None
    include_manual = aud.get("includeManual")
    if include_manual is None:
        include_manual_bool = True
    else:
        include_manual_bool = bool(include_manual)
    last_n_days = aud.get("lastNDays")
    try:
        last_n_days_int = int(last_n_days) if last_n_days is not None else None
    except Exception:
        last_n_days_int = None

    query: Dict[str, Any] = {}
    if ride_id:
        query["rideId"] = ride_id
    if status_filter:
        query["status"] = status_filter
    if last_n_days_int and last_n_days_int > 0:
        cutoff = datetime.utcnow() - timedelta(days=last_n_days_int)
        # Support both legacy and current timestamp keys
        query["$or"] = [{"createdAt": {"$gte": cutoff}}, {"created_at": {"$gte": cutoff}}]

    cursor = db.bookings.find(query, {"email": 1}).limit(20000)
    out: list[str] = []
    seen: set[str] = set()
    for doc in cursor:
        email = str(doc.get("email") or "").strip().lower()
        if not email or not EMAIL_RE.match(email):
            continue
        if email in seen:
            continue
        seen.add(email)
        out.append(email)

    if include_manual_bool:
        try:
            manual_cursor = db.marketing_manual_recipients.find({}, {"email": 1}).limit(50000)
            for doc in manual_cursor:
                email = str(doc.get("email") or "").strip().lower()
                if not email or not EMAIL_RE.match(email):
                    continue
                if email in seen:
                    continue
                seen.add(email)
                out.append(email)
        except Exception:
            pass
    # Stable ordering makes batching deterministic
    try:
        return sorted(out)
    except Exception:
        return out


@router.get("/admin/marketing/campaigns", response_model=MarketingCampaignListResponse)
def admin_list_campaigns(limit: int = 50, admin: str = Depends(get_current_admin)):
    db = get_db()
    limit_val = max(1, min(int(limit or 50), 200))
    docs = list(db.marketing_campaigns.find({}).sort("updatedAt", -1).limit(limit_val))
    return MarketingCampaignListResponse(items=[_serialize_campaign(d) for d in docs])


@router.post("/admin/marketing/campaigns", response_model=MarketingCampaignResponse)
def admin_create_campaign(payload: MarketingCampaignCreateRequest, admin: str = Depends(get_current_admin)):
    db = get_db()
    now = datetime.utcnow()
    doc = payload.model_dump()
    doc["status"] = "draft"
    doc["createdAt"] = now
    doc["updatedAt"] = now
    doc["createdBy"] = admin
    res = db.marketing_campaigns.insert_one(doc)
    saved = db.marketing_campaigns.find_one({"_id": res.inserted_id})
    return _serialize_campaign(saved or {**doc, "_id": res.inserted_id})


@router.put("/admin/marketing/campaigns/{campaign_id}", response_model=MarketingCampaignResponse)
def admin_update_campaign(
    campaign_id: str,
    payload: MarketingCampaignUpdateRequest,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Campaign not found")
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        doc = db.marketing_campaigns.find_one({"_id": oid})
        if not doc:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return _serialize_campaign(doc)
    updates["updatedAt"] = datetime.utcnow()
    updates["updatedBy"] = admin
    res = db.marketing_campaigns.update_one({"_id": oid}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Campaign not found")
    doc = db.marketing_campaigns.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return _serialize_campaign(doc)


@router.delete("/admin/marketing/campaigns/{campaign_id}")
def admin_delete_campaign(campaign_id: str, admin: str = Depends(get_current_admin)):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Campaign not found")
    res = db.marketing_campaigns.delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"ok": True}


@router.get("/admin/marketing/campaigns/{campaign_id}/recipients-preview", response_model=MarketingRecipientsPreviewResponse)
def admin_campaign_recipients_preview(campaign_id: str, admin: str = Depends(get_current_admin)):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign = db.marketing_campaigns.find_one({"_id": oid})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    recipients = _build_recipients(campaign.get("audience") or None)
    return MarketingRecipientsPreviewResponse(count=len(recipients), sample=recipients[:10])


@router.post("/admin/marketing/campaigns/{campaign_id}/send-test")
def admin_send_test_campaign(
    campaign_id: str,
    payload: MarketingSendTestRequest,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign = db.marketing_campaigns.find_one({"_id": oid})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    subject = str(campaign.get("subject") or campaign.get("name") or "Jet Ski & More").strip()
    html = campaign.get("html") or campaign.get("content") or ""
    html_to_send, inline_images = _prepare_inline_assets(str(html))
    sent_at = datetime.utcnow()
    try:
        ok = send_email(
            subject=subject,
            body=str(html_to_send),
            body_html=str(html_to_send),
            to_address=str(payload.toEmail),
            inline_images=inline_images,
        )
        if not ok:
            raise RuntimeError("SMTP send returned False")
        _log_marketing_email_event(
            campaign_id=str(oid),
            email=str(payload.toEmail).strip().lower(),
            kind="test",
            ok=True,
            subject=subject,
            sent_at=sent_at,
            admin=admin,
        )
    except Exception as e:
        _log_marketing_email_event(
            campaign_id=str(oid),
            email=str(payload.toEmail).strip().lower(),
            kind="test",
            ok=False,
            subject=subject,
            sent_at=sent_at,
            admin=admin,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Email send failed: {e}")
    return {"ok": True}


@router.post("/admin/marketing/campaigns/{campaign_id}/send", response_model=MarketingCampaignResponse)
def admin_send_campaign(
    campaign_id: str,
    offset: Optional[int] = None,
    batchSize: int = 300,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    try:
        oid = ObjectId(campaign_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign = db.marketing_campaigns.find_one({"_id": oid})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    recipients = _build_recipients(campaign.get("audience") or None)
    total_recipients = len(recipients)
    if total_recipients == 0:
        raise HTTPException(status_code=400, detail="No recipients for this audience filter")

    subject = str(campaign.get("subject") or campaign.get("name") or "Jet Ski & More").strip()
    html = str(campaign.get("html") or campaign.get("content") or "").strip()
    if not html:
        raise HTTPException(status_code=400, detail="Campaign has no content")
    html_to_send, inline_images = _prepare_inline_assets(html)

    max_batch = 300
    try:
        batch_size = int(batchSize or max_batch)
    except Exception:
        batch_size = max_batch
    batch_size = max(1, min(batch_size, max_batch))

    prev_offset = 0
    try:
        prev_offset = int(campaign.get("sendOffset") or 0)
    except Exception:
        prev_offset = 0
    if prev_offset <= 0:
        # Backwards compat: older sends didn't track a cursor; use attempted as a best-effort cursor.
        try:
            st = campaign.get("stats") or {}
            attempted_prev = int(st.get("attempted") or 0)
            if attempted_prev > 0 and str(campaign.get("status") or "").strip().lower() in ("sent", "sending"):
                prev_offset = attempted_prev
        except Exception:
            pass

    if offset is None:
        offset_val = prev_offset
    else:
        try:
            offset_val = max(0, int(offset))
        except Exception:
            offset_val = prev_offset

    # Avoid accidental duplicate sends if the UI is stale.
    if offset_val != prev_offset:
        raise HTTPException(
            status_code=409,
            detail=f"Batch cursor mismatch. Refresh and try again. Expected offset {prev_offset}.",
        )

    if offset_val >= total_recipients:
        # Nothing left to send
        now = datetime.utcnow()
        stats_prev = campaign.get("stats") or {}
        stats = {
            "attempted": int(stats_prev.get("attempted") or 0),
            "sent": int(stats_prev.get("sent") or 0),
            "failed": int(stats_prev.get("failed") or 0),
            "totalRecipients": total_recipients,
            "remainingRecipients": 0,
            "batchOffset": offset_val,
            "batchSize": 0,
            "lastBatchAttempted": 0,
            "lastBatchSent": 0,
            "lastBatchFailed": 0,
        }
        db.marketing_campaigns.update_one(
            {"_id": oid},
            {"$set": {"status": "sent", "sentAt": campaign.get("sentAt") or now, "updatedAt": now, "updatedBy": admin, "stats": stats, "sendOffset": offset_val}},
        )
        updated = db.marketing_campaigns.find_one({"_id": oid}) or campaign
        return _serialize_campaign(updated)

    batch = recipients[offset_val : offset_val + batch_size]

    attempted = 0
    sent = 0
    failed = 0
    run_id = uuid.uuid4().hex
    event_docs: list[dict[str, Any]] = []
    for email in batch:
        attempted += 1
        sent_at = datetime.utcnow()
        try:
            ok = send_email(
                subject=subject,
                body=html_to_send,
                body_html=html_to_send,
                to_address=email,
                inline_images=inline_images,
            )
            if ok:
                sent += 1
                event_docs.append(
                    {
                        "campaignId": str(oid),
                        "email": email,
                        "kind": "bulk",
                        "ok": True,
                        "error": None,
                        "subject": subject,
                        "sentAt": sent_at,
                        "createdBy": admin,
                        "runId": run_id,
                    }
                )
            else:
                failed += 1
                event_docs.append(
                    {
                        "campaignId": str(oid),
                        "email": email,
                        "kind": "bulk",
                        "ok": False,
                        "error": "SMTP send returned False",
                        "subject": subject,
                        "sentAt": sent_at,
                        "createdBy": admin,
                        "runId": run_id,
                    }
                )
        except Exception as e:
            failed += 1
            event_docs.append(
                {
                    "campaignId": str(oid),
                    "email": email,
                    "kind": "bulk",
                    "ok": False,
                    "error": str(e),
                    "subject": subject,
                    "sentAt": sent_at,
                    "createdBy": admin,
                    "runId": run_id,
                }
            )
        time.sleep(0.2)

    try:
        if event_docs:
            db.marketing_email_events.insert_many(event_docs, ordered=False)
    except Exception:
        pass

    now = datetime.utcnow()
    next_offset = offset_val + len(batch)
    remaining = max(0, total_recipients - next_offset)

    prev_stats = campaign.get("stats") or {}
    total_attempted = int(prev_stats.get("attempted") or 0) + attempted
    total_sent = int(prev_stats.get("sent") or 0) + sent
    total_failed = int(prev_stats.get("failed") or 0) + failed
    status_val = "sent" if remaining == 0 else "sending"
    sent_at_val = now if status_val == "sent" else campaign.get("sentAt")

    db.marketing_campaigns.update_one(
        {"_id": oid},
        {
            "$set": {
                "status": status_val,
                "sentAt": sent_at_val,
                "updatedAt": now,
                "updatedBy": admin,
                "stats": {
                    "attempted": total_attempted,
                    "sent": total_sent,
                    "failed": total_failed,
                    "totalRecipients": total_recipients,
                    "remainingRecipients": remaining,
                    "batchOffset": offset_val,
                    "batchSize": len(batch),
                    "lastBatchAttempted": attempted,
                    "lastBatchSent": sent,
                    "lastBatchFailed": failed,
                },
                "sendOffset": next_offset,
            }
        },
    )
    updated = db.marketing_campaigns.find_one({"_id": oid}) or campaign
    return _serialize_campaign(updated)


@router.get("/admin/marketing/audience/summary", response_model=MarketingAudienceSummaryResponse)
def admin_marketing_audience_summary(admin: str = Depends(get_current_admin)):
    db = get_db()
    docs = list(db.bookings.find({}, {"email": 1, "rideId": 1, "createdAt": 1, "created_at": 1}).limit(50000))
    now = datetime.utcnow()
    cutoff30 = now - timedelta(days=30)
    cutoff90 = now - timedelta(days=90)
    seen: set[str] = set()
    seen30: set[str] = set()
    seen90: set[str] = set()
    by_ride: dict[str, set[str]] = {}
    domains: dict[str, int] = {}

    for d in docs:
        email = str(d.get("email") or "").strip().lower()
        if not email or not EMAIL_RE.match(email):
            continue
        ride_id = str(d.get("rideId") or "").strip() or "unknown"
        by_ride.setdefault(ride_id, set()).add(email)
        domain = email.split("@")[-1] if "@" in email else ""
        if domain:
            domains[domain] = domains.get(domain, 0) + 1

        seen.add(email)
        created = d.get("createdAt") or d.get("created_at")
        if created:
            if isinstance(created, datetime):
                created_dt = created
            else:
                created_dt = None
                try:
                    created_dt = datetime.fromisoformat(str(created))
                except Exception:
                    created_dt = None
            if created_dt:
                if created_dt >= cutoff90:
                    seen90.add(email)
                if created_dt >= cutoff30:
                    seen30.add(email)

    by_ride_stats = sorted(
        [CountStat(key=k, count=len(v)) for k, v in by_ride.items()],
        key=lambda x: x.count,
        reverse=True,
    )[:20]
    top_domains = sorted(
        [CountStat(key=k, count=v) for k, v in domains.items()],
        key=lambda x: x.count,
        reverse=True,
    )[:12]

    return MarketingAudienceSummaryResponse(
        totalUniqueEmails=len(seen),
        uniqueEmailsLast30Days=len(seen30),
        uniqueEmailsLast90Days=len(seen90),
        byRide=by_ride_stats,
        topDomains=top_domains,
    )


@router.post("/admin/marketing/recipients/export", response_model=MarketingRecipientsExportResponse)
def admin_export_recipients(payload: MarketingRecipientsExportRequest, admin: str = Depends(get_current_admin)):
    emails = _build_recipients(payload.model_dump(exclude_unset=True))
    return MarketingRecipientsExportResponse(emails=emails)


def _serialize_marketing_asset(doc: Dict[str, Any]) -> MarketingAssetResponse:
    oid = doc.get("_id")
    asset_id = str(oid) if oid is not None else ""
    filename = str(doc.get("filename") or "image")
    content_type = str(doc.get("contentType") or "application/octet-stream")
    size = int(doc.get("size") or 0)
    return MarketingAssetResponse(
        id=asset_id,
        filename=filename,
        contentType=content_type,
        size=size,
        url=f"/api/marketing/assets/{asset_id}",
        createdAt=doc.get("createdAt"),
    )


@router.get("/marketing/assets/{asset_id}")
def marketing_get_asset(asset_id: str):
    db = get_db()
    try:
        oid = ObjectId(asset_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Asset not found")
    doc = db.marketing_assets.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Asset not found")
    data = doc.get("data")
    if data is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    content_type = str(doc.get("contentType") or "application/octet-stream")
    if not content_type.startswith("image/"):
        content_type = "application/octet-stream"
    content = bytes(data)
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    return Response(content=content, media_type=content_type, headers=headers)


@router.get("/admin/marketing/assets", response_model=MarketingAssetListResponse)
def admin_list_marketing_assets(limit: int = 50, admin: str = Depends(get_current_admin)):
    db = get_db()
    lim = max(1, min(int(limit or 50), 200))
    docs = list(db.marketing_assets.find({}, {"data": 0}).sort("createdAt", -1).limit(lim))
    return MarketingAssetListResponse(items=[_serialize_marketing_asset(d) for d in docs])


@router.post("/admin/marketing/assets/upload", response_model=MarketingAssetResponse)
async def admin_upload_marketing_asset(
    file: UploadFile = File(...),
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    content_type = str(file.content_type or "").strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are supported")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    max_bytes = 5 * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(status_code=400, detail="Image too large (max 5MB)")

    filename = str(file.filename or "image").strip() or "image"
    sha = hashlib.sha256(raw).hexdigest()
    now = datetime.utcnow()
    doc = {
        "filename": filename,
        "contentType": content_type,
        "size": len(raw),
        "sha256": sha,
        "data": Binary(raw),
        "createdAt": now,
        "createdBy": admin,
        "updatedAt": now,
        "updatedBy": admin,
    }
    res = db.marketing_assets.insert_one(doc)
    saved = db.marketing_assets.find_one({"_id": res.inserted_id}, {"data": 0}) or {**doc, "_id": res.inserted_id}
    return _serialize_marketing_asset(saved)


@router.get("/admin/marketing/manual-recipients", response_model=MarketingManualRecipientsListResponse)
def admin_list_manual_recipients(limit: int = 20000, admin: str = Depends(get_current_admin)):
    db = get_db()
    lim = max(1, min(int(limit or 20000), 50000))
    docs = list(db.marketing_manual_recipients.find({}, {"email": 1}).sort("createdAt", -1).limit(lim))
    emails = []
    for d in docs:
        email = str(d.get("email") or "").strip().lower()
        if not email or not EMAIL_RE.match(email):
            continue
        emails.append(email)
    # De-dupe while preserving order
    seen: set[str] = set()
    unique = []
    for e in emails:
        if e in seen:
            continue
        seen.add(e)
        unique.append(e)
    total = 0
    try:
        total = int(db.marketing_manual_recipients.count_documents({}))
    except Exception:
        total = len(unique)
    return MarketingManualRecipientsListResponse(emails=unique, total=total)


@router.post("/admin/marketing/manual-recipients/upload", response_model=MarketingManualRecipientsUploadResponse)
async def admin_upload_manual_recipients(
    file: UploadFile = File(...),
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig", errors="ignore")
    except Exception:
        text = (raw or b"").decode("utf-8", errors="ignore")

    # Parse CSV (supports: header with email column OR one email per line)
    rows: list[str] = []
    invalid = 0

    stream = io.StringIO(text)
    try:
        reader = csv.reader(stream)
        parsed = list(reader)
    except Exception:
        parsed = []

    def _norm_col(col: str) -> str:
        c = str(col or "").strip().lower()
        # "Address 1 - State/Region" -> "address 1 state region"
        c = re.sub(r"[^a-z0-9]+", " ", c).strip()
        c = re.sub(r"\s+", " ", c)
        return c

    if parsed and parsed[0] and any("email" in _norm_col(c) or "e mail" in _norm_col(c) for c in parsed[0]):
        header = [_norm_col(c) for c in parsed[0]]
        email_cols: list[int] = []
        for i, col in enumerate(header):
            if ("email" in col or "e mail" in col) and "type" not in col:
                email_cols.append(i)
        for r in parsed[1:]:
            if not r:
                continue
            if email_cols:
                for idx in email_cols:
                    if idx < len(r):
                        rows.append(str(r[idx] or ""))
            else:
                for cell in r:
                    rows.append(str(cell or ""))
    else:
        # Fallback: split by lines and commas
        for line in text.splitlines():
            for part in line.split(","):
                rows.append(part)

    seen: set[str] = set()
    now = datetime.utcnow()
    ops = []
    for r in rows:
        email = str(r or "").strip().strip('"').strip("'").lower()
        if not email:
            continue
        if email in seen:
            continue
        seen.add(email)
        if not EMAIL_RE.match(email):
            invalid += 1
            continue
        ops.append(
            {
                "updateOne": {
                    "filter": {"email": email},
                    "update": {
                        "$setOnInsert": {"createdAt": now, "createdBy": admin},
                        "$set": {"email": email, "updatedAt": now, "updatedBy": admin},
                    },
                    "upsert": True,
                }
            }
        )

    added = 0
    if ops:
        try:
            res = db.marketing_manual_recipients.bulk_write(ops, ordered=False)
            added = int(res.upserted_count or 0)
        except Exception:
            # Best-effort fallback
            for op in ops:
                try:
                    filt = op["updateOne"]["filter"]
                    upd = op["updateOne"]["update"]
                    r = db.marketing_manual_recipients.update_one(filt, upd, upsert=True)
                    if r.upserted_id:
                        added += 1
                except Exception:
                    continue

    total = 0
    try:
        total = int(db.marketing_manual_recipients.count_documents({}))
    except Exception:
        total = 0
    return MarketingManualRecipientsUploadResponse(added=added, total=total, invalid=invalid)


@router.get("/admin/marketing/email-events", response_model=MarketingEmailEventListResponse)
def admin_marketing_email_events(
    campaignId: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    q: Dict[str, Any] = {}
    if campaignId:
        q["campaignId"] = str(campaignId).strip()
    if kind:
        q["kind"] = str(kind).strip()
    lim = max(1, min(int(limit or 100), 500))
    sk = max(0, int(skip or 0))
    docs = list(db.marketing_email_events.find(q).sort("sentAt", -1).skip(sk).limit(lim))
    items: list[MarketingEmailEventResponse] = []
    for d in docs:
        sent_at = d.get("sentAt")
        if not isinstance(sent_at, datetime):
            sent_at = datetime.utcnow()
        items.append(
            MarketingEmailEventResponse(
                id=str(d.get("_id")),
                campaignId=str(d.get("campaignId") or ""),
                email=str(d.get("email") or ""),
                kind=str(d.get("kind") or ""),
                ok=bool(d.get("ok")),
                error=d.get("error"),
                subject=d.get("subject"),
                sentAt=sent_at,
            )
        )
    return MarketingEmailEventListResponse(items=items)


@router.get("/admin/marketing/send-stats", response_model=MarketingSendStatsResponse)
def admin_marketing_send_stats(
    days: int = 90,
    kind: Optional[str] = None,
    campaignId: Optional[str] = None,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    d = max(1, min(int(days or 90), 365))
    cutoff = datetime.utcnow() - timedelta(days=d)
    q: Dict[str, Any] = {"sentAt": {"$gte": cutoff}}
    if kind:
        q["kind"] = str(kind).strip()
    if campaignId:
        q["campaignId"] = str(campaignId).strip()

    docs = list(db.marketing_email_events.find(q, {"ok": 1, "sentAt": 1}))
    total_attempted = len(docs)
    total_sent = 0
    total_failed = 0

    by_hour: dict[int, int] = {h: 0 for h in range(24)}
    by_dow: dict[int, int] = {i: 0 for i in range(7)}

    for ev in docs:
        ok = bool(ev.get("ok"))
        if ok:
            total_sent += 1
        else:
            total_failed += 1
        sent_at = ev.get("sentAt")
        if not isinstance(sent_at, datetime):
            continue
        local_dt = _localize(sent_at)
        by_hour[int(local_dt.hour)] = by_hour.get(int(local_dt.hour), 0) + 1
        by_dow[_dow_sun0(local_dt)] = by_dow.get(_dow_sun0(local_dt), 0) + 1

    return MarketingSendStatsResponse(
        totalAttempted=total_attempted,
        totalSent=total_sent,
        totalFailed=total_failed,
        byHour=[HourStat(hour=h, count=by_hour.get(h, 0)) for h in range(24)],
        byDayOfWeek=[DayOfWeekStat(day=i, count=by_dow.get(i, 0)) for i in range(7)],
    )


@router.get("/admin/marketing/insights", response_model=MarketingInsightsResponse)
def admin_marketing_insights(
    industry: Optional[str] = None,
    location: Optional[str] = None,
    lookbackDays: int = 180,
    admin: str = Depends(get_current_admin),
):
    db = get_db()
    ind = (industry or "Jet ski rentals & water activities").strip()
    loc = (location or "South Africa (Africa/Johannesburg)").strip()
    lb = max(14, min(int(lookbackDays or 180), 730))

    booking_controls, _ = _load_booking_controls()
    jet_ski_open = bool(booking_controls.get("jetSkiBookingsEnabled"))

    cutoff = datetime.utcnow() - timedelta(days=lb)
    bookings = list(
        db.bookings.find(
            {"$or": [{"createdAt": {"$gte": cutoff}}, {"created_at": {"$gte": cutoff}}]},
            {"createdAt": 1, "created_at": 1, "rideId": 1, "status": 1},
        ).limit(100000)
    )

    booking_by_hour: dict[int, int] = {h: 0 for h in range(24)}
    booking_by_dow: dict[int, int] = {i: 0 for i in range(7)}
    ride_counts: dict[str, int] = {}

    for b in bookings:
        created = b.get("createdAt") or b.get("created_at")
        if not isinstance(created, datetime):
            continue
        local_dt = _localize(created)
        booking_by_hour[int(local_dt.hour)] = booking_by_hour.get(int(local_dt.hour), 0) + 1
        booking_by_dow[_dow_sun0(local_dt)] = booking_by_dow.get(_dow_sun0(local_dt), 0) + 1
        ride_id = str(b.get("rideId") or "").strip() or "unknown"
        ride_counts[ride_id] = ride_counts.get(ride_id, 0) + 1

    sorted_hours = sorted(booking_by_hour.items(), key=lambda x: x[1], reverse=True)
    peak_hours = [h for h, c in sorted_hours[:4] if c > 0]
    rec: list[int] = []
    for h in peak_hours:
        candidate = (h - 2) % 24
        if 8 <= candidate <= 19 and candidate not in rec:
            rec.append(candidate)
    if not rec:
        rec = [10, 12, 15, 18]

    today_local = datetime.now(_SAST).date()
    holidays = _upcoming_sa_holidays(today_local, days=140)

    what_to_send = (
        [
            "Winter/off-season updates (what’s changing and what you’re improving)",
            "Safety & compliance credibility content (briefings, onboarding, procedures)",
            "Gift vouchers / early-access list for spring/summer",
            "Partner-facing updates (tourism desks, hotels, corporate planners)",
            "UGC requests (reviews/photos) from past riders to keep visibility",
        ]
        if not jet_ski_open
        else [
            "Weather-aware availability updates (clear cancellation/reschedule policy)",
            "Limited-slot reminders for weekends and public holidays",
            "Family/group bundles (e.g., 2–5 jet-skis) with simple pricing",
            "Gift voucher / birthday experience messaging",
            "UGC requests: ask for photos/reviews after a successful ride",
        ]
    )
    what_not_to_send = [
        "Deep discounts without a clear limit (can erode premium perception)",
        "Late-night sends (after 20:00) or very early sends (before 07:00)",
        "Over-promising sea conditions (keep safety-first wording)",
        "Too many emails in a short window (avoid daily blasts)",
    ]

    base_url = _site_base()
    booking_url = f"{base_url}/book" if jet_ski_open else f"{base_url}/contact"
    booking_cta = "View availability" if jet_ski_open else "Join early access"
    top_ride_ids = [k for k, _ in sorted(ride_counts.items(), key=lambda x: x[1], reverse=True)[:3]]
    ride_hint = ", ".join(top_ride_ids) if top_ride_ids else "your preferred ride"

    ideas: list[CampaignIdea] = []
    if holidays and jet_ski_open:
        h0 = holidays[0]
        ideas.append(
            CampaignIdea(
                title=f"{h0.name} — Limited slots",
                subject=f"{h0.name}: secure your Jet Ski slot",
                preheader="Popular times sell out — book early for the best options.",
                content=(
                    f"Hi there,\n\n{h0.name} is coming up on {h0.date}. If you're planning a day on the water, "
                    "we recommend booking ahead so you can choose the best time.\n\n"
                    "Safety-first operations, clear briefing, and commercial-grade procedures.\n\n"
                    "Book now to lock in your slot."
                ),
                ctaLabel="Book your slot",
                ctaUrl=booking_url,
                audience=MarketingAudience(lastNDays=365),
            )
        )
    elif holidays and not jet_ski_open:
        h0 = holidays[0]
        ideas.append(
            CampaignIdea(
                title=f"{h0.name} — Vouchers / early access",
                subject=f"{h0.name}: plan ahead for spring/summer",
                preheader="Join early access or grab a voucher for the season ahead.",
                content=(
                    f"Hi there,\n\n{h0.name} is coming up on {h0.date}. Even in the off-season, you can still plan ahead.\n\n"
                    "Join our early-access list for spring/summer openings, or request a voucher for birthdays and group experiences.\n\n"
                    "Reply to this email with your preferred month and group size and we’ll prioritise you when scheduling opens."
                ),
                ctaLabel=booking_cta,
                ctaUrl=booking_url,
                audience=MarketingAudience(lastNDays=730),
            )
        )

    ideas.append(
        CampaignIdea(
            title="Thank you + apology (weather/technical)",
            subject="Thank you for your support — we’re improving",
            preheader="Sorry to anyone we couldn't help due to weather or technical issues.",
            content=(
                "Hi there,\n\n"
                "Thank you for supporting Jet Ski & More. We also want to apologise to anyone we couldn't assist due "
                "to weather conditions or technical difficulties.\n\n"
                "We’re continuously improving our systems and processes so we can deliver a smoother experience, "
                "while keeping safety as the priority.\n\n"
                "If you’d like to try again, we’d love to have you on the water."
            ),
            ctaLabel=booking_cta,
            ctaUrl=booking_url,
            audience=MarketingAudience(lastNDays=730),
        )
    )
    if jet_ski_open:
        ideas.append(
            CampaignIdea(
                title="Weekend sell-out reminder",
                subject="Weekend rides fill fast — book early",
                preheader="Grab the best times before they’re gone.",
                content=(
                    "Hi there,\n\n"
                    "Our weekend time slots can sell out quickly—especially the late morning and early afternoon.\n\n"
                    f"If you’re looking at {ride_hint}, we recommend booking ahead to secure your preferred time.\n\n"
                    "Book online in minutes."
                ),
                ctaLabel="Book online",
                ctaUrl=booking_url,
                audience=MarketingAudience(lastNDays=365),
            )
        )
        ideas.append(
            CampaignIdea(
                title="Midweek calm-seas special",
                subject="Midweek on the water — quieter, easier to book",
                preheader="If your schedule is flexible, midweek has great availability.",
                content=(
                    "Hi there,\n\n"
                    "If you can go midweek, you’ll often find more availability and a calmer, more relaxed experience.\n\n"
                    "Choose your ride time and we’ll take you through our structured customer briefing and onboarding.\n\n"
                    "Check availability and pick a slot that works for you."
                ),
                ctaLabel="Check availability",
                ctaUrl=booking_url,
                audience=MarketingAudience(lastNDays=365),
            )
        )
    else:
        ideas.append(
            CampaignIdea(
                title="Winter operations update",
                subject="Winter update: improvements underway + safety-led scheduling",
                preheader="We’re preparing for summer while keeping safety the priority.",
                content=(
                    "Hi there,\n\n"
                    "We’re in our off-season and using this time to prepare for the next season — maintenance, checks, and operational improvements.\n\n"
                    "When weather and sea conditions allow, we’ll open limited windows for sessions. Safety remains the priority.\n\n"
                    "If you’d like first notice when the next openings go live, join our early-access list."
                ),
                ctaLabel=booking_cta,
                ctaUrl=booking_url,
                audience=MarketingAudience(lastNDays=730),
            )
        )
        ideas.append(
            CampaignIdea(
                title="Safety & compliance credibility",
                subject="How our rides are run (briefing, onboarding, safety requirements)",
                preheader="Factual overview for customers and partners.",
                content=(
                    "Hi there,\n\n"
                    "A quick note on how Jet Ski & More runs sessions:\n"
                    "• Structured customer briefing process\n"
                    "• Ride onboarding steps and operating procedures\n"
                    "• Swim competency requirement (where applicable)\n"
                    "• Safety equipment provided and mandatory life jackets\n"
                    "• Weather and sea-condition rules\n\n"
                    "We operate with procedures designed around commercial safety requirements."
                ),
                ctaLabel="View safety guide",
                ctaUrl=f"{base_url}/safety",
                audience=MarketingAudience(lastNDays=730),
            )
        )

    return MarketingInsightsResponse(
        industry=ind,
        location=loc,
        upcomingHolidays=holidays,
        recommendedSendHours=rec,
        bookingByHour=[HourStat(hour=h, count=booking_by_hour.get(h, 0)) for h in range(24)],
        bookingByDayOfWeek=[DayOfWeekStat(day=i, count=booking_by_dow.get(i, 0)) for i in range(7)],
        whatToSend=what_to_send,
        whatNotToSend=what_not_to_send,
        ideas=ideas[:6],
    )


@router.get("/admin/marketing/advisor/status", response_model=MarketingAdvisorStatusResponse)
def admin_marketing_advisor_status(admin: str = Depends(get_current_admin)):
    db = get_db()
    doc = db.marketing_advisor_state.find_one({"_id": "default"}) or {}
    enabled = bool(settings.marketing_advisor_enabled and settings.marketing_advisor_to)
    return MarketingAdvisorStatusResponse(
        enabled=enabled,
        toEmail=settings.marketing_advisor_to,
        lastSentAt=doc.get("lastSentAt"),
        lastSentKey=doc.get("lastSentKey"),
        lastAttemptAt=doc.get("lastAttemptAt"),
        lastAttemptOk=doc.get("lastAttemptOk"),
        lastError=doc.get("lastError"),
    )


@router.post("/admin/marketing/advisor/send-test")
def admin_marketing_advisor_send_test(payload: MarketingAdvisorSendTestRequest, admin: str = Depends(get_current_admin)):
    to_email = str(payload.toEmail or settings.marketing_advisor_to or settings.email_to or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="Missing recipient email")
    ok = send_advisor_email(to_email, kind="test")
    if not ok:
        raise HTTPException(status_code=500, detail="Advisor email send failed")
    return {"ok": True, "toEmail": to_email}


@router.post("/metrics/pageview")
def track_page_view(payload: PageViewRequest, user_agent: Optional[str] = Header(None), accept_language: Optional[str] = Header(None)):
    db = get_db()
    session_id = (payload.sessionId or "").strip() or None
    visitor_id = (payload.visitorId or "").strip() or session_id
    duration = None
    try:
        if payload.durationSeconds is not None:
            duration = float(payload.durationSeconds)
            if duration < 0 or duration > 6 * 3600:  # ignore negative or implausibly long stays
                duration = None
    except Exception:
        duration = None
    ua_info = _parse_user_agent(payload.userAgent or user_agent)
    lang = _clean(payload.language) or _clean((accept_language or "").split(",")[0])
    doc = {
        "path": (payload.path or "").strip(),
        "referrer": (payload.referrer or "").strip() or None,
        "user_agent": (payload.userAgent or "").strip() or (user_agent or None),
        "session_id": session_id,
        "duration_seconds": duration,
        "visitor_id": visitor_id,
        "country": _clean(payload.country),
        "city": _clean(payload.city),
        "device_type": _clean(payload.deviceType) or ua_info.get("device_type"),
        "os": _clean(payload.os) or ua_info.get("os"),
        "browser": _clean(payload.browser) or ua_info.get("browser"),
        "language": lang,
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
    _require_enabled("jetSkiBookingsEnabled", "Jet ski bookings are currently closed")
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
    # Best-effort admin + primary emails on success
    try:
        if success and settings.email_to:
            admin_body = format_payment_admin_email(req.booking.model_dump(), amount, charge_id, status)
            send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=req.booking.email)
        if success and req.booking.email:
            client_body = format_payment_client_email(req.booking.model_dump(), amount, charge_id)
            ok = send_email(subject="Booking confirmed — payment received", body=client_body, to_address=req.booking.email)
            if not ok:
                print(f"[email] Failed sending payment confirmation to {req.booking.email}")
    except Exception as e:
        print(f"[email] Error sending payment emails: {e}")
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
    _require_enabled("jetSkiBookingsEnabled", "Jet ski bookings are currently closed")
    amount = compute_amount_cents(req.rideId, req.addons.model_dump())
    return PaymentQuoteResponse(amountInCents=amount)


@router.get("/payments/config")
def payments_config():
    if not settings.yoco_public_key:
        raise HTTPException(status_code=500, detail="Yoco public key not configured")
    return {"publicKey": settings.yoco_public_key, "currency": "ZAR"}


@router.post("/payments/initiate")
def payments_initiate(req: ChargeBookingRequest):
    _require_enabled("jetSkiBookingsEnabled", "Jet ski bookings are currently closed")
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
    booking_id: Optional[str] = None
    try:
        booking_id = save_booking(doc, amount, charge_id, status=status)
    except Exception as e:
        # Continue with notifications even if persistence fails
        print(f"[booking] Failed to persist booking: {e}")
        booking_id = None

    participants: list[dict] = []
    if booking_id:
        try:
            participants = _create_participants(db, doc, booking_id)
        except Exception as e:
            print(f"[booking] Failed to create participants: {e}")
            participants = []

    # Always ensure we email the primary booker even if participant creation fails
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
        # Use a placeholder _id if saving failed so email templates have an id
        placeholder_id = booking_id or str(ObjectId())
        _send_booking_notifications(doc | {"_id": placeholder_id}, participants)
    except Exception as e:
        print(f"[email] Notification send failed: {e}")
    return booking_id


@router.post("/payments/checkout")
def payments_checkout(req: ChargeBookingRequest):
    _require_enabled("jetSkiBookingsEnabled", "Jet ski bookings are currently closed")
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
            except Exception:
                pass
            try:
                _persist_booking_and_notify(booking, amount, charge_id, status='approved')
            except Exception as e:
                print(f"[booking] Persist/notify failed: {e}")
            if settings.email_to:
                admin_body = format_payment_admin_email(booking, amount, charge_id, "approved")
                send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=booking.get("email"))
            if booking.get("email"):
                client_body = format_payment_client_email(booking, amount, charge_id)
                ok = send_email(subject="Booking confirmed — payment received", body=client_body, to_address=booking.get("email"))
                if not ok:
                    print(f"[email] Failed sending payment confirmation to {booking.get('email')}")
        except Exception as e:
            print(f"[email] Verify checkout flow error: {e}")

    return VerifyCheckoutResponse(ok=is_success, checkoutId=checkout_id, status=status, paymentId=payment_id)


@router.post("/payments/link")
def payments_link(req: ChargeBookingRequest):
    _require_enabled("jetSkiBookingsEnabled", "Jet ski bookings are currently closed")
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
            except Exception as e:
                print(f"[booking] Persist/notify failed: {e}")
            if settings.email_to:
                admin_body = format_payment_admin_email(booking, amount, charge_id, status)
                send_email(subject=f"Paid booking — {charge_id}", body=admin_body, to_address=settings.email_to, reply_to=booking.get("email"))
            try:
                if booking.get("email"):
                    client_body = format_payment_client_email(booking, amount, charge_id)
                    ok = send_email(subject="Booking confirmed — payment received", body=client_body, to_address=booking.get("email"))
                    if not ok:
                        print(f"[email] Failed sending payment confirmation to {booking.get('email')}")
            except Exception as e:
                print(f"[email] Error sending client payment email: {e}")
        except Exception as e:
            print(f"[email] Verify payment flow error: {e}")

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
