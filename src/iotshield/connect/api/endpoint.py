"""An HTTP API data source as configured: where, how to authenticate, the window, the mapping.

``ApiEndpoint`` is everything about the source that is **not secret** — it is stored as JSON
beside the data source and returned to the browser as-is. The secrets (a password, an API key)
are ``ApiCredentials``, sealed separately (``storage/secrets.py``) and never returned.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from iotshield.connect.api.mapping import ApiMapping

AuthKind = Literal["none", "basic", "api_key"]
WindowFormat = Literal["date", "iso", "epoch_s", "epoch_ms"]


class ApiEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=2000, description="https:// URL of the records")
    auth: AuthKind = "none"
    username: str | None = Field(
        default=None, max_length=200, description="Basic auth: the user name (not secret)"
    )
    api_key_header: str = Field(
        default="Authorization", max_length=100, description="API key: the header it is sent in"
    )
    api_key_prefix: str = Field(
        default="Bearer ", max_length=50, description="API key: text before the key, e.g. 'Bearer '"
    )
    start_param: str = Field(
        min_length=1, max_length=100, description="Query parameter the window's start goes in"
    )
    end_param: str = Field(
        min_length=1, max_length=100, description="Query parameter the window's end goes in"
    )
    window_format: WindowFormat = Field(
        default="iso",
        description=(
            "date: YYYY-MM-DD, end = the last day (inclusive); iso / epoch_s / epoch_ms: UTC "
            "instants, end = midnight after the last day (exclusive)"
        ),
    )
    next_path: str | None = Field(
        default=None,
        max_length=200,
        description="Dotted path to the next page's URL in each response; blank = one page",
    )
    mapping: ApiMapping

    @field_validator("url", "start_param", "end_param")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("username", "next_path")
    @classmethod
    def _blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("api_key_header")
    @classmethod
    def _header(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or any(ch in stripped for ch in " \t\r\n:"):
            raise ValueError("must be a header name, e.g. Authorization or X-API-Key")
        return stripped

    @field_validator("api_key_prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("must be on one line")
        return value

    @model_validator(mode="after")
    def _auth(self) -> "ApiEndpoint":
        if self.auth == "basic" and self.username is None:
            raise ValueError("basic authentication needs a user name")
        if self.start_param == self.end_param:
            raise ValueError("the start and end parameters must differ")
        return self

    def window_params(self, start: date, end: date) -> dict[str, str]:
        """The query parameters for a pull of ``start``..``end`` (both inclusive days, UTC)."""
        if self.window_format == "date":
            return {self.start_param: start.isoformat(), self.end_param: end.isoformat()}
        first = datetime.combine(start, time.min, tzinfo=UTC)
        after = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        if self.window_format == "iso":
            return {
                self.start_param: first.isoformat().replace("+00:00", "Z"),
                self.end_param: after.isoformat().replace("+00:00", "Z"),
            }
        scale = 1000 if self.window_format == "epoch_ms" else 1
        return {
            self.start_param: str(int(first.timestamp()) * scale),
            self.end_param: str(int(after.timestamp()) * scale),
        }


@dataclass(frozen=True)
class ApiCredentials:
    """The secret half: what basic auth's password or an API key is. Never serialized out."""

    password: str | None = None
    api_key: str | None = None

    def as_sealable(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.password is not None:
            values["password"] = self.password
        if self.api_key is not None:
            values["api_key"] = self.api_key
        return values

    @classmethod
    def from_sealed(cls, values: dict[str, str]) -> "ApiCredentials":
        return cls(password=values.get("password"), api_key=values.get("api_key"))

    def complete_for(self, endpoint: ApiEndpoint) -> bool:
        """Whether these are the credentials ``endpoint``'s authentication needs."""
        if endpoint.auth == "basic":
            return self.password is not None
        if endpoint.auth == "api_key":
            return self.api_key is not None
        return True
