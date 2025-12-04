from email.message import EmailMessage
import smtplib
import ssl
import time
import certifi
import re
from .config import settings

SAFETY_VIDEO_URL = "https://www.youtube.com/watch?v=5bZ37Hf82B0&t=11s"
INDEMNITY_FORM_URL = "https://www.jetskiandmore.com/interim-skipper-quiz"
INDEMNITY_DYNAMIC_BASE = "https://www.jetskiandmore.com/indemnity"

RIDE_LABELS = {
    '30-1': '30‑min Rental (1 Jet‑Ski)',
    '60-1': '60‑min Rental (1 Jet‑Ski)',
    '30-2': '30‑min Rental (2 Jet‑Skis)',
    '60-2': '60‑min Rental (2 Jet‑Skis)',
    '30-3': '30‑min Rental (3 Jet‑Skis)',
    '60-3': '60‑min Rental (3 Jet‑Skis)',
    '30-4': '30‑min Rental (4 Jet‑Skis)',
    '60-4': '60‑min Rental (4 Jet‑Skis)',
    '30-5': '30‑min Rental (5 Jet‑Skis)',
    '60-5': '60‑min Rental (5 Jet‑Skis)',
    'joy': 'Joy Ride (Instructed) • 10 min',
    'group': 'Group Session • 2 hr 30 min',
}


def _smtp_client():
    if not settings.gmail_user or not settings.gmail_app_password:
        raise RuntimeError("Gmail credentials not configured. Set JSM_GMAIL_USER and JSM_GMAIL_APP_PASSWORD")
    # Use certifi CA bundle to avoid local trust store issues
    context = ssl.create_default_context(cafile=certifi.where())

    # Prefer SMTPS (465); fall back to STARTTLS (587) if necessary
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context)
        server.login(settings.gmail_user, settings.gmail_app_password)
        return server
    except Exception:
        # Fallback: STARTTLS
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.ehlo()
        server.starttls(context=context)
        server.login(settings.gmail_user, settings.gmail_app_password)
        return server


def send_email(subject: str, body: str, to_address: str, reply_to: str | None = None, body_html: str | None = None):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"{settings.email_from_name} <{settings.gmail_user}>"
    msg['To'] = to_address
    if reply_to:
        msg['Reply-To'] = reply_to

    # If HTML provided or detected, send multipart with text fallback
    is_html = bool(body_html) or ('<' in (body or '') and ('<html' in body.lower() or '<table' in body.lower() or '<div' in body.lower()))
    if is_html:
        html = body_html or body
        # Naive plain-text fallback by stripping tags
        text = re.sub(r'<[^>]+>', '', html)
        msg.set_content(text)
        msg.add_alternative(html, subtype='html')
    else:
        msg.set_content(body)

    server = _smtp_client()
    try:
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _brand_name() -> str:
    return settings.email_from_name or "Jet Ski & More"


def _ride_label(ride_id: str | None, include_code: bool = False) -> str:
    ride_id = ride_id or "-"
    label = RIDE_LABELS.get(ride_id, ride_id)
    if include_code:
        return f"{label} ({ride_id})"
    return label


def _yesno(v: bool) -> str:
    return "Yes" if bool(v) else "No"


def _extract_passenger_names(raw_passengers) -> list[str]:
    names: list[str] = []
    for p in raw_passengers or []:
        if isinstance(p, dict):
            name = str(p.get("name") or "").strip()
        else:
            name = str(p or "").strip()
        if name:
            names.append(name)
    return names


def _info_table(rows: list[tuple[str, str]], label_width: int = 170) -> str:
    row_html = "".join(
        f"<tr><td style=\"padding:6px 0;font-size:14px;color:#6b7280;width:{label_width}px;\">{label}</td>"
        f"<td style=\"padding:6px 0;font-size:14px;color:#0f172a;\">{value}</td></tr>"
        for label, value in rows
    )
    return (
        f"<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" "
        f"style=\"border-collapse:collapse;margin:4px 0 4px 0;\">{row_html}</table>"
    )


