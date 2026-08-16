from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Lead Qualification Agent"
    debug: bool = False
    environment: str = "development"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_agent"
    redis_url: str | None = None

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_temperature: float = 0.1
    gemini_timeout: float = 30.0
    gemini_rpm_limit: int = 10

    langchain_tracing_v2: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "lead-qualification-agent"

    max_conversation_history: int = 50
    qualification_threshold_hot: float = 0.7
    qualification_threshold_warm: float = 0.4
    human_handoff_confidence: float = 0.3

    api_key: str = ""
    demo_token_secret: str = ""
    demo_token_ttl_seconds: int = 7200
    demo_token_rpm_limit: int = 8
    webhook_rpm_limit: int = 60
    allowed_origins: list[str] = ["*"]

    vertical: str = "generic"
    business_name: str = "our company"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    sms_enabled: bool = False

    resend_api_key: str = ""
    resend_from_email: str = ""

    calendly_api_key: str = ""
    calendly_event_type_uri: str = ""
    calendly_user_uri: str = ""
    calendly_enabled: bool = False

    calendar_availability_days: int = 14
    meeting_duration_minutes: int = 30

    # Multi-tenancy & auth
    auth_enabled: bool = False
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_minutes: int = 30
    jwt_refresh_token_ttl_days: int = 30
    password_reset_token_ttl_minutes: int = 30

    # Rate limits for the public auth endpoints. NOTE: "rpm" here means
    # "requests per limiter window", NOT requests per minute (unlike the
    # per-minute limits above). Forgot-password is keyed by the target email
    # and uses a 1-hour window to stop mail-bombing; login is keyed by the
    # (email, IP) pair with a 15-minute window to stop brute-forcing.
    password_reset_rpm_limit: int = 3
    login_rpm_limit: int = 10

    # Default tenant for single-tenant (legacy) mode
    default_tenant_name: str = "Default Organization"

    # White-labeling / custom domains
    app_hostname: str = ""

    # Sentry
    sentry_dsn: str = ""

    # CRM integrations
    crm_encryption_key: str = ""
    fub_api_key: str = ""
    kvcore_api_key: str = ""
    kvcore_api_secret: str = ""
    ams360_api_key: str = ""
    ams360_api_secret: str = ""

    # Billing / Stripe
    stripe_enabled: bool = False
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""


settings = Settings()
