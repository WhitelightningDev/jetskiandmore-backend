from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field


class ContactRequest(BaseModel):
    fullName: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    phone: str = Field(..., min_length=3, max_length=50)
    message: str = Field(..., min_length=2, max_length=4000)
    subject: Optional[str] = None
    targetEmail: Optional[EmailStr] = None
    type: Optional[str] = None
    date: Optional[str] = None  # ISO date (YYYY-MM-DD)
    people: Optional[int] = None


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
    email: Optional[EmailStr] = None


class Rider(BaseModel):
    name: str
    email: Optional[EmailStr] = None


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
    riders: Optional[List[Rider]] = None


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


class BookingControlsResponse(BaseModel):
    jetSkiBookingsEnabled: bool
    boatRideBookingsEnabled: bool
    fishingChartersBookingsEnabled: bool
    updatedAt: Optional[datetime] = None


class BookingControlsUpdateRequest(BaseModel):
    jetSkiBookingsEnabled: Optional[bool] = None
    boatRideBookingsEnabled: Optional[bool] = None
    fishingChartersBookingsEnabled: Optional[bool] = None


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
    bookingReference: Optional[str] = None
    bookingGroupId: Optional[str] = None
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
    totalPageViews: int = 0
    rides: List[RideAnalytics]


class PageViewRequest(BaseModel):
    path: Optional[str] = None
    referrer: Optional[str] = None
    userAgent: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    deviceType: Optional[str] = None
    os: Optional[str] = None
    browser: Optional[str] = None
    language: Optional[str] = None
    durationSeconds: Optional[float] = None
    sessionId: Optional[str] = None
    visitorId: Optional[str] = None


class PageViewAnalyticsItem(BaseModel):
    path: str
    views: int
    uniqueSessions: int
    avgDurationSeconds: Optional[float] = None
    totalDurationSeconds: float = 0
    firstSeen: Optional[datetime] = None
    lastSeen: Optional[datetime] = None


class CountStat(BaseModel):
    key: str
    count: int


class TimeOfDayStat(BaseModel):
    hour: int
    views: int


class ReturningStat(BaseModel):
    newVisitors: int
    returningVisitors: int
    totalVisitors: int


class PageViewBreakdowns(BaseModel):
    countries: List[CountStat] = []
    cities: List[CountStat] = []
    deviceTypes: List[CountStat] = []
    os: List[CountStat] = []
    browsers: List[CountStat] = []
    languages: List[CountStat] = []
    timeOfDay: List[TimeOfDayStat] = []
    returning: ReturningStat


class PageViewAnalyticsResponse(BaseModel):
    items: List[PageViewAnalyticsItem]
    totalViews: int
    totalUniqueSessions: int
    totalUniqueVisitors: int
    breakdowns: PageViewBreakdowns


# --- Participants / indemnities ---


class ParticipantRole(str):
    PRIMARY_RIDER = "PRIMARY_RIDER"
    RIDER = "RIDER"
    PASSENGER = "PASSENGER"


class ParticipantResponse(BaseModel):
    id: str
    bookingId: str
    bookingGroupId: str
    fullName: str
    email: Optional[EmailStr] = None
    role: str
    isRider: bool = False
    positionNumber: int
    indemnityToken: Optional[str] = None
    createdAt: Optional[datetime] = None


class IndemnitySubmitRequest(BaseModel):
    token: str
    fullName: Optional[str] = None
    email: Optional[EmailStr] = None
    hasWatchedVideo: Optional[bool] = None


class IndemnityStatusItem(BaseModel):
    participantId: str
    fullName: str
    email: Optional[EmailStr] = None
    role: str
    isRider: bool
    positionNumber: int
    indemnityStatus: str
    signedAt: Optional[datetime] = None


class IndemnityStatusResponse(BaseModel):
    bookingId: str
    bookingGroupId: str
    rideId: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    participants: List[IndemnityStatusItem]


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


# --- Admin marketing / campaigns ---


class MarketingAudience(BaseModel):
    rideId: Optional[str] = None
    status: Optional[str] = None
    lastNDays: Optional[int] = None
    includeManual: Optional[bool] = True


