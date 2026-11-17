"""Igris edge daemon.

A small process for a desktop or laptop that periodically reports coarse
activity information to the Igris hub.  Run it with ``python -m igris.edge``.

Privacy rules honoured by this module:

* The foreground window title is read only to choose a category and is never
  stored, logged or sent over the network.
* Keystrokes are never read.  Idle time comes exclusively from the Windows
  ``GetLastInputInfo`` API.

Only the standard library is used.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("igris.edge")

# Idle time at or above this many seconds is considered "away".
IDLE_THRESHOLD_SECONDS = 600.0

# A heartbeat report is sent at least this often, even without changes. This plus
# one probe interval must stay below the hub's stale_after (300 s), or a quiet
# device would briefly look absent between two heartbeats.
HEARTBEAT_SECONDS = 240.0

# Case-insensitive substring rules used by categorize().
DEFAULT_RULES = {
    "coding": ["visual studio code", "vs code", "pycharm"],
    "studying": ["pdf", "obsidian", "acrobat", "calibre"],
    "media": ["youtube", "netflix", "spotify", "vlc"],
    "browsing": ["chrome", "firefox", "edge", "safari", "opera"],
}

# Categories are checked in this order, so "media" wins over "browsing"
# when a title matches both (e.g. "YouTube - Google Chrome").
CATEGORY_PRIORITY = ("coding", "studying", "media", "browsing")

# --------------------------------------------------------------------------
# Platform probes
# --------------------------------------------------------------------------

def _elapsed_seconds(tick_now: int, tick_then: int) -> float:
    """Seconds between two 32-bit tick counts.

    GetTickCount wraps after 49.7 days, and ctypes returns it as a signed int,
    so it turns negative after 24.8 days of uptime.
    """
    return ((tick_now - tick_then) & 0xFFFFFFFF) / 1000.0


if os.name == "nt":  # pragma: no cover - exercised only on real Windows
    import ctypes

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    def foreground_title() -> str:
        """Return the foreground window title on Windows, '' elsewhere.

        The title is used only to pick a category; callers must never
        store, log or transmit it.
        """
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return ""
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value
        except Exception:
            return ""

    def idle_seconds() -> float:
        """Return seconds since the last user input on Windows, 0.0 elsewhere.

        Uses GetLastInputInfo; keystrokes themselves are never read.
        """
        try:
            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            return _elapsed_seconds(ctypes.windll.kernel32.GetTickCount(), info.dwTime)
        except Exception:
            return 0.0

else:  # non-Windows platforms: inert probes

    def foreground_title() -> str:
        """Foreground title is only available on Windows; return ''."""
        return ""

    def idle_seconds() -> float:
        """Idle time is only available on Windows; return 0.0."""
        return 0.0


# --------------------------------------------------------------------------
# Categorisation and reporting
# --------------------------------------------------------------------------

def categorize(title: str, rules: dict = DEFAULT_RULES) -> str:
    """Map a window *title* to one of the known activity categories.

    Matching is a case-insensitive substring test.  Categories are tried in
    CATEGORY_PRIORITY order, so 'media' wins over 'browsing'.  Returns
    'other' when nothing matches.
    """
    lowered = (title or "").lower()
    for category in CATEGORY_PRIORITY:
        for needle in rules.get(category, ()):
            if needle.lower() in lowered:
                return category
    return "other"


def report(device: str, title_probe, idle_probe, clock) -> dict:
    """Build one activity payload.

    The window title is consumed immediately by categorize() and is never
    placed in the returned payload.
    """
    category = categorize(title_probe())
    return {
        "device": device,
        "category": category,
        "idle_seconds": float(idle_probe()),
        "at": float(clock()),
    }


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

def send(url: str, token: str, payload: dict, opener=None) -> bool:
    """POST *payload* as JSON to *url* with a Bearer authorization header.

    Returns True on success and False on any failure; this function never
    raises.  The token is never written to the log.
    """
    if opener is None:
        opener = urllib.request.build_opener()
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        response = opener.open(request, timeout=10)
        close = getattr(response, "close", None)
        if close is not None:
            close()  # the daemon runs for days; do not keep sockets open
        return True
    except Exception:
        # Deliberately do not include the exception text: it could echo
        # request headers and therefore leak the token.
        logger.warning("failed to deliver activity report to %s", url)
        return False


def run(
    device: str,
    url: str,
    token: str,
    interval: float = 30,
    stop: threading.Event | None = None,
    title_probe=foreground_title,
    idle_probe=idle_seconds,
    clock=time.time,
    opener=None,
) -> None:
    """Loop until *stop* is set, sending reports when they matter.

    A report is sent only when the category changes, when the idle state
    crosses IDLE_THRESHOLD_SECONDS, or at least every HEARTBEAT_SECONDS
    as a heartbeat.
    """
    if stop is None:
        stop = threading.Event()
    last_category = None
    last_idle_over = None
    last_sent_at = None
    while not stop.is_set():
        payload = report(device, title_probe, idle_probe, clock)
        idle_over = payload["idle_seconds"] >= IDLE_THRESHOLD_SECONDS
        category_changed = payload["category"] != last_category
        idle_crossed = idle_over != last_idle_over
        heartbeat_due = last_sent_at is None or (
            payload["at"] - last_sent_at
        ) >= HEARTBEAT_SECONDS
        if category_changed or idle_crossed or heartbeat_due:
            if send(url, token, payload, opener):
                # Only advance the remembered state after a successful
                # delivery so that a failed send is retried.
                last_category = payload["category"]
                last_idle_over = idle_over
                last_sent_at = payload["at"]
        stop.wait(interval)


# --------------------------------------------------------------------------
# Command line entry point
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m igris.edge",
        description="Report desktop activity to the Igris hub.",
    )
    parser.add_argument("--url", required=True, help="hub ingestion endpoint")
    parser.add_argument("--device", required=True, help="device name")
    parser.add_argument(
        "--interval", type=float, default=30,
        help="seconds between probe iterations (default: 30)",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("IGRIS_EDGE_TOKEN")
    if not token:
        # The token is deliberately never accepted on the command line.
        print(
            "IGRIS_EDGE_TOKEN is not set; the edge daemon refuses to start.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    logging.basicConfig(level=logging.INFO)
    run(args.device, args.url, token, interval=args.interval)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
