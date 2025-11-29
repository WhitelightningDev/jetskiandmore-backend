from email.message import EmailMessage
import smtplib
import ssl
import time
import certifi
import re
from .config import settings


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


def format_booking_email(data: dict) -> str:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    brand = settings.email_from_name or "Jet Ski & More"

    def ride_label(ride_id: str | None) -> str:
        labels = {
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
        ride_id = ride_id or '-'
        return f"{labels.get(ride_id, ride_id)} ({ride_id})"

    def yesno(v: bool) -> str:
        return 'Yes' if bool(v) else 'No'

    a = data.get('addons') or {}
    addons_html = f"""
      <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;margin:6px 0 0 0;\">
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;width:180px;\">Drone footage</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{yesno(a.get('drone'))}</td>
        </tr>
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;\">GoPro</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{yesno(a.get('gopro'))}</td>
        </tr>
        <tr>
          <td style=\"padding:4px 0;font-size:14px;color:#6b7280;\">Wetsuit</td>
          <td style=\"padding:4px 0;font-size:14px;color:#111827;\">{yesno(a.get('wetsuit'))}</td>
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
                          <td style=\"padding:6px 0;font-size:14px;color:#111827;\">{ride_label(data.get('rideId'))}</td>
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
    brand = settings.email_from_name or "Jet Ski & More"
    amount_num = int(amount_in_cents) // 100
    amount = f"ZAR {amount_num:,}".replace(',', ' ')
    def ride_label(ride_id: str | None) -> str:
        labels = {
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
        ride_id = ride_id or '-'
        return f"{labels.get(ride_id, ride_id)} ({ride_id})"
    def yesno(v: bool) -> str:
        return 'Yes' if bool(v) else 'No'
    a = booking.get('addons') or {}
    addons_rows = f"""
      <tr><td style='padding:4px 0;color:#6b7280;width:180px'>Drone footage</td><td style='padding:4px 0;color:#111827'>{yesno(a.get('drone'))}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>GoPro</td><td style='padding:4px 0;color:#111827'>{yesno(a.get('gopro'))}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>Wetsuit</td><td style='padding:4px 0;color:#111827'>{yesno(a.get('wetsuit'))}</td></tr>
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
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;width:180px;">Ride</td><td style="padding:6px 0;font-size:14px;color:#111827;">{ride_label(booking.get('rideId'))}</td></tr>
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
    brand = settings.email_from_name or "Jet Ski & More"
    amount_num = int(amount_in_cents) // 100
    amount = f"ZAR {amount_num:,}".replace(',', ' ')
    def ride_label(ride_id: str | None) -> str:
        labels = {
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
        ride_id = ride_id or '-'
        return labels.get(ride_id, ride_id)
    def yesno(v: bool) -> str:
        return 'Yes' if bool(v) else 'No'
    a = booking.get('addons') or {}
    addons_rows = f"""
      <tr><td style='padding:4px 0;color:#6b7280;width:160px'>Drone footage</td><td style='padding:4px 0;color:#111827'>{yesno(a.get('drone'))}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>GoPro</td><td style='padding:4px 0;color:#111827'>{yesno(a.get('gopro'))}</td></tr>
      <tr><td style='padding:4px 0;color:#6b7280'>Wetsuit</td><td style='padding:4px 0;color:#111827'>{yesno(a.get('wetsuit'))}</td></tr>
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
    <html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width, initial-scale=1'/><title>Booking confirmed — {brand}</title></head>
    <body style="margin:0;padding:0;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f6f7f9;padding:24px;"><tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
          <tr><td style="background:#0ea5e9;color:#ffffff;padding:16px 20px;font-weight:600;font-size:16px;">Thank you — payment received</td></tr>
          <tr><td style="padding:20px;">
            <p style='margin:0 0 10px 0;color:#374151;'>We\'ve received your payment of <strong>{amount}</strong>.</p>
            <div style="margin:0 0 10px 0;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Your session</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:4px 0 8px 0;">
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;width:160px;">Ride</td><td style="padding:6px 0;font-size:14px;color:#111827;">{ride_label(booking.get('rideId'))}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Date</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('date') or '-'}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Time</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('time') or '-'}</td></tr>
              </table>
            </div>
            {passengers_html}
            <div style="margin:10px 0 12px 0;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:6px;">Add-ons</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">{addons_rows}</table>
            </div>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:12px 0 0 0;">
              <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;width:160px;">Payment reference</td><td style="padding:6px 0;font-size:14px;color:#111827;">{charge_id}</td></tr>
            </table>
            <p style='margin:12px 0 0 0;color:#374151;'>We\'ll confirm availability shortly and get you riding. If you have any questions, simply reply to this email.</p>
          </td></tr>
          <tr><td style="padding:14px 20px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#6b7280;font-size:12px;">This email was sent automatically after your payment.</td></tr>
        </table>
      </td></tr></table>
    </body></html>
    """
    return html


def format_booking_status_update_email(booking: dict, new_status: str, message: str) -> str:
    brand = settings.email_from_name or "Jet Ski & More"
    status_label = new_status.capitalize()

    def ride_label(ride_id: str | None) -> str:
        labels = {
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
        ride_id = ride_id or '-'
        return labels.get(ride_id, ride_id)

    raw_passengers = booking.get("passengers") or []
    passenger_names: list[str] = []
    for p in raw_passengers:
        if isinstance(p, dict):
            name = str(p.get("name") or "").strip()
        else:
            name = str(p or "").strip()
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
    <html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width, initial-scale=1'/><title>Booking update — {brand}</title></head>
    <body style="margin:0;padding:0;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f6f7f9;padding:24px;"><tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
          <tr><td style="background:#0f766e;color:#ffffff;padding:16px 20px;font-weight:600;font-size:16px;">Booking update — {status_label}</td></tr>
          <tr><td style="padding:20px;">
            <p style='margin:0 0 10px 0;color:#374151;'>We&apos;ve updated the status of your booking.</p>
            <div style="margin:0 0 10px 0;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Session</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:4px 0 8px 0;">
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;width:160px;">Ride</td><td style="padding:6px 0;font-size:14px;color:#111827;">{ride_label(booking.get('rideId'))}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Date</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('date') or '-'}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">Time</td><td style="padding:6px 0;font-size:14px;color:#111827;">{booking.get('time') or '-'}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#6b7280;">New status</td><td style="padding:6px 0;font-size:14px;color:#111827;">{status_label}</td></tr>
              </table>
            </div>
            {passengers_html}
            <div style="margin-top:12px;padding:12px 14px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:6px;">
              <div style="font-size:13px;color:#6b7280;margin-bottom:6px;">Message from the team</div>
              <div style="white-space:pre-wrap;font-size:15px;color:#111827;">{message}</div>
            </div>
            <p style='margin:12px 0 0 0;color:#374151;'>If you have any questions, simply reply to this email.</p>
          </td></tr>
          <tr><td style="padding:14px 20px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#6b7280;font-size:12px;">This email was sent automatically when your booking status changed.</td></tr>
        </table>
      </td></tr></table>
    </body></html>
    """
    return html