def _addons_table(addons: dict, label_width: int = 170) -> str:
    a = addons or {}
    boat_has = a.get("boat")
    try:
        boat_count = max(1, int(a.get("boatCount") or 1))
    except Exception:
        boat_count = 1
    boat_value = f"{_yesno(boat_has)}{f' ({boat_count})' if boat_has else ''}".strip()
    try:
        extra_people = int(a.get("extraPeople") or 0)
    except Exception:
        extra_people = 0
    rows = [
        ("Drone footage", _yesno(a.get("drone"))),
        ("GoPro", _yesno(a.get("gopro"))),
        ("Wetsuit", _yesno(a.get("wetsuit"))),
        ("Boat passengers", boat_value),
        ("Extra people", str(extra_people)),
    ]
    return _info_table(rows, label_width=label_width)


def _passengers_block(raw_passengers, empty_message: str = "Passengers: none specified.") -> str:
    passenger_names = _extract_passenger_names(raw_passengers)
    if passenger_names:
        items = "".join(f"<li style='margin:2px 0;'>{name}</li>" for name in passenger_names)
        return (
            "<div style=\"margin:12px 0 0 0;\">"
            "<div style=\"font-size:13px;color:#6b7280;margin-bottom:6px;\">Passengers</div>"
            f"<ul style=\"margin:0;padding-left:18px;font-size:14px;color:#0f172a;\">{items}</ul>"
            "</div>"
        )
    return f"<div style=\"margin:12px 0 0 0;font-size:13px;color:#6b7280;\">{empty_message}</div>"


def _safety_section() -> str:
    return f"""
      <div style="margin:16px 0 0 0;padding:14px 16px;background:#fff7ed;border:1px solid #fdba74;border-radius:12px;">
        <div style="font-size:13px;font-weight:700;color:#9a3412;letter-spacing:0.02em;text-transform:uppercase;">Safety first</div>
        <p style="margin:8px 0 10px 0;color:#7c2d12;font-size:14px;line-height:1.55;">
          Please watch the safety briefing video and complete the indemnity form before you arrive.
          This is mandatory for all riders so we can check you in quickly on the day.
        </p>
        <div style="margin-top:6px;">
          <a href="{SAFETY_VIDEO_URL}" target="_blank" rel="noreferrer" style="display:inline-block;margin:0 10px 8px 0;padding:12px 14px;background:#0ea5e9;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:700;">Watch safety video</a>
          <a href="{INDEMNITY_FORM_URL}" target="_blank" rel="noreferrer" style="display:inline-block;margin:0 0 8px 0;padding:12px 14px;color:#0ea5e9;text-decoration:none;border-radius:8px;border:1px solid #0ea5e9;font-weight:700;">Complete indemnity form</a>
        </div>
        <div style="margin-top:6px;font-size:13px;font-weight:700;color:#9a3412;">Required: please finish both steps.</div>
      </div>
    """


