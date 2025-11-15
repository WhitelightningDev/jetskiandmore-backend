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
