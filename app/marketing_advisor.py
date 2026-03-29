from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from .config import settings
from .db import get_db
from .emailer import send_email


try:
    _SAST = ZoneInfo("Africa/Johannesburg") if ZoneInfo else None
except Exception:
    _SAST = None


def _localize(dt: datetime) -> datetime:
    try:
        tz = _SAST or timezone.utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)
    except Exception:
        return dt


def _now_local() -> datetime:
    try:
        tz = _SAST or timezone.utc
        return datetime.now(tz)
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc)


@dataclass
class AdvisorRecommendation:
    recommended_send_hours: list[int]
    peak_booking_hours: list[int]
    upcoming_holidays: list[tuple[str, str]]  # (iso_date, name)
    season: str
    operating_status: str
    booking_controls: dict[str, bool]
    days_to_spring: int
    days_to_summer: int
    seasonal_focus: list[str]
    what_to_send: list[str]
    what_not_to_send: list[str]
    ideas: list[dict[str, Any]]


def _easter_sunday(year: int) -> date:
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


def _upcoming_sa_holidays(from_dt: date, days: int = 120) -> list[tuple[str, str]]:
    end_dt = from_dt + timedelta(days=max(1, days))
    years = sorted({from_dt.year, end_dt.year})
    out: list[tuple[str, str]] = []

    def add(d: date, name: str):
        if from_dt <= d <= end_dt:
            out.append((d.isoformat(), name))

    for y in years:
        add(date(y, 1, 1), "New Year's Day")
        add(date(y, 3, 21), "Human Rights Day")
        good_friday = _easter_sunday(y) - timedelta(days=2)
        family_day = _easter_sunday(y) + timedelta(days=1)
        add(good_friday, "Good Friday")
        add(family_day, "Family Day")
        add(date(y, 4, 27), "Freedom Day")
        add(date(y, 5, 1), "Workers' Day")
        add(date(y, 6, 16), "Youth Day")
        add(date(y, 8, 9), "National Women's Day")
        add(date(y, 9, 24), "Heritage Day")
        add(date(y, 12, 16), "Day of Reconciliation")
        add(date(y, 12, 25), "Christmas Day")
        add(date(y, 12, 26), "Day of Goodwill")
    out.sort(key=lambda x: x[0])
    return out[:12]


def _format_hour(h: int) -> str:
    hh = int(h) % 24
    return f"{hh:02d}:00"


_DEFAULT_BOOKING_CONTROLS: dict[str, bool] = {
    "jetSkiBookingsEnabled": False,
    "boatRideBookingsEnabled": True,
    "fishingChartersBookingsEnabled": True,
}


def _load_booking_controls(db) -> dict[str, bool]:
    try:
        doc = db.site_settings.find_one({"key": "booking_controls"}) or {}
        return {
            "jetSkiBookingsEnabled": bool(doc.get("jetSkiBookingsEnabled", _DEFAULT_BOOKING_CONTROLS["jetSkiBookingsEnabled"])),
            "boatRideBookingsEnabled": bool(doc.get("boatRideBookingsEnabled", _DEFAULT_BOOKING_CONTROLS["boatRideBookingsEnabled"])),
            "fishingChartersBookingsEnabled": bool(
                doc.get("fishingChartersBookingsEnabled", _DEFAULT_BOOKING_CONTROLS["fishingChartersBookingsEnabled"])
            ),
        }
    except Exception:
        return dict(_DEFAULT_BOOKING_CONTROLS)