def _wrap_user_email(
    title: str,
    hero: str,
    body_html: str,
    preheader: str = "",
    footer_note: str = "Reply to this email if you need any changes.",
    accent_color: str = "#0ea5e9",
) -> str:
    brand = _brand_name()
    preheader_html = (
        f"<div style=\"display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;\">{preheader}</div>"
        if preheader
        else ""
    )
    return f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{title} — {brand}</title>
      </head>
      <body style="margin:0;padding:0;background:#0b172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
        {preheader_html}
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:linear-gradient(140deg,#0b172a,#0f172a);padding:24px 14px;">
          <tr>
            <td align="center">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:700px;background:#ffffff;border:1px solid #e5e7eb;border-radius:16px;overflow:hidden;box-shadow:0 18px 60px rgba(12,40,74,0.18);">
                <tr>
                  <td style="background:{accent_color};color:#ffffff;padding:18px 22px;font-weight:700;font-size:18px;letter-spacing:0.01em;border-bottom:1px solid rgba(255,255,255,0.16);">
                    {hero}
                    <div style="margin-top:4px;font-size:13px;opacity:0.92;font-weight:500;">{brand}</div>
                  </td>
                </tr>
                <tr>
                  <td style="padding:22px 24px;">
                    {body_html}
                  </td>
                </tr>
                <tr>
                  <td style="padding:14px 22px;background:#f8fafc;border-top:1px solid #e5e7eb;color:#6b7280;font-size:12px;">
                    {footer_note}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """


def _summary_list(items: list[str]) -> str:
    if not items:
        return ""
    bullets = "".join(f"<li style='margin:4px 0;color:#0f172a;font-size:14px;'>{item}</li>" for item in items)
    return f"<ul style='margin:10px 0 0 0;padding-left:18px;'>{bullets}</ul>"


def format_booking_confirmation_email(
    booking: dict,
    participants: list[dict],
    booking_reference: str,
    booking_group_id: str,
    indemnity_links: dict[str, str],
) -> str:
    ride_id = booking.get("rideId")
    ride_label = _ride_label(ride_id, include_code=True)
    date = booking.get("date") or "-"
    time = booking.get("time") or "-"
    addons = booking.get("addons") or {}
    summary_rows = [
        ("Booking reference", booking_reference),
        ("Booking group", booking_group_id),
        ("Ride", ride_label),
        ("Date", date),
        ("Time", time),
        ("Jet skis", str(booking.get("numberOfJetSkis") or 1)),
    ]
    body_rows = _info_table(summary_rows)
    addons_table = _addons_table(addons, label_width=170)
    participant_items: list[str] = []
    for p in participants:
        role_label = p.get("role") or "Participant"
        name = p.get("fullName") or "Guest"
        link = indemnity_links.get(str(p.get("_id") or p.get("id") or ""))
        link_html = f' — <a href="{link}">Indemnity link</a>' if link else ""
        participant_items.append(f"{role_label}: {name}{link_html}")
    participants_html = _summary_list(participant_items) if participant_items else "<p style='color:#6b7280;'>No participants captured.</p>"
    safety = _safety_section()
    body = f"""
      <p style="font-size:15px;color:#0f172a;">Thanks for booking with Jet Ski &amp; More. Here are your details:</p>
      {body_rows}
      <div style="margin-top:12px;font-weight:600;color:#0f172a;">Participants</div>
      {participants_html}
      <div style="margin-top:12px;font-weight:600;color:#0f172a;">Add-ons</div>
      {addons_table}
      {safety}
    """
    return _wrap_user_email(
        title="Booking confirmed",
        hero="Booking confirmed",
        body_html=body,
        preheader=f"Booking {booking_reference} confirmed",
    )


def format_participant_notification(
    primary_name: str,
    participant: dict,
    booking_reference: str,
    booking_group_id: str,
    ride_label: str,
    date: str | None,
    time: str | None,
    indemnity_link: str | None,
) -> str:
    role = participant.get("role") or "Participant"
    name = participant.get("fullName") or "Guest"
    indemnity_button = (
        f'<a href="{indemnity_link}" target="_blank" rel="noreferrer" '
        'style="display:inline-block;margin:0 0 10px 0;padding:12px 14px;'
        'color:#0ea5e9;text-decoration:none;border-radius:8px;'
        'border:1px solid #0ea5e9;font-weight:700;">Complete indemnity</a>'
        if indemnity_link
        else ""
    )
    body = f"""
      <p style="font-size:15px;color:#0f172a;">{primary_name} booked a ride and listed you as {role.lower().replace('_', ' ')}.</p>
      {_info_table([
        ("Booking reference", booking_reference),
        ("Booking group", booking_group_id),
        ("Ride", ride_label),
        ("Date", date or "-"),
        ("Time", time or "-"),
        ("Your role", role),
      ])}
      <p style="margin-top:10px;font-weight:600;color:#0f172a;">What to do next</p>
      <ol style="padding-left:18px;margin:6px 0;color:#0f172a;font-size:14px;">
        <li>Watch the safety video before arrival.</li>
        <li>Complete your indemnity form.</li>
      </ol>
      <div style="margin:12px 0;">
        <a href="{SAFETY_VIDEO_URL}" target="_blank" rel="noreferrer" style="display:inline-block;margin:0 10px 10px 0;padding:12px 14px;background:#0ea5e9;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:700;">Watch safety video</a>
        {indemnity_button}
      </div>
      <p style="color:#475569;font-size:13px;">Role: {role} • Name: {name}</p>
    """
    return _wrap_user_email(
        title="You’re on a Jet Ski booking",
        hero="You’re on a booking",
        body_html=body,
        preheader=f"{primary_name} booked a ride and listed you as {role}",
    )


def build_indemnity_link(base: str, token: str) -> str:
    return f"{base}?token={token}"


def format_booking_email(data: dict) -> str:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    brand = _brand_name()

    a = data.get('addons') or {}
    addons_html = f"""
      <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;margin:6px 0 0 0;\">
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;width:180px;\">Drone footage</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{_yesno(a.get('drone'))}</td>
        </tr>
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;\">GoPro</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{_yesno(a.get('gopro'))}</td>
        </tr>
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;\">Wetsuit</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{_yesno(a.get('wetsuit'))}</td>
        </tr>
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;\">Boat passengers</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{'%s (%s)' % (('Yes' if a.get('boat') else 'No'), ('%d' % max(1, int(a.get('boatCount') or 1))) if a.get('boat') else '')}</td>
        </tr>
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;\">Extra people</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{int(a.get('extraPeople') or 0)}</td>
        </tr>
      </table>
    """

    full_name = str(data.get('fullName') or '').strip()
    email = str(data.get('email') or '').strip()
    phone = str(data.get('phone') or '').strip()
    notes = str(data.get('notes') or '').strip()

    # Passengers (optional)
    raw_passengers = data.get('passengers') or []
    passenger_names: list[str] = []
    for p in raw_passengers:
        if isinstance(p, dict):
            name = str(p.get('name') or '').strip()
        else:
            name = str(p or '').strip()
        if name:
            passenger_names.append(name)
    if passenger_names:
        passengers_html = """
          <div style="margin-top:12px;">
            <div style="font-size:13px;color:#6b7280;margin-bottom:6px;">Passengers</div>
            <ul style="margin:0;padding-left:18px;font-size:14px;color:#111827;">
        """ + "".join(
            f"<li>Passenger {idx + 1}: {name}</li>" for idx, name in enumerate(passenger_names)
        ) + "</ul></div>"
    else:
        passengers_html = """
          <div style="margin-top:12px;font-size:13px;color:#6b7280;">
            Passengers: none specified.
          </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>New booking — {brand}</title>
      </head>
      <body style=\"margin:0;padding:0;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;\">
        <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"background:#f6f7f9;padding:24px;\">
          <tr>
            <td align=\"center\">
              <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"max-width:640px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;\">
                <tr>
                  <td style=\"background:#0ea5e9;color:#ffffff;padding:16px 20px;font-weight:600;font-size:16px;\">
                    {brand} — New booking request
                  </td>
                </tr>
                <tr>
                  <td style=\"padding:20px;\">
                    <p style=\"margin:0 0 12px 0;color:#374151;\">A new booking request was submitted.</p>
                    <div style=\"margin:0 0 10px 0;\">
                      <div style=\"font-size:13px;color:#6b7280;margin-bottom:4px;\">Session</div>
                      <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;margin:4px 0 10px 0;\">
                        <tr>
                          <td style=\"padding:6px 0;font-size:14px;color:#6b7280;width:180px;\">Received</td>
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{ts}</td>
                        </tr>
                        <tr>
                          <td style=\"padding:6px 0;font-size:14px;color:#6b7280;\">Ride</td>
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{_ride_label(data.get('rideId'), include_code=True)}</td>
                        </tr>
                        <tr>
                          <td style=\"padding:6px 0;font-size:14px;color:#6b7280;\">Date</td>
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{data.get('date') or '-'}</td>
                        </tr>
                        <tr>
                          <td style=\"padding:6px 0;font-size:14px;color:#6b7280;\">Time</td>
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{data.get('time') or '-'}</td>
                        </tr>
                      </table>
                    </div>
                    <div style=\"margin:0 0 10px 0;\">
                      <div style=\"font-size:13px;color:#6b7280;margin-bottom:4px;\">Person who booked</div>
                      <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;margin:4px 0 10px 0;\">
                        <tr>
                          <td style=\"padding:6px 0;font-size:14px;color:#6b7280;width:180px;\">Name</td>
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{full_name or '-'}</td>
                        </tr>
                        <tr>
                          <td style=\"padding:6px 0;font-size:14px;color:#6b7280;\">Email</td>
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{email or '-'}</td>
                        </tr>
                        <tr>
                          <td style=\"padding:6px 0;font-size:14px;color:#6b7280;\">Phone</td>
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{phone or '-'}</td>
                        </tr>
                      </table>
                    </div>
                    {passengers_html}
                    <div style=\"margin:10px 0 0 0;\">
                      <div style=\"font-size:13px;color:#6b7280;margin-bottom:6px;\">Add-ons</div>
                      {addons_html}
                    </div>
                    <div style=\"margin-top:14px;padding:12px 14px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:6px;\">
                      <div style=\"font-size:13px;color:#6b7280;margin-bottom:6px;\">Notes</div>
                      <div style=\"white-space:pre-wrap;font-size:15px;color:#111827;\">{notes or '-'}</div>
                    </div>
                  </td>
                </tr>
                <tr>
                  <td style=\"padding:14px 20px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#6b7280;font-size:12px;\">
                    This email was sent automatically from your website booking form.
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    return html


def format_contact_email(data: dict) -> str:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    brand = settings.email_from_name or "Jet Ski & More"
    full_name = str(data.get('fullName') or '').strip()
    email = str(data.get('email') or '').strip()
    phone = str(data.get('phone') or '').strip()
    message = str(data.get('message') or '').strip()

    # Simple, mobile-friendly HTML with inline styles
    html = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>New contact — {brand}</title>
      </head>
      <body style="margin:0;padding:0;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f6f7f9;padding:24px;">
          <tr>
            <td align="center">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
                <tr>
                  <td style="background:#0ea5e9;color:#ffffff;padding:16px 20px;font-weight:600;font-size:16px;">
                    {brand} — New contact message
                  </td>
                </tr>
                <tr>
                  <td style="padding:20px;">
                    <p style="margin:0 0 12px 0;color:#374151;">You received a new message via the contact form.</p>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:12px 0 16px 0;">
                      <tr>
                        <td style="padding:6px 0;font-size:14px;color:#6b7280;width:120px;">Received</td>
                        <td style="padding:6px 0;font-size:14px;color:#111827;">{ts}</td>
                      </tr>
                      <tr>
                        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Name</td>
                        <td style="padding:6px 0;font-size:14px;color:#111827;">{full_name or '-'}</td>
                      </tr>
                      <tr>
                        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Email</td>
                        <td style="padding:6px 0;font-size:14px;color:#111827;">{email or '-'}</td>
                      </tr>
                      <tr>
                        <td style="padding:6px 0;font-size:14px;color:#6b7280;">Phone</td>
                        <td style="padding:6px 0;font-size:14px;color:#111827;">{phone or '-'}</td>
                      </tr>
                    </table>
                    <div style="margin-top:8px;padding:12px 14px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:6px;">
                      <div style="font-size:13px;color:#6b7280;margin-bottom:6px;">Message</div>
                      <div style="white-space:pre-wrap;font-size:15px;color:#111827;">{message or '-'}</div>
                    </div>
                  </td>
                </tr>
                <tr>
                  <td style="padding:14px 20px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#6b7280;font-size:12px;">
                    This email was sent automatically from your website contact form.
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    return html


def format_payment_admin_email(booking: dict, amount_in_cents: int, charge_id: str, status: str) -> str:
    brand = _brand_name()
    amount_num = int(amount_in_cents) // 100
    amount = f"ZAR {amount_num:,}".replace(',', ' ')
    a = booking.get('addons') or {}
    addons_rows = f"""
      <tr><td style='padding:4px 0;color:#6b7280;width:180px'>Drone footage</td><td style='padding:4px 0;color:#111827'>{_yesno(a.get('drone'))}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>GoPro</td><td style='padding:4px 0;color:#111827'>{_yesno(a.get('gopro'))}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>Wetsuit</td><td style='padding:4px 0;color:#111827'>{_yesno(a.get('wetsuit'))}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>Boat passengers</td><td style='padding:4px 0;color:#111827'>{('%s (%s)' % (('Yes' if a.get('boat') else 'No'), ('%d' % max(1, int(a.get('boatCount') or 1))) if a.get('boat') else '')).strip()}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>Extra people</td><td style='padding:4px 0;color:#111827'>{int(a.get('extraPeople') or 0)}</td></tr>
    """
    # Passengers
    raw_passengers = booking.get('passengers') or []
    passenger_names: list[str] = []
    for p in raw_passengers:
        if isinstance(p, dict):
            name = str(p.get('name') or '').strip()
        else:
            name = str(p or '').strip()
        if name:
            passenger_names.append(name)
    if passenger_names:
        passengers_html = """
            <div style="margin:10px 0 12px 0;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:6px;">Passengers</div>
              <ul style="margin:0;padding-left:18px;font-size:14px;color:#111827;">
        """ + "".join(
            f"<li>Passenger {idx + 1}: {name}</li>" for idx, name in enumerate(passenger_names)
        ) + "</ul></div>"
    else:
        passengers_html = """
            <div style="margin:10px 0 12px 0;font-size:13px;color:#6b7280;">
              Passengers: none specified.
            </div>
        """
    html = f"""
    <!DOCTYPE html>
    <html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width, initial-scale=1'/><title>Paid booking — {brand}</title></head>
    <body style="margin:0;padding:0;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f6f7f9;padding:24px;"><tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
          <tr><td style="background:#10b981;color:#ffffff;padding:16px 20px;font-weight:600;font-size:16px;">{brand} — Paid booking</td></tr>
          <tr><td style="padding:20px;">
            <div style="margin:0 0 10px 0;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Session</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:4px 0 8px 0;">
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;width:180px;">Ride</td><td style="padding:6px 0;font-size:14px;color:#111827;">{_ride_label(booking.get('rideId'), include_code=True)}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Date</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('date') or '-'}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Time</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('time') or '-'}</td></tr>
              </table>
            </div>
            <div style="margin:0 0 10px 0;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Person who booked</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:4px 0 8px 0;">
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;width:180px;">Name</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('fullName') or '-'}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Email</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('email') or '-'}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Phone</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('phone') or '-'}</td></tr>
              </table>
            </div>
            {passengers_html}
            <div style="margin:10px 0 12px 0;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:6px;">Add-ons</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{addons_rows}</table>
            </div>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:12px 0 0 0;">
              <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;width:180px;">Amount</td><td style="padding:6px 0;font-size:14px;color:#111827;">{amount}</td></tr>
              <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Payment reference</td><td style="padding:6px 0;font-size:14px;color:#111827;">{charge_id}</td></tr>
              <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Status</td><td style="padding:6px 0;font-size:14px;color:#111827;">{status}</td></tr>
            </table>
          </td></tr>
          <tr><td style="padding:14px 20px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#6b7280;font-size:12px;">This email was sent automatically after a successful payment.</td></tr>
        </table>
      </td></tr></table>
    </body></html>
    """
    return html


def format_payment_client_email(booking: dict, amount_in_cents: int, charge_id: str) -> str:
    brand = _brand_name()
    amount_num = int(amount_in_cents) // 100
    amount = f"ZAR {amount_num:,}".replace(',', ' ')
    session_rows = [
        ("Ride", _ride_label(booking.get('rideId'))),
        ("Date", booking.get('date') or '-'),
        ("Time", booking.get('time') or '-'),
        ("Amount", amount),
        ("Payment reference", charge_id),
    ]
    passengers_html = _passengers_block(booking.get('passengers') or [], "Passengers: none added yet.")
    addons_html = _addons_table(booking.get('addons') or {}, label_width=170)
    body_html = f"""
        <p style="margin:0 0 12px 0;color:#0f172a;font-size:15px;line-height:1.6;">
          Thanks for booking with {brand}! We have received your payment of <strong>{amount}</strong>.
          Your spot is being held and we'll confirm availability shortly.
        </p>
        <div style="margin:14px 0 0 0;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
          <div style="padding:12px 14px;background:#f8fafc;border-bottom:1px solid #e5e7eb;font-size:13px;font-weight:700;color:#0f172a;">Booking overview</div>
          <div style="padding:14px 16px;">
            {_info_table(session_rows, label_width=170)}
          </div>
        </div>
        {passengers_html}
        <div style="margin:14px 0 0 0;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
          <div style="padding:12px 14px;background:#f8fafc;border-bottom:1px solid #e5e7eb;font-size:13px;font-weight:700;color:#0f172a;">Add-ons</div>
          <div style="padding:14px 16px;">
            {addons_html}
          </div>
        </div>
        {_safety_section()}
        <p style="margin:14px 0 0 0;color:#0f172a;font-size:14px;line-height:1.6;">
          If anything needs to change, just reply to this email and we'll help. We look forward to getting you on the water!
        </p>
    """
    return _wrap_user_email(
        title="Booking confirmed",
        hero="Booking confirmed — payment received",
        body_html=body_html,
        preheader=f"Payment received for your {brand} booking. Watch the safety video and complete the indemnity form before arrival.",
        footer_note="This email was sent automatically after your payment. Reply if you need any changes.",
    )


def format_booking_status_update_email(booking: dict, new_status: str, message: str) -> str:
    brand = _brand_name()
    status_label = (new_status or "updated").replace("_", " ").title()
    accent_color = {
        "approved": "#10b981",
        "confirmed": "#0f766e",
        "paid": "#0ea5e9",
        "cancelled": "#ef4444",
        "canceled": "#ef4444",
    }.get((new_status or "").lower(), "#0ea5e9")

    session_rows = [
        ("Ride", _ride_label(booking.get('rideId'))),
        ("Date", booking.get('date') or '-'),
        ("Time", booking.get('time') or '-'),
        ("Status", status_label),
    ]
    passengers_html = _passengers_block(booking.get("passengers") or [], "Passengers: none added yet.")
    message_block = f"""
      <div style="margin:14px 0 0 0;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
        <div style="padding:12px 14px;background:#f8fafc;border-bottom:1px solid #e5e7eb;font-size:13px;font-weight:700;color:#0f172a;">Message from the team</div>
        <div style="padding:14px 16px;">
          <div style="white-space:pre-wrap;font-size:14px;color:#0f172a;line-height:1.6;">{message or 'No extra message provided.'}</div>
        </div>
      </div>
    """
    body_html = f"""
        <p style="margin:0 0 12px 0;color:#0f172a;font-size:15px;line-height:1.6;">
          We updated your booking status to <strong>{status_label}</strong>.
        </p>
        <div style="display:inline-block;padding:6px 10px;margin:0 0 12px 0;background:#e0f2fe;color:#075985;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:0.02em;text-transform:uppercase;">{status_label}</div>
        <div style="margin:8px 0 0 0;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
          <div style="padding:12px 14px;background:#f8fafc;border-bottom:1px solid #e5e7eb;font-size:13px;font-weight:700;color:#0f172a;">Booking details</div>
          <div style="padding:14px 16px;">
            {_info_table(session_rows, label_width=170)}
          </div>
        </div>
        {passengers_html}
        {message_block}
        {_safety_section()}
        <p style="margin:14px 0 0 0;color:#0f172a;font-size:14px;line-height:1.6;">Reply if you have any questions or updates.</p>
    """
    return _wrap_user_email(
        title="Booking update",
        hero=f"Booking update — {status_label}",
        body_html=body_html,
        preheader=f"Your {brand} booking is now {status_label.lower()}. Watch the safety video and complete indemnity inside.",
        footer_note="This email was sent automatically when your booking status changed.",
        accent_color=accent_color,
    )