class MarketingCampaignCreateRequest(BaseModel):
    name: str
    subject: str
    preheader: Optional[str] = None
    content: Optional[str] = None
    ctaLabel: Optional[str] = None
    ctaUrl: Optional[str] = None
    audience: Optional[MarketingAudience] = None
    html: Optional[str] = None


class MarketingCampaignUpdateRequest(BaseModel):
    name: Optional[str] = None
    subject: Optional[str] = None
    preheader: Optional[str] = None
    content: Optional[str] = None
    ctaLabel: Optional[str] = None
    ctaUrl: Optional[str] = None
    audience: Optional[MarketingAudience] = None
    html: Optional[str] = None


class MarketingCampaignStats(BaseModel):
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    totalRecipients: Optional[int] = None
    remainingRecipients: Optional[int] = None
    batchOffset: Optional[int] = None
    batchSize: Optional[int] = None
    lastBatchAttempted: Optional[int] = None
    lastBatchSent: Optional[int] = None
    lastBatchFailed: Optional[int] = None


class MarketingCampaignResponse(BaseModel):
    id: str
    name: str
    subject: str
    preheader: Optional[str] = None
    content: Optional[str] = None
    ctaLabel: Optional[str] = None
    ctaUrl: Optional[str] = None
    audience: Optional[MarketingAudience] = None
    html: Optional[str] = None
    status: str = "draft"
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    sentAt: Optional[datetime] = None
    stats: Optional[MarketingCampaignStats] = None


class MarketingCampaignListResponse(BaseModel):
    items: List[MarketingCampaignResponse] = []


class MarketingRecipientsExportRequest(BaseModel):
    rideId: Optional[str] = None
    status: Optional[str] = None
    lastNDays: Optional[int] = None
    includeManual: Optional[bool] = True


class MarketingRecipientsExportResponse(BaseModel):
    emails: List[str] = []


class MarketingManualRecipientsUploadResponse(BaseModel):
    added: int = 0
    total: int = 0
    invalid: int = 0


class MarketingManualRecipientsListResponse(BaseModel):
    emails: List[str] = []
    total: int = 0


class MarketingAssetResponse(BaseModel):
    id: str
    filename: str
    contentType: str
    size: int
    url: str
    createdAt: Optional[datetime] = None


class MarketingAssetListResponse(BaseModel):
    items: List[MarketingAssetResponse] = []


class MarketingRecipientsPreviewResponse(BaseModel):
    count: int
    sample: List[str] = []


class MarketingSendTestRequest(BaseModel):
    toEmail: EmailStr


class MarketingAudienceSummaryResponse(BaseModel):
    totalUniqueEmails: int
    uniqueEmailsLast30Days: int
    uniqueEmailsLast90Days: int
    byRide: List[CountStat] = []
    topDomains: List[CountStat] = []


class MarketingEmailEventResponse(BaseModel):
    id: str
    campaignId: str
    email: EmailStr
    kind: str  # test | bulk
    ok: bool
    error: Optional[str] = None
    subject: Optional[str] = None
    sentAt: datetime


class MarketingEmailEventListResponse(BaseModel):
    items: List[MarketingEmailEventResponse] = []


class HourStat(BaseModel):
    hour: int
    count: int


class DayOfWeekStat(BaseModel):
    day: int  # 0=Sun .. 6=Sat
    count: int


class MarketingSendStatsResponse(BaseModel):
    totalAttempted: int
    totalSent: int
    totalFailed: int
    byHour: List[HourStat] = []
    byDayOfWeek: List[DayOfWeekStat] = []


class HolidayItem(BaseModel):
    date: str  # YYYY-MM-DD
    name: str


class CampaignIdea(BaseModel):
    title: str
    subject: str
    preheader: Optional[str] = None
    content: str
    ctaLabel: Optional[str] = None
    ctaUrl: Optional[str] = None
    audience: Optional[MarketingAudience] = None


class MarketingInsightsResponse(BaseModel):
    industry: str
    location: str
    upcomingHolidays: List[HolidayItem] = []
    recommendedSendHours: List[int] = []
    bookingByHour: List[HourStat] = []
    bookingByDayOfWeek: List[DayOfWeekStat] = []
    whatToSend: List[str] = []
    whatNotToSend: List[str] = []
    ideas: List[CampaignIdea] = []
