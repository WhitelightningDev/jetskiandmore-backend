from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field


class ContactRequest(BaseModel):
    fullName: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    phone: str = Field(..., min_length=3, max_length=50)
    message: str = Field(..., min_length=2, max_length=4000)


class ContactResponse(BaseModel):
    ok: bool
    id: str


class Addons(BaseModel):
    drone: bool = False
    gopro: bool = False
    wetsuit: bool = False
    boat: bool = False
    boatCount: int = 1
    extraPeople: int = 0


class Passenger(BaseModel):
    name: str


class BookingRequest(BaseModel):
    rideId: str
    date: Optional[str] = None  # ISO date string (YYYY-MM-DD)
    time: Optional[str] = None
    fullName: str
    email: EmailStr
    phone: str
    notes: Optional[str] = None
    addons: Addons
    passengers: Optional[List[Passenger]] = None


class BookingResponse(BaseModel):
    ok: bool
    id: str


class ChargeRequest(BaseModel):
    token: str
    amount: int
    currency: str = "ZAR"
    email: Optional[EmailStr] = None
    reference: Optional[str] = None


class ChargeResponse(BaseModel):
    ok: bool
    id: str
    status: str
    raw: dict


class PaymentQuoteRequest(BaseModel):
    rideId: str
    addons: Addons


class PaymentQuoteResponse(BaseModel):
    currency: str = "ZAR"
    amountInCents: int


class ChargeBookingRequest(BaseModel):
    token: str
    booking: BookingRequest


class VerifyPaymentRequest(BaseModel):
    orderId: str
    booking: BookingRequest


class VerifyPaymentResponse(BaseModel):
    ok: bool
    orderId: str
    status: str


class VerifyPaymentByIdRequest(BaseModel):
    paymentId: str
    booking: BookingRequest


class VerifyPaymentByIdResponse(BaseModel):
    ok: bool
    paymentId: str
    orderId: Optional[str] = None
    status: str


class VerifyCheckoutRequest(BaseModel):
    checkoutId: str
    booking: BookingRequest


class VerifyCheckoutResponse(BaseModel):
    ok: bool
    checkoutId: str
    status: str
    paymentId: Optional[str] = None


class TimeslotAvailabilityResponse(BaseModel):
    rideId: str
    date: str
    times: List[str]


# --- Admin / dashboard schemas ---


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


class AdminLoginResponse(BaseModel):
    token: str
    tokenType: str = "bearer"


class BookingAdminResponse(BaseModel):
    id: str
    rideId: str
    date: Optional[str] = None
    time: Optional[str] = None
    fullName: str
    email: EmailStr
    phone: str
    notes: Optional[str] = None
    addons: Dict[str, Any] | None = None
    status: str
    amountInCents: int
    paymentRef: Optional[str] = None
    createdAt: Optional[datetime] = None
    passengers: Optional[List[Dict[str, Any]]] = None


class BookingUpdateRequest(BaseModel):
    status: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    notes: Optional[str] = None
    message: Optional[str] = None


class RideAnalytics(BaseModel):
    rideId: str
    bookings: int
    revenueInCents: int


class AnalyticsSummaryResponse(BaseModel):
    totalBookings: int
    totalRevenueInCents: int
    totalRevenueZar: float
    rides: List[RideAnalytics]


# --- Interim skipper quiz ---


class InterimSkipperQuizAnswers(BaseModel):
    q1_distance_from_shore: str
    q2_kill_switch: str
    q3_what_to_wear: str
    q4_kill_switch_connection: str
    q5_harbour_passing_rule: str
    q6_harbour_rules: List[str]
    q7_max_distance: str
    q8_connect_kill_switch_two_places: List[str]
    q9_deposit_loss_reasons: List[str]
    q10_emergency_items_onboard: List[str]


class InterimSkipperQuizRequest(BaseModel):
    email: EmailStr
    name: str
    surname: str
    idNumber: str
    passengerName: Optional[str] = None
    passengerSurname: Optional[str] = None
    passengerEmail: Optional[EmailStr] = None
    passengerIdNumber: Optional[str] = None
    hasWatchedTutorial: bool
    hasAcceptedIndemnity: bool
    quizAnswers: InterimSkipperQuizAnswers


class InterimSkipperQuizResponse(BaseModel):
    success: bool = True
    ok: bool
    id: str


class InterimSkipperQuizAdminResponse(BaseModel):
    id: str
    email: EmailStr
    name: str
    surname: str
    idNumber: str
    passengerName: Optional[str] = None
    passengerSurname: Optional[str] = None
    passengerEmail: Optional[EmailStr] = None
    passengerIdNumber: Optional[str] = None
    hasWatchedTutorial: bool
    hasAcceptedIndemnity: bool
    quizAnswers: Dict[str, Any]
    createdAt: Optional[datetime] = None
