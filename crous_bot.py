#!/usr/bin/env python3
"""
CROUS housing monitor -> Telegram alert bot.

Runs as a stateless one-shot job (designed for GitHub Actions cron):

    load state -> detect tool id -> query CROUS -> diff -> alert -> save state -> exit

Scope: ALERTING ONLY. This bot never books, never logs in, never submits a
form. It only reads the public search API and notifies you on Telegram.

Author-facing note: the CROUS search API is undocumented and may change its
field names between campaigns. Field extraction below is defensive (safe
`.get()` access with fallbacks) and the first run logs the raw item keys so the
mapping can be adjusted quickly if CROUS renames something.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

# Optional: load a local .env file when running OUTSIDE GitHub Actions.
# In production the values come from GitHub Secrets (environment variables).
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #
# Every tunable value lives here. A non-technical user can change these without
# reading the logic below. Anything can also be overridden with an environment
# variable of the same name (useful for GitHub Actions).

# --- CROUS endpoints ---------------------------------------------------------
CROUS_BASE_URL = "https://trouverunlogement.lescrous.fr"
CROUS_HOMEPAGE_URL = CROUS_BASE_URL + "/"
# Search API. {tool_id} is the campaign identifier (it changes every year).
CROUS_SEARCH_URL_TEMPLATE = CROUS_BASE_URL + "/api/fr/search/{tool_id}"
# Public detail / booking page for a given accommodation id.
CROUS_ACCOMMODATION_URL_TEMPLATE = (
    CROUS_BASE_URL + "/tools/{tool_id}/accommodations/{acc_id}"
)

# --- Tool id (campaign) resolution ------------------------------------------
# The tool id is the campaign number in the URL: /tools/<ID>/search.
# For Ile-de-France 2026/2027 it is 47 (verified from the official search URL).
#
# Resolution priority:
#   1. CROUS_TOOL_ID           -> if set, used directly (pinned).
#   2. homepage auto-detection -> only if CROUS_AUTODETECT_TOOL_ID is enabled.
#   3. FALLBACK_TOOL_ID        -> safe default (47).
#
# Auto-detection is OFF by default: the homepage exposes several tool ids and
# can return the wrong one (it returned 42 in testing), so relying on the known
# pinned id is more reliable.
CROUS_TOOL_ID = os.getenv("CROUS_TOOL_ID", "").strip()
CROUS_AUTODETECT_TOOL_ID = os.getenv("CROUS_AUTODETECT_TOOL_ID", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
FALLBACK_TOOL_ID = int(os.getenv("CROUS_FALLBACK_TOOL_ID", "47"))

# --- Geographic bounding box (Ile-de-France, includes Paris) -----------------
# Two opposite corners of the search rectangle.
BBOX_WEST = float(os.getenv("CROUS_BBOX_WEST", "1.4462445"))     # min longitude
BBOX_SOUTH = float(os.getenv("CROUS_BBOX_SOUTH", "48.1201456"))  # min latitude
BBOX_EAST = float(os.getenv("CROUS_BBOX_EAST", "3.5592208"))     # max longitude
BBOX_NORTH = float(os.getenv("CROUS_BBOX_NORTH", "49.241431"))   # max latitude

# --- Search paging -----------------------------------------------------------
PAGE_SIZE = int(os.getenv("CROUS_PAGE_SIZE", "50"))
MAX_PAGES = int(os.getenv("CROUS_MAX_PAGES", "40"))  # hard safety cap

# --- HTTP behaviour (CROUS requests) -----------------------------------------
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30"))
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
HTTP_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "4"))
HTTP_BACKOFF_BASE = float(os.getenv("HTTP_BACKOFF_BASE", "2.0"))  # seconds
USER_AGENT = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

# --- Telegram ----------------------------------------------------------------
TELEGRAM_API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MAX_RETRIES = int(os.getenv("TELEGRAM_MAX_RETRIES", "5"))
TELEGRAM_BACKOFF_BASE = float(os.getenv("TELEGRAM_BACKOFF_BASE", "2.0"))

# --- Monitoring behaviour ----------------------------------------------------
FAILURE_THRESHOLD = int(os.getenv("FAILURE_THRESHOLD", "3"))
HEARTBEAT_INTERVAL_HOURS = int(os.getenv("HEARTBEAT_INTERVAL_HOURS", "24"))

# --- State file (committed back to the repo by the workflow) -----------------
STATE_FILE = os.getenv("STATE_FILE", "state.json")
STATE_VERSION = 1

# --- Secrets (NEVER hardcode; provided via environment / GitHub Secrets) -----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# --------------------------------------------------------------------------- #
# LOGGING (stdout -> visible in GitHub Actions logs)
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("crous-bot")


# --------------------------------------------------------------------------- #
# SMALL HELPERS
# --------------------------------------------------------------------------- #
def html_escape(text):
    """Escape the characters Telegram HTML parse_mode cares about."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _first_present(node, keys, default=None):
    """Return the first non-empty value among `keys` in a dict."""
    if not isinstance(node, dict):
        return default
    for key in keys:
        value = node.get(key)
        if value not in (None, ""):
            return value
    return default


