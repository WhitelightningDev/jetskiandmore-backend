from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    # CORS
    allowed_origins: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://www.jetskiandmore.com",
    ]

    # Gmail SMTP (recommended: App Password)
    gmail_user: str | None = None
    gmail_app_password: str | None = None
    email_to: str | None = None  # default admin recipient
    email_from_name: str = "Jet Ski & More"

    # Yoco
    yoco_public_key: str | None = None
    yoco_secret_key: str | None = None
    yoco_checkout_token: str | None = None  # Optional; falls back to secret key
    site_base_url: str | None = None  # Optional; default: first allowed_origin

    # MongoDB
    mongodb_uri: str | None = None
    mongodb_db: str | None = None
    # Yoco OAuth (for new API incl. Payment Links)
    yoco_client_id: str | None = None
    yoco_client_secret: str | None = None

    # Admin auth (for dashboard)
    admin_email: str | None = None
    admin_password: str | None = None
    admin_jwt_secret: str = "change-me-in-prod"

    # Marketing advisor (automated email suggestions)
    marketing_advisor_enabled: bool = False
    marketing_advisor_to: str | None = None
    marketing_advisor_check_seconds: int = 300
    marketing_advisor_retry_minutes: int = 30
    marketing_advisor_lookback_days: int = 365
    marketing_advisor_industry: str = "Jet ski rentals & water activities"
    marketing_advisor_location: str = "South Africa (Africa/Johannesburg)"
    marketing_advisor_mode: str = "auto"  # auto | winter_ramp | spring_ramp | summer_peak

    class Config:
        env_file = ".env"
        env_prefix = "JSM_"


settings = Settings()
