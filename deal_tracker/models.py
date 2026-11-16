from datetime import UTC, date, datetime, timedelta
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

Mode = Literal["exact", "sale", "discovery"]


def public_url(value: str) -> str:
    if not value:
        return value
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"https", "http"} or not hostname:
        raise ValueError("Use an http or https URL")
    if parsed.username or parsed.password:
        raise ValueError("URLs must not contain credentials")
    if "." not in hostname or hostname.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Use a public shop URL, not a local address")
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private network addresses are not allowed")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Use a standard web port")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class CheckSchedule(StrictModel):
    repeat: Literal["daily", "weekly"] = "weekly"
    interval: int = Field(default=1, ge=1, le=52)
    weekdays: list[Literal[0, 1, 2, 3, 4, 5, 6]] = Field(default_factory=list, max_length=7)
    start_date: date = Field(ge=date(2000, 1, 1), le=date(2100, 12, 31))
    timezone: str = Field(default="UTC", max_length=100)
    sale_periods: list[Literal["black_friday", "christmas", "new_year", "winter_sales"]] = Field(default_factory=list, max_length=4)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Choose a valid IANA time zone") from None
        return value

    @model_validator(mode="after")
    def valid_weekdays(self) -> "CheckSchedule":
        if self.repeat == "weekly" and not self.weekdays:
            raise ValueError("Select at least one weekday")
        self.weekdays = sorted(set(self.weekdays))
        self.sale_periods = list(dict.fromkeys(self.sale_periods))
        return self


class WatchSpec(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    mode: Mode = "exact"
    description: str = Field(default="", max_length=5000)
    shop_url: str = Field(default="", max_length=2000)
    product_url: str = Field(default="", max_length=2000)
    sku: str = Field(default="", max_length=100)
    audience: str = Field(default="", max_length=100)
    size: str = Field(default="", max_length=100)
    color: str = Field(default="", max_length=100)
    country: str = Field(default="", max_length=80)
    currency: str = Field(default="", pattern=r"^$|^[A-Z]{3}$")
    target_price: float | None = Field(default=None, gt=0, le=1_000_000)
    criteria: str = Field(default="", max_length=3000)
    interval_hours: int | None = Field(default=None, ge=1, le=720)
    schedule: CheckSchedule | None = None
    notify_review: bool = False

    _urls = field_validator("shop_url", "product_url")(public_url)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


    @model_validator(mode="after")
    def one_schedule(self) -> "WatchSpec":
        if self.schedule is not None and self.interval_hours is not None:
            raise ValueError("Use a calendar schedule or a legacy interval, not both")
        return self


class WatchUpdate(StrictModel):
    version: int = Field(ge=1)
    spec: WatchSpec


class DeriveRequest(WatchUpdate):
    candidate_id: str | None = Field(default=None, max_length=64)


class StatusUpdate(StrictModel):
    version: int = Field(ge=1)
    status: Literal["active", "paused", "archived"]


class CheckRequest(StrictModel):
    version: int = Field(ge=1)


class DeliveryResolution(StrictModel):
    action: Literal["retry", "mark_sent", "dismiss"]
    acknowledge_duplicate: bool = False


class Observation(StrictModel):
    kind: Literal["product", "sale"] = "product"
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    observed_at: AwareDatetime
    evidence: str = Field(min_length=1, max_length=4000, description="One or more verbatim source excerpts, separated by a blank line. Each must occur in the captured page text or structured_data.")
    reason: str = Field(default="", max_length=2000)
    sku: str = Field(default="", max_length=100)
    audience: str = Field(default="", max_length=100)
    size: str = Field(default="", max_length=100)
    color: str = Field(default="", max_length=100)
    country: str = Field(default="", max_length=80)
    price: float | None = Field(default=None, ge=0, le=1_000_000)
    shipping: float | None = Field(default=None, ge=0, le=1_000_000)
    currency: str = Field(default="", pattern=r"^$|^[A-Z]{3}$")
    availability: Literal["in_stock", "out_of_stock", "unknown"] = "unknown"
    sale_scope: Literal["shop", "category", "product", "unknown"] = "unknown"
    valid_until: AwareDatetime | None = None

    _url = field_validator("url")(public_url)

    @field_validator("observed_at")
    @classmethod
    def no_future_observations(cls, value: datetime) -> datetime:
        if value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("Observation time cannot be in the future")
        return value


class ShopResearch(StrictModel):
    outcome: Literal["found", "not_found", "blocked"]
    summary: str = Field(min_length=1, max_length=1200)
    url: str = Field(default="", max_length=2000)
    observed_at: AwareDatetime | None = None
    evidence: str = Field(default="", max_length=2000, description="Verbatim source excerpts separated by a blank line; no added labels or interpretation.")

    _url = field_validator("url")(public_url)

    @model_validator(mode="after")
    def found_needs_source(self) -> "ShopResearch":
        if self.outcome == "found" and not (self.url and self.observed_at and self.evidence):
            raise ValueError("Shop sale notes need a dated source and quoted evidence")
        return self


class CollectionResult(StrictModel):
    _usage: dict | None = PrivateAttr(default=None)
    schema_version: Literal[1] = 1
    watch_version: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    outcome: Literal["success", "no_change", "partial", "blocked", "failed", "timed_out"]
    diagnostics: str = Field(default="", max_length=3000)
    observations: list[Observation] = Field(default_factory=list, max_length=30)
    shop_research: ShopResearch | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> "CollectionResult":
        if self.outcome in {"blocked", "failed", "timed_out"} and self.observations:
            raise ValueError("Failed collection cannot contain successful observations")
        if self.outcome == "success" and not self.observations:
            raise ValueError("A successful collection needs evidence; otherwise use no_change")
        return self


class Feedback(StrictModel):
    feedback: Literal["unreviewed", "useful", "not_useful"]
    reason: str = Field(default="", max_length=1000)


def expansion_spec(parent: dict, mode: Mode = "discovery") -> WatchSpec:
    return WatchSpec(
        title=f"{parent['title'][:140]} - related",
        mode=mode,
        description=parent["description"],
        shop_url=parent["shop_url"],
        audience=parent["audience"],
        country=parent["country"],
        currency=parent["currency"],
    )