def _label_or_value(node):
    """CROUS returns many fields as {'label': '...', 'value': N, ...}."""
    if node is None:
        return None
    if isinstance(node, dict):
        return _first_present(node, ["label", "value", "name"])
    return node


def _search_price(node, depth=0):
    """Best-effort search for a rent/price label inside a nested structure.

    The CROUS item does not expose rent at the top level; it lives inside a
    booking-related subtree. We scan only those subtrees to avoid picking up an
    unrelated number (surface, bed count...).
    """
    if node is None or depth > 3:
        return None
    if isinstance(node, dict):
        for key in ("rent", "price", "amount", "monthlyPrice", "totalPrice", "cost"):
            if key in node:
                value = node[key]
                label = _label_or_value(value) if isinstance(value, dict) else value
                if label not in (None, ""):
                    return label
        label = node.get("label")
        if isinstance(label, str) and "\u20ac" in label:  # contains a euro sign
            return label
        for value in node.values():
            found = _search_price(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _search_price(value, depth + 1)
            if found is not None:
                return found
    return None


# --------------------------------------------------------------------------- #
# HTTP SESSION + RETRYING REQUEST
# --------------------------------------------------------------------------- #
def build_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        }
    )
    return session


def http_request(session, method, url, **kwargs):
    """HTTP request with retries + exponential backoff. Raises on final failure."""
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    last_exc = None
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code >= 500:
                raise requests.HTTPError("server error %s" % resp.status_code)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == HTTP_MAX_RETRIES:
                break
            delay = HTTP_BACKOFF_BASE ** attempt
            log.warning(
                "HTTP %s failed (attempt %d/%d): %s -> retry in %.1fs",
                method,
                attempt,
                HTTP_MAX_RETRIES,
                exc,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(
        "HTTP request to %s failed after %d attempts: %s"
        % (url, HTTP_MAX_RETRIES, last_exc)
    )


# --------------------------------------------------------------------------- #
# TOOL ID DETECTION
# --------------------------------------------------------------------------- #
def detect_tool_id_from_homepage(session):
    """Scrape the homepage for a campaign tool id. Returns int or None."""
    try:
        resp = http_request(session, "GET", CROUS_HOMEPAGE_URL)
        html = resp.text
        candidates = []
        candidates += re.findall(r"tools/(\d+)/search", html)
        candidates += re.findall(r'"idTool"\s*:\s*(\d+)', html)
        candidates += re.findall(r"tools/(\d+)/", html)
        if candidates:
            tool_id = int(candidates[0])
            log.info("Auto-detected tool id from homepage: %d", tool_id)
            return tool_id
        log.warning("Could not detect tool id from homepage markup.")
    except Exception as exc:
        log.warning("Tool id detection failed: %s", exc)
    return None


def resolve_tool_id(session):
    """Resolve the campaign tool id using the configured priority."""
    if CROUS_TOOL_ID.isdigit():
        tool_id = int(CROUS_TOOL_ID)
        log.info("Using pinned CROUS_TOOL_ID: %d", tool_id)
        return tool_id
    if CROUS_AUTODETECT_TOOL_ID:
        detected = detect_tool_id_from_homepage(session)
        if detected is not None:
            return detected
        log.warning("Auto-detection failed; falling back.")
    log.info("Using tool id: %d", FALLBACK_TOOL_ID)
    return FALLBACK_TOOL_ID


# --------------------------------------------------------------------------- #
# LISTING PARSING
# --------------------------------------------------------------------------- #
def parse_units(item):
    """Best-effort available-unit count, used for restock detection.

    Defaults to 1 when the API does not expose an explicit count.
    """
    for key in (
        "available",
        "nbAvailable",
        "availableCount",
        "stock",
        "quantity",
        "count",
        "occupancy",
    ):
        val = item.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            return max(val, 0)
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return 1


def parse_listing(item, tool_id):
    """Convert a raw API item into a normalized listing dict, or None."""
    if not isinstance(item, dict):
        return None

    acc_id = item.get("id") or item.get("uid") or item.get("code")
    if acc_id is None:
        return None
    acc_id = str(acc_id)

    residence = item.get("residence")
    if not isinstance(residence, dict):
        residence = {}

    label = _label_or_value(item.get("label")) or item.get("name") or "Logement CROUS"
    residence_label = (
        _label_or_value(residence.get("label"))
        or _label_or_value(residence)
        or "Residence CROUS"
    )
    address = (
        _first_present(residence, ["address", "adresse"])
        or _first_present(item, ["address", "adresse"])
        or "Adresse non communiquee"
    )
    area = _label_or_value(item.get("area")) or _label_or_value(item.get("surface"))
    rent = (
        _label_or_value(item.get("rent"))
        or _label_or_value(item.get("price"))
        or _search_price(item.get("bookingData"))
        or _search_price(item.get("occupationModes"))
    )

    url = item.get("url")
    if not url:
        url = CROUS_ACCOMMODATION_URL_TEMPLATE.format(tool_id=tool_id, acc_id=acc_id)
    elif isinstance(url, str) and url.startswith("/"):
        url = CROUS_BASE_URL + url

    return {
        "id": acc_id,
        "label": str(label),
        "residence": str(residence_label),
        "address": str(address),
        "area": None if area is None else str(area),
        "rent": None if rent is None else str(rent),
        "url": str(url),
        "available": bool(item.get("available", True)),
        "units": parse_units(item),
    }


def fetch_all_listings(session, tool_id):
    """Query the CROUS search API across all pages inside the bounding box."""
    url = CROUS_SEARCH_URL_TEMPLATE.format(tool_id=tool_id)
    listings = {}
    logged_sample = False

    for page in range(1, MAX_PAGES + 1):
        # This payload mirrors exactly what the CROUS website sends for its
        # Ile-de-France search (captured from the browser Network tab), so the
        # API returns the same results the site shows.
        payload = {
            "idTool": tool_id,
            "need_aggregation": True,
            "page": page,
            "pageSize": PAGE_SIZE,
            "sector": None,
            "occupationModes": [],
            "location": [
                {"lon": BBOX_WEST, "lat": BBOX_NORTH},
                {"lon": BBOX_EAST, "lat": BBOX_SOUTH},
            ],
            "residence": None,
            "precision": 4,
            "equipment": [],
            "adaptedPmr": False,
            "area": {"min": 0},
            "price": {"max": 10000000},
            "toolMechanism": "residual",
        }
        resp = http_request(
            session,
            "POST",
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError("CROUS API returned non-JSON response") from exc

        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            results = data if isinstance(data, dict) else {}
        items = results.get("items") or results.get("results") or []
        if not isinstance(items, list):
            items = []

        if not logged_sample and items and isinstance(items[0], dict):
            # Helps adjust field mapping if CROUS renames something.
            log.info("Sample raw item keys: %s", sorted(items[0].keys()))
            logged_sample = True

        for raw in items:
            parsed = parse_listing(raw, tool_id)
            if parsed:
                listings[parsed["id"]] = parsed

        total = results.get("total")
        log.info(
            "Page %d: %d items (running total %d).", page, len(items), len(listings)
        )

        if not items:
            break
        if isinstance(total, int) and len(listings) >= total:
            break
        if len(items) < PAGE_SIZE:
            break

    log.info("Fetched %d listing(s) in bounding box.", len(listings))
    return listings


# --------------------------------------------------------------------------- #
# TELEGRAM
# --------------------------------------------------------------------------- #
def send_telegram(text, disable_preview=True):
    """Send an HTML message with retries, backoff and retry_after handling."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram credentials are missing.")

    api_url = TELEGRAM_API_URL_TEMPLATE.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }

    for attempt in range(1, TELEGRAM_MAX_RETRIES + 1):
        try:
            resp = requests.post(api_url, json=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == TELEGRAM_MAX_RETRIES:
                raise RuntimeError("Telegram network error: %s" % exc) from exc
            delay = TELEGRAM_BACKOFF_BASE ** attempt
            log.warning(
                "Telegram network error (attempt %d/%d): %s -> retry in %.1fs",
                attempt,
                TELEGRAM_MAX_RETRIES,
                exc,
                delay,
            )
            time.sleep(delay)
            continue

        if resp.status_code == 200:
            return True

        if resp.status_code == 429:
            retry_after = TELEGRAM_BACKOFF_BASE ** attempt
            try:
                retry_after = float(resp.json()["parameters"]["retry_after"])
            except Exception:
                pass
            log.warning("Telegram rate limited (429). Waiting %.1fs.", retry_after)
            time.sleep(retry_after + 0.5)
            continue

        if 500 <= resp.status_code < 600:
            delay = TELEGRAM_BACKOFF_BASE ** attempt
            log.warning(
                "Telegram server error %d (attempt %d/%d) -> retry in %.1fs",
                resp.status_code,
                attempt,
                TELEGRAM_MAX_RETRIES,
                delay,
            )
            time.sleep(delay)
            continue

        # Any other 4xx is unrecoverable (bad token, bad chat id, bad HTML...).
        raise RuntimeError(
            "Telegram API error %d: %s" % (resp.status_code, resp.text)
        )

    raise RuntimeError("Telegram send failed after all retries.")


def format_listing_message(listing, restock=False):
    """Build the French, HTML-formatted alert for a single listing."""
    if restock:
        header = "\u267b\ufe0f <b>R\u00e9approvisionnement CROUS</b>"
    else:
        header = "\U0001f6a8 <b>Nouveau logement CROUS</b>"

    lines = [
        header,
        "",
        "\U0001f3f7\ufe0f <b>%s</b>" % html_escape(listing["label"]),
        "\U0001f3e0 %s" % html_escape(listing["residence"]),
        "\U0001f4cd %s" % html_escape(listing["address"]),
    ]
    if listing.get("area"):
        lines.append("\U0001f4d0 %s" % html_escape(listing["area"]))
    if listing.get("rent"):
        lines.append("\U0001f4b6 %s" % html_escape(listing["rent"]))
    if restock and listing.get("units"):
        lines.append("\U0001f4e6 Unit\u00e9s disponibles : %s" % listing["units"])
    lines.append("")
    lines.append(
        '\U0001f449 <a href="%s">R\u00e9server / voir l\'annonce</a>'
        % html_escape(listing["url"])
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# STATE MANAGEMENT
# --------------------------------------------------------------------------- #
def default_state():
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "listings": {},
        "consecutive_failures": 0,
        "failure_alert_sent": False,
        "last_heartbeat": None,
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        log.info("No state file found; starting fresh.")
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        base = default_state()
        for key in base:
            if key in loaded:
                base[key] = loaded[key]
        if not isinstance(base.get("listings"), dict):
            base["listings"] = {}
        return base
    except Exception as exc:
        log.error("State file unreadable (%s). Recreating fresh state.", exc)
        return default_state()


def save_state(state):
    """Write state atomically (temp file + rename) so it is never corrupted."""
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, STATE_FILE)
    log.info(
        "State saved (%d listings, failures=%d).",
        len(state.get("listings", {})),
        state.get("consecutive_failures", 0),
    )


# --------------------------------------------------------------------------- #
# DIFF + HEARTBEAT LOGIC
# --------------------------------------------------------------------------- #
def diff_listings(old, new):
    """Return (new_ids, restocked_ids).

    A listing is *bookable* when its `available` flag is true.
    - new_ids       : ids never seen before AND currently available.
    - restocked_ids : known ids that flipped from unavailable to available,
      or (fallback) whose available-unit count increased.
    All ids (available or not) are still stored by the caller, so a later flip
    from unavailable to available is detected as a restock.
    """
    new_ids = []
    restocked = []
    for acc_id, current in new.items():
        cur_available = current.get("available", True)
        if acc_id not in old:
            if cur_available:
                new_ids.append(acc_id)
            continue
        prev = old[acc_id]
        prev_available = prev.get("available", True)
        if cur_available and not prev_available:
            restocked.append(acc_id)
        elif cur_available and current.get("units", 1) > prev.get("units", 1):
            restocked.append(acc_id)
    return new_ids, restocked


def should_send_heartbeat(state, now):
    last = state.get("last_heartbeat")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    elapsed_hours = (now - last_dt).total_seconds() / 3600.0
    return elapsed_hours >= HEARTBEAT_INTERVAL_HOURS


# --------------------------------------------------------------------------- #
# MONITORING CYCLE
# --------------------------------------------------------------------------- #
def run_monitor_cycle(state, session, now):
    """Perform one full monitoring cycle. Raises on failure."""
    tool_id = resolve_tool_id(session)
    listings = fetch_all_listings(session, tool_id)

    if not state["initialized"]:
        # First run: record everything silently, send ONE confirmation.
        available_count = sum(
            1 for item in listings.values() if item.get("available", True)
        )
        state["listings"] = listings
        state["initialized"] = True
        state["last_heartbeat"] = now.isoformat()
        send_telegram(
            "\u2705 <b>Bot CROUS activ\u00e9</b>\n\n"
            "Surveillance de l'\u00cele-de-France d\u00e9marr\u00e9e.\n"
            "\U0001f4e6 %d logement(s) disponible(s) actuellement "
            "(%d suivi(s) au total).\n\n"
            "Vous recevrez une alerte d\u00e8s qu'un nouveau logement appara\u00eet "
            "ou qu'un logement est r\u00e9approvisionn\u00e9."
            % (available_count, len(listings))
        )
        log.info(
            "First run: recorded %d listings (%d available) silently.",
            len(listings),
            available_count,
        )
        return listings

    new_ids, restocked = diff_listings(state["listings"], listings)
    log.info("Diff: %d new, %d restocked.", len(new_ids), len(restocked))

    for acc_id in new_ids:
        send_telegram(format_listing_message(listings[acc_id], restock=False))
    for acc_id in restocked:
        send_telegram(format_listing_message(listings[acc_id], restock=True))

    # Only replace the persisted listings AFTER alerts were sent successfully.
    state["listings"] = listings
    return listings


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Missing TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID. "
            "Set them as GitHub Actions secrets. Aborting."
        )
        return 1

    now = datetime.now(timezone.utc)
    state = load_state()
    session = build_session()
    was_in_failure = bool(state.get("failure_alert_sent", False))

    try:
        listings = run_monitor_cycle(state, session, now)

        # Recovery notification (only if a failure alert had been sent before).
        if was_in_failure:
            try:
                send_telegram(
                    "\u2705 <b>R\u00e9tablissement</b>\n\n"
                    "Le bot CROUS refonctionne normalement apr\u00e8s une panne."
                )
            except Exception as exc:
                log.warning("Could not send recovery message: %s", exc)

        state["consecutive_failures"] = 0
        state["failure_alert_sent"] = False

        # Daily heartbeat.
        if should_send_heartbeat(state, now):
            available_count = sum(
                1 for item in listings.values() if item.get("available", True)
            )
            try:
                send_telegram(
                    "\U0001f493 <b>Bot CROUS \u2014 statut quotidien</b>\n\n"
                    "\u00c9tat : \u2705 op\u00e9rationnel\n"
                    "\U0001f4e6 Logements disponibles : %d (%d suivis)\n"
                    "\U0001f552 %s"
                    % (
                        available_count,
                        len(listings),
                        now.strftime("%d/%m/%Y %H:%M UTC"),
                    )
                )
                state["last_heartbeat"] = now.isoformat()
            except Exception as exc:
                log.warning("Could not send heartbeat: %s", exc)

        save_state(state)
        log.info("Cycle completed successfully.")
        return 0

    except Exception as exc:
        log.exception("Monitoring cycle failed: %s", exc)
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        log.warning("Consecutive failures: %d", state["consecutive_failures"])

        if (
            state["consecutive_failures"] >= FAILURE_THRESHOLD
            and not state.get("failure_alert_sent", False)
        ):
            try:
                send_telegram(
                    "\u26a0\ufe0f <b>Alerte panne \u2014 Bot CROUS</b>\n\n"
                    "%d \u00e9checs cons\u00e9cutifs.\n"
                    "Le bot n'arrive plus \u00e0 interroger CROUS.\n"
                    "V\u00e9rifiez les logs GitHub Actions."
                    % state["consecutive_failures"]
                )
                state["failure_alert_sent"] = True
            except Exception as send_exc:
                log.error("Could not send failure alert: %s", send_exc)

        # Listings are left untouched -> saved state is never corrupted.
        save_state(state)
        # Exit 0: the failure is handled AND reported via Telegram. Keeping the
        # Actions run green avoids GitHub failure-notification noise; the real
        # signal is the Telegram warning after FAILURE_THRESHOLD failures.
        return 0


if __name__ == "__main__":
    sys.exit(main())