def escape_html(input: str) -> str:
    return (
        str(input)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def _season_for_sa(d: date) -> str:
    # Southern hemisphere seasons
    if d.month in (6, 7, 8):
        return "Winter"
    if d.month in (9, 10, 11):
        return "Spring"
    if d.month in (12, 1, 2):
        return "Summer"
    return "Autumn"


def _days_until(d: date, target_month: int, target_day: int = 1) -> int:
    try:
        target = date(d.year, target_month, target_day)
    except Exception:
        return 0
    if target < d:
        try:
            target = date(d.year + 1, target_month, target_day)
        except Exception:
            return 0
    return max(0, (target - d).days)


def build_advisor_recommendation(db, lookback_days: int = 365) -> AdvisorRecommendation:
    lb = max(14, min(int(lookback_days or 365), 730))
    cutoff = datetime.utcnow() - timedelta(days=lb)

    controls = _load_booking_controls(db)
    jet_ski_open = bool(controls.get("jetSkiBookingsEnabled"))
    operating_status = "Open (jet ski bookings enabled)" if jet_ski_open else "Closed (off-season / jet ski bookings disabled)"

    bookings = list(
        db.bookings.find(
            {"$or": [{"createdAt": {"$gte": cutoff}}, {"created_at": {"$gte": cutoff}}]},
            {"createdAt": 1, "created_at": 1},
        ).limit(100000)
    )

    booking_by_hour: dict[int, int] = {h: 0 for h in range(24)}
    for b in bookings:
        created = b.get("createdAt") or b.get("created_at")
        if not isinstance(created, datetime):
            continue
        local_dt = _localize(created)
        booking_by_hour[int(local_dt.hour)] = booking_by_hour.get(int(local_dt.hour), 0) + 1

    sorted_hours = sorted(booking_by_hour.items(), key=lambda x: x[1], reverse=True)
    peak_hours = [h for h, c in sorted_hours[:4] if c > 0]
    rec: list[int] = []
    for h in peak_hours:
        candidate = (h - 2) % 24
        if 8 <= candidate <= 19 and candidate not in rec:
            rec.append(candidate)
    if not rec:
        rec = [10, 12, 15, 18]

    today_local = _now_local().date()
    mode = str(getattr(settings, "marketing_advisor_mode", "auto") or "auto").strip().lower()
    if mode == "winter_ramp":
        season = "Winter"
    elif mode == "spring_ramp":
        season = "Spring"
    elif mode == "summer_peak":
        season = "Summer"
    else:
        season = _season_for_sa(today_local)
    days_to_spring = _days_until(today_local, 9, 1)
    days_to_summer = _days_until(today_local, 12, 1)

    holidays = _upcoming_sa_holidays(today_local, days=180)

    booking_url = "https://www.jetskiandmore.com/Bookings"
    contact_url = "https://www.jetskiandmore.com/contact"

    is_off_season = (not jet_ski_open) and mode == "auto"

    if is_off_season:
        seasonal_focus = [
            "Off-season visibility: partner updates + credibility assets (safety/compliance)",
            "Build demand: early-access list + vouchers for spring/summer",
            "Stay factual: weather windows only when conditions allow",
        ]
    elif season == "Winter":
        seasonal_focus = [
            "Stay visible: partner updates, safety/compliance credibility, behind-the-scenes improvements",
            "Build demand: early-access list + gift vouchers for spring/summer",
            "Keep it factual: weather windows only when conditions allow",
        ]
    elif season == "Spring":
        seasonal_focus = [
            "Reopening momentum: early-access + limited slots messaging",
            "Push weekends first (highest intent), midweek specials second",
            "Reinforce credibility: safety briefing + procedures + SAMSA-certified operations",
        ]
    elif season == "Summer":
        seasonal_focus = [
            "Capacity + urgency: weekend sell-outs, holiday availability, group bookings",
            "Referral + repeat: bring-a-friend and return rider offers",
            "Operational clarity: check-in steps, safety requirements, weather rules",
        ]
    else:
        seasonal_focus = [
            "End-of-season relationship: thank-you + service recovery",
            "Winter ramp: early-access list + partner planning",
            "Credibility assets: safety/compliance positioning",
        ]

    what_to_send = [
        "Seasonal updates (what’s changing, what’s new, and what to expect)",
        "Safety & compliance positioning (briefings, onboarding, procedures)",
        "Partner-facing listings refresh (tourism operators, hotels, activity desks)",
        "Gift vouchers / early-access list for spring/summer",
        "Weather window updates (confirming go/no-go and next available slots)",
        "Weekend availability reminders when you're close to summer",
    ]
    what_not_to_send = [
        "Late-night sends (keeps engagement low and raises spam complaints)",
        "Overly aggressive discounting (hurts perceived quality)",
        "Vague or informal wording (use factual, professional tone)",
        "Too many emails in a short period (batch thoughtfully)",
    ]

    base_ideas: list[dict[str, Any]] = [
        {
            "title": "Thank you + service recovery",
            "subject": "Thank you for your support — and our apologies for weather/technical delays",
            "preheader": "We appreciate your patience and we’re improving our operations.",
            "content": (
                "Hi there,\n\n"
                "Thank you for supporting Jet Ski & More.\n\n"
                "We’d like to apologise to anyone we couldn’t assist recently due to technical difficulties or weather conditions. "
                "We operate with safety as the priority and we’re continuously improving to deliver a smooth experience.\n\n"
                "If you’d like to try again, we’d love to have you on the water."
            ),
            "ctaLabel": "Stay in touch",
            "ctaUrl": contact_url,
        },
        {
            "title": "Safety & compliance credibility",
            "subject": "How our rides are run (briefing, onboarding, safety requirements)",
            "preheader": "Factual overview for customers and partners.",
            "content": (
                "Hi there,\n\n"
                "A quick note on how Jet Ski & More runs sessions:\n"
                "• Structured customer briefing process\n"
                "• Ride onboarding steps and operating procedures\n"
                "• Swim competency requirement (where applicable)\n"
                "• Safety equipment provided and mandatory life jackets\n"
                "• Weather and sea-condition rules\n\n"
                "We operate with procedures designed around commercial safety requirements."
            ),
            "ctaLabel": "View safety guide",
            "ctaUrl": "https://www.jetskiandmore.com/safety",
        },
    ]

    winter_ideas: list[dict[str, Any]] = [
        {
            "title": "Winter operations update (keep visibility)",
            "subject": "Winter update: improvements underway + limited weather windows",
            "preheader": "We’re preparing for summer while keeping safety the priority.",
            "content": (
                "Hi there,\n\n"
                "We’re in our winter period and using this time to prepare for the next season — equipment checks, maintenance, and operational improvements.\n\n"
                "When weather and sea conditions allow, we’ll open limited windows for sessions. Safety remains the priority and we may pause operations when conditions change.\n\n"
                "If you’d like to be notified first when new slots open, reply to this email or join our early-access list."
            ),
            "ctaLabel": "Join early access",
            "ctaUrl": contact_url,
        },
        {
            "title": "Gift vouchers / early-bird summer",
            "subject": "Gift a summer ride — vouchers + early access now open",
            "preheader": "Perfect for birthdays, couples, and groups planning ahead.",
            "content": (
                "Hi there,\n\n"
                "Even while it’s winter, you can still plan ahead for spring/summer. We’re opening an early-access list for:\n"
                "• Gift vouchers\n"
                "• Group bookings (birthdays / corporate)\n"
                "• First access to opening-weekend slots\n\n"
                "Reply to this email with your preferred month and group size and we’ll prioritise you when scheduling opens."
            ),
            "ctaLabel": "Enquire",
            "ctaUrl": contact_url,
        },
        {
            "title": "Partner & tourism desk update",
            "subject": "Partner update: winter prep + safety-led operations",
            "preheader": "For hotels, tourism desks, and local partners.",
            "content": (
                "Hi partner,\n\n"
                "Jet Ski & More operates guided jet ski rides from Gordon’s Bay Harbour with structured briefings, onboarding, and procedures designed around commercial safety requirements.\n\n"
                "We’re currently in winter prep and will open limited weather windows when conditions allow, with a full ramp into spring/summer.\n\n"
                "If you’d like updated brochures, pricing, or a partner blurb for your listings, reply and we’ll send the latest."
            ),
            "ctaLabel": "Request partner pack",
            "ctaUrl": contact_url,
        },
        {
            "title": "Reviews + social proof (off-season visibility)",
            "subject": "Quick favour: could you share a short review of your ride?",
            "preheader": "Reviews help customers and tourism partners compare operators.",
            "content": (
                "Hi there,\n\n"
                "If you’ve ridden with Jet Ski & More, we’d appreciate a short review.\n\n"
                "Reviews help new customers (and tourism partners) understand what we offer — and how we run sessions with a structured briefing and safety-led procedures.\n\n"
                "Thank you for your support."
            ),
            "ctaLabel": "Leave a review",
            "ctaUrl": "https://www.google.com/search?q=Jet+Ski+%26+More+Gordon%27s+Bay+review",
        },
    ]

    spring_ideas: list[dict[str, Any]] = [
        {
            "title": "Season reopening early access",
            "subject": "Spring slots opening soon — early access list",
            "preheader": "Get first pick of weekend times in Gordon’s Bay.",
            "content": (
                "Hi there,\n\n"
                "We’re preparing to open our spring schedule. Weekend times are usually the first to fill.\n\n"
                "If you’d like early access, reply with your preferred weekend(s) and group size. We’ll help you secure a slot when the schedule opens."
            ),
            "ctaLabel": "Join early access",
            "ctaUrl": contact_url,
        },
        {
            "title": "Weekend sell-out reminder",
            "subject": "Weekend rides fill fast — book early",
            "preheader": "Grab the best times before they’re gone.",
            "content": (
                "Hi there,\n\n"
                "Our weekend time slots can sell out quickly—especially the late morning and early afternoon.\n\n"
                "If you're planning a ride in Gordon’s Bay, we recommend booking ahead to secure your preferred time.\n\n"
                "Book online in minutes."
            ),
            "ctaLabel": "Book online",
            "ctaUrl": booking_url,
        },
    ]

    summer_ideas: list[dict[str, Any]] = [
        {
            "title": "Holiday availability",
            "subject": "Holiday rides: limited slots — check availability",
            "preheader": "Secure your preferred time before it sells out.",
            "content": (
                "Hi there,\n\n"
                "Holiday and weekend sessions fill quickly. If you're planning a jet ski ride, the best way to secure your time is to book early.\n\n"
                "We include structured briefings, onboarding, and mandatory safety gear with every session.\n\n"
                "Check availability and pick your slot."
            ),
            "ctaLabel": "Check availability",
            "ctaUrl": booking_url,
        }
    ]

    if is_off_season:
        ideas = winter_ideas + base_ideas
        # As you approach spring/summer, bubble those campaigns up automatically.
        if days_to_spring <= 90:
            ideas = spring_ideas + ideas
        if days_to_summer <= 90:
            ideas = summer_ideas + ideas
    else:
        if season == "Winter":
            ideas = winter_ideas + base_ideas
        elif season == "Spring":
            ideas = spring_ideas + base_ideas
        elif season == "Summer":
            ideas = summer_ideas + base_ideas
        else:
            ideas = base_ideas + winter_ideas[:2]

    return AdvisorRecommendation(
        recommended_send_hours=rec,
        peak_booking_hours=peak_hours,
        upcoming_holidays=holidays,
        season=season,
        operating_status=operating_status,
        booking_controls=controls,
        days_to_spring=days_to_spring,
        days_to_summer=days_to_summer,
        seasonal_focus=seasonal_focus,
        what_to_send=what_to_send,
        what_not_to_send=what_not_to_send,
        ideas=ideas[:6],
    )


def _advisor_email_html(rec: AdvisorRecommendation, now_local: datetime, headline_hour: Optional[int]) -> str:
    brand = settings.email_from_name or "Jet Ski & More"
    rec_hours = ", ".join(_format_hour(h) for h in rec.recommended_send_hours) or "—"
    peak_hours = ", ".join(_format_hour(h) for h in rec.peak_booking_hours) or "—"
    holidays = rec.upcoming_holidays[:6]

    def li(items: list[str]) -> str:
        return "".join(f"<li style='margin:4px 0;'>{item}</li>" for item in items)

    controls = rec.booking_controls or {}
    controls_lines = [
        f"Jet ski bookings: {'Open' if controls.get('jetSkiBookingsEnabled') else 'Closed'}",
        f"Boat rides: {'Open' if controls.get('boatRideBookingsEnabled') else 'Closed'}",
        f"Fishing charters: {'Open' if controls.get('fishingChartersBookingsEnabled') else 'Closed'}",
    ]

    holiday_rows = "".join(
        f"<tr><td style='padding:6px 0;color:#0f172a;'>{name}</td>"
        f"<td style='padding:6px 0;color:#64748b;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;text-align:right;'>{iso}</td></tr>"
        for iso, name in holidays
    ) or "<tr><td style='padding:6px 0;color:#64748b;' colspan='2'>—</td></tr>"

    idea = (rec.ideas or [{}])[0] or {}
    idea_title = str(idea.get("title") or "Campaign idea").strip()
    idea_subject = str(idea.get("subject") or "").strip()
    idea_preheader = str(idea.get("preheader") or "").strip()
    idea_content = str(idea.get("content") or "").strip().replace("\n", "<br/>")

    queue = rec.ideas[1:5] if rec.ideas else []
    queue_rows = "".join(
        "<tr>"
        f"<td style='padding:8px 0;color:#0f172a;font-weight:700;vertical-align:top;'>{escape_html(str(i.get('title') or '').strip())}</td>"
        f"<td style='padding:8px 0;color:#334155;vertical-align:top;'>{escape_html(str(i.get('subject') or '').strip())}</td>"
        "</tr>"
        for i in queue
        if isinstance(i, dict)
    ) or "<tr><td style='padding:8px 0;color:#64748b;' colspan='2'>—</td></tr>"

    recommended_now = ""
    if headline_hour is not None:
        recommended_now = f"Recommended send window: <strong>{_format_hour(headline_hour)}</strong> (SAST)"

    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Marketing advisor — {brand}</title>
  </head>
  <body style="margin:0;background:#f8fafc;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">
      <tr>
        <td align="center" style="padding:24px 12px;">
          <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;max-width:640px;">
            <tr>
              <td style="padding:18px 20px;border:1px solid #e2e8f0;border-radius:16px;background:#ffffff;">
                <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;margin-bottom:8px;">{brand}</div>
	                <h1 style="font-size:18px;margin:0 0 10px 0;color:#0f172a;">Marketing advisor</h1>
	                <p style="margin:0 0 12px 0;color:#334155;font-size:14px;line-height:1.55;">
	                  {recommended_now or "Here are your next best send windows and suggested campaign content."}
	                </p>

	                <div style="margin:0 0 12px 0;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;">
	                  <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">Season</div>
	                  <div style="margin-top:6px;color:#0f172a;font-size:14px;">
	                    <strong>{rec.season}</strong> • {rec.days_to_spring} days to Spring • {rec.days_to_summer} days to Summer
	                  </div>
	                  <ul style="margin:10px 0 0 18px;color:#334155;font-size:14px;line-height:1.5;">{li(rec.seasonal_focus[:5])}</ul>
	                </div>

	                <div style="margin:0 0 12px 0;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;">
	                  <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">Operations</div>
	                  <div style="margin-top:6px;color:#0f172a;font-size:14px;"><strong>{escape_html(rec.operating_status)}</strong></div>
	                  <ul style="margin:10px 0 0 18px;color:#334155;font-size:14px;line-height:1.5;">{li([escape_html(x) for x in controls_lines])}</ul>
	                </div>

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:14px 0 12px 0;">
                  <tr>
                    <td style="padding:10px 12px;border:1px solid #e2e8f0;border-radius:12px;background:#f8fafc;">
                      <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">Send windows (SAST)</div>
                      <div style="margin-top:6px;color:#0f172a;font-size:14px;"><strong>{rec_hours}</strong></div>
                      <div style="margin-top:4px;color:#64748b;font-size:12px;">Peak booking hours: {peak_hours}</div>
                    </td>
                  </tr>
                </table>

                <div style="display:flex;gap:14px;flex-wrap:wrap;">
                  <div style="flex:1;min-width:260px;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;">
                    <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">What to send</div>
                    <ul style="margin:10px 0 0 18px;color:#334155;font-size:14px;line-height:1.5;">{li(rec.what_to_send[:6])}</ul>
                  </div>
                  <div style="flex:1;min-width:260px;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;">
                    <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">What not to send</div>
                    <ul style="margin:10px 0 0 18px;color:#334155;font-size:14px;line-height:1.5;">{li(rec.what_not_to_send[:6])}</ul>
                  </div>
                </div>

	                <div style="margin-top:14px;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#f8fafc;">
	                  <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">Suggested campaign (ready to paste)</div>
	                  <div style="margin-top:6px;color:#0f172a;font-size:14px;"><strong>{idea_title}</strong></div>
	                  <div style="margin-top:4px;color:#64748b;font-size:12px;">Subject: {idea_subject}</div>
	                  <div style="margin-top:2px;color:#64748b;font-size:12px;">Preheader: {idea_preheader}</div>
	                  <div style="margin-top:10px;color:#334155;font-size:14px;line-height:1.55;">{idea_content}</div>
	                </div>

	                <div style="margin-top:14px;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;">
	                  <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">Next campaign queue</div>
	                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin-top:8px;">
	                    {queue_rows}
	                  </table>
	                </div>

                <div style="margin-top:14px;padding:12px;border:1px solid #e2e8f0;border-radius:12px;background:#ffffff;">
                  <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">Upcoming holidays</div>
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin-top:8px;">{holiday_rows}</table>
                </div>

	                <div style="margin-top:16px;padding-top:14px;border-top:1px solid #e2e8f0;font-size:12px;color:#64748b;">
	                  Generated at {now_local.strftime("%Y-%m-%d %H:%M")} SAST • Season: {rec.season} • Industry: {settings.marketing_advisor_industry} • Location: {settings.marketing_advisor_location}
	                </div>
	              </td>
	            </tr>
	          </table>
	        </td>
	      </tr>
	    </table>
	  </body>
	</html>"""


def _state_key(local_dt: datetime) -> str:
    d = local_dt.date().isoformat()
    h = int(local_dt.hour)
    return f"{d}:{h:02d}"


def _should_attempt(db, key: str, retry_minutes: int) -> bool:
    state = db.marketing_advisor_state.find_one({"_id": "default"}) or {}
    if str(state.get("lastSentKey") or "") == key:
        return False
    if str(state.get("lastAttemptKey") or "") == key:
        last_attempt_at = state.get("lastAttemptAt")
        if isinstance(last_attempt_at, datetime):
            if datetime.utcnow() - last_attempt_at < timedelta(minutes=max(1, retry_minutes)):
                return False
    return True


def _claim_attempt(db, key: str, retry_minutes: int) -> bool:
    now = datetime.utcnow()
    retry_cutoff = now - timedelta(minutes=max(1, retry_minutes))
    res = db.marketing_advisor_state.update_one(
        {
            "_id": "default",
            "$and": [
                {"lastSentKey": {"$ne": key}},
                {
                    "$or": [
                        {"lastAttemptKey": {"$ne": key}},
                        {"lastAttemptAt": {"$lt": retry_cutoff}},
                        {"lastAttemptAt": {"$exists": False}},
                    ]
                },
            ],
        },
        {
            "$set": {
                "lastAttemptKey": key,
                "lastAttemptAt": now,
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
    )
    return bool(res.matched_count or res.upserted_id)


def send_advisor_email(to_email: str, *, kind: str = "scheduled") -> bool:
    db = get_db()
    local_now = _now_local()
    key = _state_key(local_now)
    retry_minutes = int(settings.marketing_advisor_retry_minutes or 30)
    if kind == "scheduled":
        # At most one scheduled advisor email per local day (fallback to the next recommended hour if the first is missed).
        try:
            state = db.marketing_advisor_state.find_one({"_id": "default"}) or {}
            if str(state.get("lastSentLocalDate") or "") == str(local_now.date().isoformat()):
                return False
        except Exception:
            pass
        if not _should_attempt(db, key, retry_minutes):
            return False
        if not _claim_attempt(db, key, retry_minutes):
            return False

    rec = build_advisor_recommendation(db, lookback_days=int(settings.marketing_advisor_lookback_days or 365))
    headline_hour = int(local_now.hour) if int(local_now.hour) in set(rec.recommended_send_hours or []) else (rec.recommended_send_hours[0] if rec.recommended_send_hours else None)
    subject_hour = _format_hour(headline_hour) if headline_hour is not None else "today"
    season_label = rec.season
    try:
        if "closed" in str(rec.operating_status or "").lower():
            season_label = f"{rec.season} • Off-season"
    except Exception:
        pass
    subject = f"Marketing advisor ({season_label}): best time to send ({subject_hour} SAST)"
    html = _advisor_email_html(rec, local_now, headline_hour)

    sent_at = datetime.utcnow()
    ok = False
    error = None
    try:
        ok = bool(send_email(subject=subject, body=html, body_html=html, to_address=str(to_email).strip()))
        if not ok:
            raise RuntimeError("SMTP send returned False")
    except Exception as e:
        ok = False
        error = str(e)
    finally:
        try:
            db.marketing_advisor_events.insert_one(
                {
                    "toEmail": str(to_email).strip().lower(),
                    "kind": str(kind),
                    "ok": bool(ok),
                    "error": error,
                    "sentAt": sent_at,
                    "localKey": key,
                }
            )
        except Exception:
            pass

        try:
            update: dict[str, Any] = {"lastAttemptOk": bool(ok), "lastError": error, "updatedAt": datetime.utcnow()}
            if ok and kind == "scheduled":
                update.update(
                    {
                        "lastSentKey": key,
                        "lastSentAt": sent_at,
                        "lastSentLocalDate": str(local_now.date().isoformat()),
                        "lastSentHour": int(local_now.hour),
                    }
                )
            db.marketing_advisor_state.update_one({"_id": "default"}, {"$set": update}, upsert=True)
        except Exception:
            pass
    return ok


async def marketing_advisor_loop() -> None:
    while True:
        try:
            if settings.marketing_advisor_enabled and settings.marketing_advisor_to:
                db = get_db()
                rec = build_advisor_recommendation(db, lookback_days=int(settings.marketing_advisor_lookback_days or 365))
                local_now = _now_local()
                # Send once during the recommended hour (best effort if the server starts late).
                if int(local_now.hour) in set(rec.recommended_send_hours or []) and int(local_now.minute) < 30:
                    await asyncio.to_thread(send_advisor_email, str(settings.marketing_advisor_to), kind="scheduled")
        except Exception as e:
            print(f"[marketing_advisor] loop error: {e}")
        await asyncio.sleep(max(30, int(settings.marketing_advisor_check_seconds or 300)))
