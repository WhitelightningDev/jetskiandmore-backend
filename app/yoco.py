import json
from .config import settings
import httpx
import base64
import time


class YocoError(Exception):
    pass


_OAUTH_CACHE: dict[str, tuple[str, float]] = {}


def _get_oauth_token(scopes: str) -> str:
    if not settings.yoco_client_id or not settings.yoco_client_secret:
        raise YocoError("Yoco OAuth credentials not configured. Set JSM_YOCO_CLIENT_ID and JSM_YOCO_CLIENT_SECRET")
    cached = _OAUTH_CACHE.get(scopes)
    now = time.time()
    if cached and now < cached[1] - 60:
        return cached[0]
    basic = base64.b64encode(f"{settings.yoco_client_id}:{settings.yoco_client_secret}".encode("utf-8")).decode("utf-8")
    try:
        r = httpx.post(
            "https://api.yoco.com/v1/oauth2/token",
            data={"grant_type": "client_credentials", "scope": scopes},
            headers={"Authorization": f"Basic {basic}"},
            timeout=30,
        )
    except httpx.RequestError as e:
        raise YocoError(f"Network error during Yoco OAuth: {e}")
    if r.status_code >= 400:
        raise YocoError(f"Yoco OAuth token failed: {r.text}")
    data = r.json()
    token = data.get("access_token")
    expires_in = int(data.get("expires_in") or 300)
    _OAUTH_CACHE[scopes] = (token, now + expires_in)
    return token


def create_charge(token: str, amount: int, currency: str = "ZAR", email: str | None = None, reference: str | None = None):
    if not settings.yoco_secret_key:
        raise YocoError("Yoco secret key not configured. Set JSM_YOCO_SECRET_KEY")
    if amount <= 0:
        raise YocoError("Invalid amount")

    payload = {
        "token": token,
        "amountInCents": int(amount),
        "currency": currency,
    }
    if email:
        payload["email"] = email
    if reference:
        payload["reference"] = reference

    try:
        r = httpx.post(
            "https://api.yoco.com/v1/charges/",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.yoco_secret_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                err = {"error": r.text}
            raise YocoError(f"Yoco charge failed: {err}")
        return r.json()
    except httpx.RequestError as e:
        raise YocoError(f"Network error contacting Yoco: {e}")
