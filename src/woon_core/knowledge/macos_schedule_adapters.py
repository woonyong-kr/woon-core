"""macOS-only adapters for the approval-gated schedule bridge.

The adapters are deliberately narrow: Things is reached through its documented
URL scheme and Apple Calendar only through EventKit.  They never access
Things' private database, never use an arbitrary calendar, and never persist a
token.  The caller must still pass an explicitly approved candidate.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlparse

from woon_core.errors import WoonError
from woon_core.knowledge.schedule_bridge import ScheduleCandidate

_CALENDAR_NAME: Final = "Woon Tasks"
_TITLE_SUFFIX: Final = {
    "career": "커리어",
    "learning": "학습",
    "creative": "창작",
    "life": "생활",
    "relationship": "관계",
    "health": "건강",
    "admin": "행정",
}
_SWIFT_SCRIPT: Final = Path(__file__).resolve().parents[3] / "scripts/woon-calendar-bridge.swift"
_THINGS_SWIFT_SCRIPT: Final = (
    Path(__file__).resolve().parents[3] / "scripts/woon-things-url-bridge.swift"
)
_AREA_TITLES: Final = {
    "career": "커리어·일",
    "learning": "학습·지식",
    "creative": "창작·발행",
    "life": "생활·집",
    "relationship": "관계·사람",
    "health": "건강·성장",
    "admin": "행정·재정",
}
NativeRunner = Callable[[dict[str, str | None]], Mapping[str, str]]
ThingsRunner = Callable[[dict[str, object]], Mapping[str, str]]


class MacOSCalendarPort:
    """EventKit port confined to the one Woon-owned calendar."""

    def __init__(self, runner: NativeRunner | None = None) -> None:
        self._runner = runner or _run_calendar_bridge

    def ensure_permission(self) -> None:
        response = self._runner({"action": "permission", "calendarName": _CALENDAR_NAME})
        if response.get("status") != "granted":
            raise WoonError("calendar bridge did not confirm EventKit permission")

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str:
        if candidate.start_at is None or candidate.end_at is None:
            raise WoonError("calendar bridge requires a date-time candidate")
        event_id = self._runner(
            {
                "action": "create-or-update",
                "calendarName": _CALENDAR_NAME,
                "title": _calendar_title(candidate),
                "startAt": _iso(candidate.start_at),
                "endAt": _iso(candidate.end_at),
                "existingID": existing_id,
            }
        ).get("calendar_event_id")
        if not event_id:
            raise WoonError("calendar bridge did not return an event identifier")
        return event_id

    def cancel(self, event_id: str) -> None:
        response = self._runner(
            {
                "action": "cancel",
                "calendarName": _CALENDAR_NAME,
                "existingID": event_id,
            }
        )
        if response.get("calendar_event_id") != event_id:
            raise WoonError("calendar bridge cancellation receipt mismatch")


class MacOSThingsURLSchemePort:
    """Things port using only the documented URL Scheme and x-success IDs."""

    def __init__(self, runner: ThingsRunner | None = None) -> None:
        self._runner = runner or _run_things_bridge

    def ensure_authorization(self) -> None:
        response = self._runner({"action": "permission"})
        if response.get("status") != "keychain-ready":
            raise WoonError("Things URL Scheme token is unavailable")

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str:
        action = "update" if existing_id else "add"
        response = self._runner(
            {
                "action": action,
                "title": candidate.intent.strip(),
                "when": _things_when(candidate.start_at),
                "tags": list(candidate.things_tags),
                "list": _area_title(candidate.area_id),
                "notes": "Woon Second Brain이 생성한 일정입니다.",
                "existingID": existing_id,
            }
        )
        things_id = response.get("things_id")
        if not things_id:
            raise WoonError("Things URL Scheme did not return a to-do identifier")
        return things_id

    def cancel(self, things_id: str) -> None:
        response = self._runner({"action": "update", "existingID": things_id, "canceled": True})
        if response.get("things_id") != things_id:
            raise WoonError("Things URL Scheme cancellation receipt mismatch")


def _calendar_title(candidate: ScheduleCandidate) -> str:
    try:
        suffix = _TITLE_SUFFIX[candidate.area_id]
    except KeyError as error:
        raise WoonError("calendar bridge requires a configured category") from error
    return f"{candidate.intent.strip()} · {suffix}"


def _area_title(area_id: str) -> str:
    try:
        return _AREA_TITLES[area_id]
    except KeyError as error:
        raise WoonError("Things URL Scheme requires a configured area") from error


def _things_when(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise WoonError("Things URL Scheme requires timezone-aware start times")
    return value.isoformat()


def _run_calendar_bridge(payload: dict[str, str | None]) -> Mapping[str, str]:
    if not _SWIFT_SCRIPT.is_file():
        raise WoonError("Woon CalendarBridge script is missing")
    result = subprocess.run(
        ["/usr/bin/swift", str(_SWIFT_SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WoonError(f"calendar bridge failed: {detail or 'unknown native error'}")
    try:
        decoded = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WoonError("calendar bridge returned invalid JSON") from error
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise WoonError("calendar bridge returned an invalid payload")
    return decoded


def _run_things_bridge(payload: dict[str, object]) -> Mapping[str, str]:
    if not _THINGS_SWIFT_SCRIPT.is_file():
        raise WoonError("Woon Things URL bridge script is missing")
    if payload.get("action") == "permission":
        return _run_things_native(payload)
    with _ThingsCallback() as callback:
        response = _run_things_native({**payload, "callbackURL": callback.url})
        if response.get("status") != "dispatched":
            raise WoonError("Things URL Scheme did not dispatch the command")
        return callback.wait()


def _run_things_native(payload: dict[str, object]) -> Mapping[str, str]:
    result = subprocess.run(
        ["/usr/bin/swift", str(_THINGS_SWIFT_SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WoonError(f"Things URL bridge failed: {detail or 'unknown native error'}")
    try:
        decoded = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WoonError("Things URL bridge returned invalid JSON") from error
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise WoonError("Things URL bridge returned an invalid payload")
    return decoded


class _ThingsCallback:
    """One-shot loopback receiver for a single Things x-callback-url response."""

    def __init__(self) -> None:
        self._nonce = secrets.token_urlsafe(32)
        self._event = threading.Event()
        self._response: dict[str, str] | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != f"/{owner._nonce}":
                    self.send_error(404)
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                status = _single_query(query, "status")
                things_id = _single_query(query, "x-things-id")
                owner._response = {"status": status, "things_id": things_id}
                owner._event.set()
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/{self._nonce}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _ThingsCallback:
        self._thread.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def wait(self) -> Mapping[str, str]:
        if not self._event.wait(timeout=20):
            raise WoonError("Things URL Scheme did not send an x-success callback")
        response = self._response
        if response is None or response.get("status") != "success" or not response.get("things_id"):
            raise WoonError("Things URL Scheme reported an unsuccessful command")
        return response


def _single_query(query: Mapping[str, list[str]], field: str) -> str:
    values = query.get(field)
    if not values or len(values) != 1 or not values[0]:
        raise WoonError("Things URL Scheme callback is malformed")
    return values[0]


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WoonError("calendar bridge requires timezone-aware event times")
    return value.isoformat()
