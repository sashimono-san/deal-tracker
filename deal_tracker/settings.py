import os
import shutil
from email.headerregistry import Address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator

from .models import StrictModel, public_url


class Settings(StrictModel):
    collection_enabled: bool = False
    scheduler_enabled: bool = False
    pi_executable: str = "pi"
    pi_directory: Path | None = None
    pi_provider: str = ""
    pi_model: str = ""
    pi_thinking: Literal["off", "low", "medium"] = "low"
    browser_enabled: bool = True
    browser_headless: bool = True
    browser_resource_origins: str = Field(default="", max_length=2000)
    check_timeout: int = Field(default=120, ge=5, le=120)
    daily_check_limit: int = Field(default=12, ge=1, le=100)
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "tls"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    email_from: str = ""
    email_to: str = ""

    @field_validator("browser_resource_origins")
    @classmethod
    def public_browser_origins(cls, value: str) -> str:
        origins = [origin.strip() for origin in value.split(",") if origin.strip()]
        if len(origins) > 8:
            raise ValueError("At most eight browser resource origins may be approved")
        for origin in origins:
            public_url(origin)
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("Browser resources must be public HTTPS origins")
        return ",".join(origins)

    @field_validator("email_from", "email_to")
    @classmethod
    def mailbox(cls, value: str) -> str:
        if value:
            if not value.isascii() or "\r" in value or "\n" in value or "@" not in value:
                raise ValueError("Use one plain ASCII email address")
            if Address(addr_spec=value).addr_spec != value:
                raise ValueError("Use one email address without a display name")
        return value

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls.model_validate({
            field: os.environ[f"DEAL_TRACKER_{field.upper()}"]
            for field in cls.model_fields
            if os.environ.get(f"DEAL_TRACKER_{field.upper()}")
        })

    def collector_issues(self) -> list[str]:
        issues = []
        if not self.collection_enabled:
            issues.append("Live collection is disabled in server configuration")
        if not self.pi_provider or not self.pi_model:
            issues.append("An approved pi provider and model must be configured")
        if not self.pi_directory or not self.pi_directory.is_absolute():
            issues.append("Set an absolute, dedicated pi configuration directory")
        elif not self.pi_directory.is_dir():
            issues.append("The dedicated pi configuration directory does not exist")
        if not shutil.which(self.pi_executable):
            issues.append("The pi executable is not available")
        if self.browser_enabled and not (Path(__file__).resolve().parent.parent / "node_modules/@playwright/mcp/cli.js").is_file():
            issues.append("Install browser dependencies with npm ci and npm run browser:install")
        return issues

    def email_issues(self) -> list[str]:
        issues = []
        if not self.email_enabled:
            issues.append("Email delivery is disabled; previews remain available")
        if not self.smtp_host or not self.email_from or not self.email_to:
            issues.append("Configure SMTP host, sender and recipient")
        if self.smtp_username and not self.smtp_password.get_secret_value():
            issues.append("SMTP credentials are incomplete")
        return issues