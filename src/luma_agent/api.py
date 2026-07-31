"""HTTP client for the Luma Bistro reservation API.

Every network concern the agent should not have to think about lives here:
retries, timeouts, idempotency keys, and turning HTTP errors into one exception
carrying the API's own error code.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .rules import normalize_phone

logger = logging.getLogger("luma.api")

# The mock API advertises retry_after_ms=500 on its synthetic 503. One retry is
# enough to clear a transient blip without stalling the caller: a second retry
# would push worst-case tool latency past the ~2s where silence feels broken.
MAX_RETRIES = 1
RETRY_BACKOFF_S = 0.5
REQUEST_TIMEOUT_S = 5.0


@dataclass
class APICall:
    """One HTTP attempt. Doubles as the API-latency sample and the retry audit trail."""

    method: str
    path: str
    attempt: int
    outcome: str  # HTTP status code, or the exception name for transport failures
    duration_ms: float


# The API returns an error code on every failure, so the client passes that code
# straight through rather than inventing a parallel class hierarchy for it. One
# exception type, and callers branch on `err.code`.
TEMPORARY = "TEMPORARY_FAILURE"
SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"
INVALID_SLOT = "INVALID_SLOT"
NOT_FOUND = "NOT_FOUND"
ALREADY_CANCELLED = "ALREADY_CANCELLED"


class LumaAPIError(Exception):
    def __init__(self, code: str, detail: Any = None) -> None:
        super().__init__(f"{code} {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def alternatives(self) -> list[dict[str, Any]]:
        """Other open times, when the API supplied them with a 409."""
        return self.detail.get("alternatives", []) if isinstance(self.detail, dict) else []


def reservation_fingerprint(name: str, phone: str, date: str, time: str, party_size: int) -> str:
    """Stable idempotency key derived from the booking's identity.

    Deriving the key from content rather than generating a UUID per call is what
    actually prevents duplicates: if the LLM calls create twice (retry, garbled
    confirmation, duplicate tool call), both calls produce the same key and the
    API returns the original reservation instead of writing a second one.
    """
    digest = hashlib.sha256(
        "|".join(
            [name.strip().lower(), normalize_phone(phone), date, time, str(party_size)]
        ).encode()
    ).hexdigest()
    return f"luma-{digest[:32]}"


class LumaAPI:
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (
            base_url or os.getenv("LUMA_API_BASE_URL", "http://localhost:8000")
        ).rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=REQUEST_TIMEOUT_S)
        self.calls: list[APICall] = []

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Send a request, retrying only on failures that are safe and likely to clear.

        Retrying POST /reservations is safe here because the Idempotency-Key makes
        it idempotent server-side. Without that guarantee this method would have to
        treat writes as non-retryable.

        The retry is deliberately invisible to the LLM. A 500ms blip that resolves
        on its own should not cost the caller a "let me try that again" turn, and
        enforcing the retry budget in code makes "at most one retry" a guarantee
        rather than something the model might or might not honor.
        """
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            started = time.perf_counter()
            try:
                response = await self._client.request(method, path, **kwargs)
                self._record(method, path, attempt, str(response.status_code), started)
                if response.status_code >= 500 or response.status_code == 429:
                    last_error = LumaAPIError(TEMPORARY, response.status_code)
                    logger.warning(
                        "api.transient",
                        extra={"path": path, "status": response.status_code, "attempt": attempt},
                    )
                else:
                    self._raise_for_client_error(response)
                    return response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._record(method, path, attempt, type(exc).__name__, started)
                last_error = LumaAPIError(TEMPORARY, type(exc).__name__)
                logger.warning("api.network", extra={"path": path, "error": str(exc)})

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_S)

        logger.error("api.exhausted", extra={"path": path, "attempts": MAX_RETRIES + 1})
        raise last_error  # type: ignore[misc]

    def _record(self, method: str, path: str, attempt: int, outcome: str, started: float) -> None:
        call = APICall(
            method=method,
            path=path,
            attempt=attempt,
            outcome=outcome,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        self.calls.append(call)
        logger.info(
            "api.call",
            extra={
                "method": method,
                "path": path,
                "attempt": attempt,
                "outcome": outcome,
                "duration_ms": call.duration_ms,
            },
        )

    @staticmethod
    def _raise_for_client_error(response: httpx.Response) -> None:
        """Turn a 4xx into the API's own error code. 422s have no code, so they
        become VALIDATION_ERROR, which always means we sent bad arguments."""
        if response.status_code < 400:
            return
        detail = _error_detail(response)
        code = detail.get("code") if isinstance(detail, dict) else None
        raise LumaAPIError(code or "VALIDATION_ERROR", detail)

    # --- Endpoints ----------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def restaurant(self) -> dict[str, Any]:
        return await self._request("GET", "/restaurant")

    async def check_availability(self, date: str, time: str, party_size: int) -> dict[str, Any]:
        return await self._request(
            "GET", "/availability", params={"date": date, "time": time, "party_size": party_size}
        )

    async def create_reservation(
        self, name: str, phone: str, date: str, time: str, party_size: int, notes: str | None
    ) -> dict[str, Any]:
        key = reservation_fingerprint(name, phone, date, time, party_size)
        return await self._request(
            "POST",
            "/reservations",
            json={
                "name": name,
                "phone": phone,
                "date": date,
                "time": time,
                "party_size": party_size,
                "notes": notes,
            },
            headers={"Idempotency-Key": key},
        )

    async def search_reservations(
        self, phone: str | None = None, confirmation_code: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if phone:
            params["phone"] = normalize_phone(phone)
        if confirmation_code:
            params["confirmation_code"] = confirmation_code.upper()
        result = await self._request("GET", "/reservations/search", params=params)
        return result.get("results", [])

    async def update_reservation(self, reservation_id: str, **changes: Any) -> dict[str, Any]:
        payload = {k: v for k, v in changes.items() if v is not None}
        return await self._request("PATCH", f"/reservations/{reservation_id}", json=payload)

    async def cancel_reservation(self, reservation_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/reservations/{reservation_id}/cancel")

    async def create_handoff(
        self, reason: str, conversation_summary: str, customer_phone: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/handoff",
            json={
                "reason": reason,
                "conversation_summary": conversation_summary,
                "customer_phone": customer_phone,
            },
        )

    async def reset(self) -> dict[str, Any]:
        """Test-only: restores seed data. Used by the eval harness between scenarios."""
        return await self._request("POST", "/admin/reset")


def _error_detail(response: httpx.Response) -> Any:
    try:
        return response.json().get("detail", response.text)
    except ValueError:
        return response.text
