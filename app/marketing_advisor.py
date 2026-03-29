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


def build_advisor_recommendation(db, lookback_days: int = 365) -> AdvisorRecommendation:
    lb = max(14, min(int(lookback_days or 365), 730))
    cutoff = datetime.utcnow() - timedelta(days=lb)

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
    holidays = _upcoming_sa_holidays(today_local, days=120)

    booking_url = "https://www.jetskiandmore.com/Bookings"
    what_to_send = [
        "Weather window updates (confirming go/no-go and next available slots)",
        "Midweek specials and quiet-sea availability",
        "Weekend sell-out reminders (limited slots)",
        "Partner-facing credibility notes: safety briefing + onboarding + procedures",
        "Group booking availability for birthdays / corporates",
        "Thank-you / service recovery follow-ups after weather cancellations",
    ]
    what_not_to_send = [
        "Late-night sends (keeps engagement low and raises spam complaints)",
        "Overly aggressive discounting (hurts perceived quality)",
        "Vague or informal wording (use factual, professional tone)",
        "Too many emails in a short period (batch thoughtfully)",
    ]

    ideas: list[dict[str, Any]] = [
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
            "ctaLabel": "View availability",
            "ctaUrl": booking_url,
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
        {
            "title": "Midweek calm-seas special",
            "subject": "Midweek on the water — quieter, easier to book",
            "preheader": "If your schedule is flexible, midweek has great availability.",
            "content": (
                "Hi there,\n\n"
                "If you can go midweek, you’ll often find more availability and a calmer, more relaxed experience.\n\n"
                "Choose your ride time and we’ll take you through our structured customer briefing and onboarding.\n\n"
                "Check availability and pick a slot that works for you."
            ),
            "ctaLabel": "Check availability",
            "ctaUrl": booking_url,
        },
    ]

    return AdvisorRecommendation(
        recommended_send_hours=rec,
        peak_booking_hours=peak_hours,
        upcoming_holidays=holidays,
        what_to_send=what_to_send,
        what_not_to_send=what_not_to_send,
        ideas=ideas,
    )


def _advisor_email_html(rec: AdvisorRecommendation, now_local: datetime, headline_hour: Optional[int]) -> str:
    brand = settings.email_from_name or "Jet Ski & More"
    rec_hours = ", ".join(_format_hour(h) for h in rec.recommended_send_hours) or "—"
    peak_hours = ", ".join(_format_hour(h) for h in rec.peak_booking_hours) or "—"
    holidays = rec.upcoming_holidays[:6]

    def li(items: list[str]) -> str:
        return "".join(f"<li style='margin:4px 0;'>{item}</li>" for item in items)

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
                  <div style="font-size:12px;letter-spacing:0.24em;text-transform:uppercase;color:#64748b;">Upcoming holidays</div>
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin-top:8px;">{holiday_rows}</table>
                </div>

                <div style="margin-top:16px;padding-top:14px;border-top:1px solid #e2e8f0;font-size:12px;color:#64748b;">
                  Generated at {now_local.strftime("%Y-%m-%d %H:%M")} SAST • Industry: {settings.marketing_advisor_industry} • Location: {settings.marketing_advisor_location}
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
    subject = f"Marketing advisor: best time to send ({subject_hour} SAST)"
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
