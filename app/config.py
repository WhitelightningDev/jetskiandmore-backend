from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    # CORS
    allowed_origins: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://jetskiandmore-frontend.vercel.app",
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

    class Config:
        env_file = ".env"
        env_prefix = "JSM_"


settings = Settings()
