"""Application settings and configuration."""

from pydantic import Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # =========================================================================
    # Environment
    # =========================================================================
    debug: bool = False
    environment: str | None = None

    # =========================================================================
    # Logging
    # =========================================================================
    log_level: str | None = None
    log_format: str = "json"
    log_file: str | None = None

    # =========================================================================
    # Application Metadata
    # =========================================================================
    project_name: str = "Pólizas Service"
    project_version: str = "1.0.0"
    project_description: str = "Insurance policy query microservice"
    api_prefix: str = "/api/v1"

    # =========================================================================
    # CORS
    # =========================================================================
    # No cross-origin browser access unless origins are listed; the dashboard is
    # served from this same origin.
    cors_origins: list[str] = Field(default_factory=list)

    # =========================================================================
    # Dashboard
    # =========================================================================
    docs_url: str = ""  # Optional link to external docs shown in the dashboard sidebar

    # =========================================================================
    # SQL Server (Read-Only)
    # =========================================================================
    sqlserver_host: str = "localhost"
    sqlserver_port: int = 1433
    sqlserver_database: str = "insurance_core"
    sqlserver_user: str = "polizas_reader"
    sqlserver_password: SecretStr = SecretStr("")
    sqlserver_driver: str = "FreeTDS"  # FreeTDS para SQL Server 2008 R2
    sqlserver_timeout: int = 30
    sqlserver_tds_version: str = "7.3"  # TDS 7.3 para SQL Server 2008+

    # INTENTIONAL: plain @property, not @computed_field. Computed fields are part of
    # repr(settings) and model_dump(), which would put the password in any log line
    # or debug dump of the settings object.
    @property
    def sqlserver_connection_string(self) -> str:
        """Construct SQL Server connection string."""
        return (
            f"DRIVER={{{self.sqlserver_driver}}};"
            f"SERVER={self.sqlserver_host};"
            f"PORT={self.sqlserver_port};"
            f"DATABASE={self.sqlserver_database};"
            f"UID={self.sqlserver_user};"
            f"PWD={self.sqlserver_password.get_secret_value()};"
            f"TDS_Version={self.sqlserver_tds_version};"
        )

    # =========================================================================
    # Redis (Cache)
    # =========================================================================
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 1  # Different DB than other services
    redis_password: SecretStr | None = None
    cache_ttl_hours: int = 24

    # INTENTIONAL: plain @property for the same reason (it embeds the Redis password).
    @property
    def redis_url(self) -> str:
        """Construct Redis URL."""
        if self.redis_password:
            return (
                f"redis://:{self.redis_password.get_secret_value()}@"
                f"{self.redis_host}:{self.redis_port}/{self.redis_db}"
            )
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # =========================================================================
    # Identity Service (for token validation)
    # =========================================================================
    identity_service_url: str = "http://localhost:8080/api/v1"

    # External URL for Swagger UI tokenUrl (browser needs to reach this),
    # e.g. http://localhost:8080/identity/api/v1. If not set, falls back to
    # identity_service_url.
    identity_external_url: str = ""

    @computed_field
    @property
    def identity_login_url(self) -> str:
        """URL for Swagger UI login (must be browser-accessible)."""
        base = self.identity_external_url or self.identity_service_url
        return f"{base}/login"

    @computed_field
    @property
    def identity_me_url(self) -> str:
        """URL to validate tokens and get user info."""
        return f"{self.identity_service_url}/me"

    # =========================================================================
    # Rate Limiting
    # =========================================================================
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    # =========================================================================
    # Path Transformation (Network UNC → Local mount)
    # =========================================================================
    # Mount: /media/polizas = espejo del share UNC (ej. \\fileserver\policies\)
    # El prefijo UNC (\\server\share) se detecta y reemplaza por este valor.
    local_path_prefix: str = "/media/polizas"

    # Prefijos de paths excluidos (shares sin acceso).
    # Archivos con estos prefijos se marcan como available=False.
    # Valor en .env como JSON array: EXCLUDED_PATHS=["\\\\fileserver\\policies-urgent","c:\\PolicyPDF"]
    excluded_paths: list[str] = Field(default_factory=list)

    # =========================================================================
    # Cache
    # =========================================================================
    # Days to preload on startup (0 = disabled, 730 = 2 years)
    cache_warmup_days: int = 730
    # Sync interval in minutes (how often to check for new policies in SQL Server)
    cache_sync_interval_minutes: int = 5

    # =========================================================================
    # Validation
    # =========================================================================
    @model_validator(mode="after")
    def validate_settings(self):
        """Derive values and validate configuration."""
        if self.environment is None:
            self.environment = "development" if self.debug else "production"

        if self.log_level is None:
            self.log_level = "DEBUG" if self.debug else "INFO"

        return self


settings = Settings()
