"""Edge + CDP + Playwright session helpers."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from availity_app import state
from availity_app.constants import AVAILITY_LOGIN_URL, CDP_ENDPOINT
from availity_app.logging_gui import log_to_gui
from availity_app.utils import retry_with_backoff

# ============================================================================
# SECTION 6 — BROWSER MANAGEMENT
# ============================================================================

_CDP_CONNECT_ATTEMPTS = 6
_CDP_PORT_WAIT_S = 45
_CDP_HTTP_TIMEOUT_S = 4.0
_CDP_POLL_HTTP_TIMEOUT_S = 1.0
_CDP_CONNECT_TIMEOUT_MS = 60000
_SUBPROC_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _current_cdp_endpoint():
    """Return the CDP endpoint for our managed Edge, or the default."""
    return state.cdp_endpoint or CDP_ENDPOINT


def _cdp_host_port():
    """Parse host and port from the current CDP endpoint."""
    parsed = urlparse(_current_cdp_endpoint())
    host = parsed.hostname or "localhost"
    port = parsed.port or 9222
    return host, port


def _cdp_http_get(path, timeout_s=_CDP_HTTP_TIMEOUT_S):
    """Fetch a CDP JSON endpoint with a bounded timeout."""
    host, port = _cdp_host_port()
    path = path if path.startswith("/") else f"/{path}"
    url = f"http://{host}:{port}{path}"
    try:
        request = urllib.request.Request(url, headers={"Connection": "close"})
        with urllib.request.urlopen(request, timeout=float(timeout_s)) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _cdp_version_info(timeout_s=_CDP_HTTP_TIMEOUT_S):
    """Return the /json/version payload when CDP is responding."""
    data = _cdp_http_get("/json/version", timeout_s=timeout_s)
    if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
        return data
    return None


def _is_cdp_port_open(port=None):
    """Return True when something is listening on the given (or current) CDP TCP port."""
    host, current_port = _cdp_host_port()
    target_port = current_port if port is None else port
    try:
        with socket.create_connection((host, target_port), timeout=0.5):
            return True
    except OSError:
        return False


def is_cdp_available():
    """Return True when CDP is serving a valid /json/version endpoint."""
    return _cdp_version_info(timeout_s=_CDP_POLL_HTTP_TIMEOUT_S) is not None


def is_cdp_port_free():
    """Return True when the CDP TCP port is not in use."""
    return not _is_cdp_port_open()


def _find_free_cdp_port(start=9222, count=100):
    """Return the first TCP port with nothing listening, starting at ``start``.

    Lets a new Edge bind a debugging port even when another automation is
    already using 9222, instead of killing that other instance.
    """
    for port in range(start, start + count):
        if not _is_cdp_port_open(port):
            return port
    return None


def _resolve_cdp_connect_url():
    """Return the browser-level CDP endpoint Playwright should attach to."""
    data = _cdp_version_info()
    if data:
        return data["webSocketDebuggerUrl"]
    return _current_cdp_endpoint()


def _wait_for_cdp_ready(timeout_s=_CDP_PORT_WAIT_S):
    """Poll until CDP serves /json/version (not just an open TCP port)."""
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        if not state.is_running:
            return False
        if _cdp_version_info(timeout_s=_CDP_POLL_HTTP_TIMEOUT_S) is not None:
            return True
        _interruptible_sleep(0.4)
    return False


def _wait_for_playwright_release(timeout_s=30):
    """Block until no Playwright session is active or the owner releases it."""
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        with state.playwright_lock:
            if state.current_playwright is None:
                return True
            owner = state.playwright_thread_id
        if owner == threading.get_ident():
            return True
        _interruptible_sleep(0.25)
    return False


def setup_browser():
    """Connect to a CDP-enabled Edge instance and return the best Availity tab."""
    if not _wait_for_playwright_release():
        raise RuntimeError(
            "Timed out waiting for the previous Playwright session to release"
        )

    with state.playwright_lock:
        if state.current_playwright is not None:
            if state.playwright_thread_id == threading.get_ident():
                disconnect_browser_session()
            else:
                raise RuntimeError(
                    "Playwright session is still active on another worker thread"
                )

        if not _wait_for_cdp_ready():
            raise RuntimeError(
                "CDP port did not become ready. Close all Edge windows and click "
                "Open Edge & Login again, or enable remote debugging at "
                "edge://inspect/#remote-debugging."
            )

        connect_url = _resolve_cdp_connect_url()
        state.current_playwright = sync_playwright().start()
        state.playwright_thread_id = threading.get_ident()
        state.playwright_disconnect_requested = False

        state.current_browser = retry_with_backoff(
            "Connect to CDP",
            lambda: state.current_playwright.chromium.connect_over_cdp(
                connect_url,
                timeout=_CDP_CONNECT_TIMEOUT_MS,
            ),
            attempts=_CDP_CONNECT_ATTEMPTS,
            base_delay=0.8,
            max_delay=3.0,
        )
        return _pick_startup_page(None)


def disconnect_browser_session():
    """Detach Playwright from the browser without closing Edge itself."""
    caller_id = threading.get_ident()

    with state.playwright_lock:
        if not state.current_playwright:
            state.playwright_disconnect_requested = False
            return

        owner_id = state.playwright_thread_id
        if owner_id is not None and caller_id != owner_id:
            state.playwright_disconnect_requested = True
            return

        pw = state.current_playwright
        state.current_playwright = None
        state.current_browser = None
        state.playwright_thread_id = None
        state.playwright_disconnect_requested = False

    try:
        pw.stop()
    except Exception as e:
        err_name = type(e).__name__
        if err_name == "error" and e.__class__.__module__ == "greenlet":
            log_to_gui("  ⚠️ Playwright already detached (greenlet)\n", "error")
        else:
            log_to_gui(f"  ⚠️ Browser cleanup error: {e}\n", "error")


def ensure_playwright_disconnected():
    """Disconnect on the owning worker thread (honors cross-thread requests)."""
    if state.playwright_disconnect_requested or state.current_playwright is not None:
        disconnect_browser_session()


def get_live_page(preferred_page=None):
    """Return a non-closed page, preferring Availity tabs when multiple are open."""
    if preferred_page is not None:
        try:
            if not preferred_page.is_closed():
                return preferred_page
        except Exception:
            pass

    pages = _get_all_open_pages(preferred_page)
    preferred = _find_best_availity_page(pages)
    if preferred is not None:
        return preferred

    for p in reversed(pages):
        try:
            if not p.is_closed():
                return p
        except Exception:
            continue
    raise RuntimeError("No live browser page available")


def _find_edge_executable():
    """Return a valid Edge executable path, or None if not found."""
    edge_candidates = [
        shutil.which("msedge"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in edge_candidates:
        if path and os.path.exists(path):
            return path
    return None


def _automation_user_data_dir():
    """Return the persistent, dedicated Edge profile for the automation.

    A fixed profile (separate from the user's normal Edge) keeps this instance
    isolated while persisting installed extensions (e.g. LastPass) and logins
    across launches. It is never deleted.
    """
    profile_dir = os.path.join(tempfile.gettempdir(), "availity-automation-edge-profile")
    os.makedirs(profile_dir, exist_ok=True)
    state.edge_user_data_dir = profile_dir
    return profile_dir


def _managed_edge_is_alive():
    """Return True when this app's own Edge process is still running."""
    return bool(
        state.managed_edge_process
        and state.managed_edge_process.poll() is None
    )


def _launch_managed_edge_with_cdp():
    """Spawn our own isolated Edge on a free debugging port and wait for CDP."""
    edge_exe = _find_edge_executable()
    if not edge_exe:
        log_to_gui("❌ Microsoft Edge executable not found.\n", "error")
        return False

    port = _find_free_cdp_port()
    if port is None:
        log_to_gui(
            "❌ Could not find a free debugging port for Edge.\n",
            "error",
        )
        return False

    state.cdp_port = port
    state.cdp_endpoint = f"http://localhost:{port}"
    user_data_dir = _automation_user_data_dir()
    args = [
        edge_exe,
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        AVAILITY_LOGIN_URL,
    ]
    try:
        state.managed_edge_process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_SUBPROC_FLAGS,
        )
        state.browser_owned_by_app = True
        log_to_gui(
            f"🟢 Opened Edge with CDP on localhost:{port} (dedicated profile)\n",
            "success",
        )
    except Exception as e:
        log_to_gui(f"❌ Could not launch Edge: {e}\n", "error")
        return False

    if not _wait_for_cdp_ready():
        log_to_gui(
            f"❌ CDP port did not become ready on localhost:{port}.\n"
            "   If an automation Edge window from a previous run is still open, "
            "close it, then click Open Edge & Login again.\n",
            "error",
        )
        return False
    return True


def ensure_edge_with_cdp():
    """Launch our own isolated Edge when needed; never touch other instances.

    Reuses only the Edge this app launched during the current session. Other
    Edge / Playwright instances are left running, and if their port is taken
    we simply pick a different free port for our own instance.
    """
    if _managed_edge_is_alive() and is_cdp_available():
        log_to_gui(
            f"ℹ️ Reusing our Edge with CDP on localhost:{state.cdp_port}\n",
            "info",
        )
        return True

    if _managed_edge_is_alive():
        log_to_gui("ℹ️ Waiting for CDP on our managed Edge instance...\n", "info")
        if _wait_for_cdp_ready(timeout_s=15):
            return True
        log_to_gui(
            "ℹ️ Managed Edge is not exposing CDP — launching a fresh instance...\n",
            "info",
        )

    log_to_gui("ℹ️ Launching a new isolated Edge instance...\n", "info")
    return _launch_managed_edge_with_cdp()


def close_managed_edge_if_owned():
    """Close Edge only if this automation launched it."""
    if state.browser_owned_by_app and state.managed_edge_process and state.managed_edge_process.poll() is None:
        try:
            state.managed_edge_process.terminate()
            state.managed_edge_process.wait(timeout=10)
            log_to_gui("🛑 Closed managed Edge browser\n", "info")
        except Exception:
            try:
                state.managed_edge_process.kill()
            except Exception:
                pass
        finally:
            state.managed_edge_process = None
            state.browser_owned_by_app = False
            _cleanup_edge_session_state()


def _cleanup_edge_session_state():
    """Clear per-session CDP tracking.

    The dedicated profile dir is persistent (holds extensions/logins) and is
    intentionally left on disk so the next launch reuses it.
    """
    state.cdp_port = None
    state.cdp_endpoint = None


def is_logged_in_navigation(url):
    """Return True when URL indicates logged-in Availity navigation shell."""
    if not isinstance(url, str):
        return False
    normalized = url.strip().lower()
    return "essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/" in normalized


def is_availity_login_url(url):
    """Return True when URL is the Availity sign-in page."""
    if not isinstance(url, str):
        return False
    normalized = url.strip().lower()
    if AVAILITY_LOGIN_URL.lower() in normalized:
        return True
    return "availity-fr-ui" in normalized and "login" in normalized


def is_availity_homepage_url(url):
    """Return True when URL is a logged-in Availity homepage or in-app shell."""
    if is_logged_in_navigation(url):
        return True
    if not isinstance(url, str):
        return False
    normalized = url.strip().lower()
    return (
        "enhanced-claim-status-ui" in normalized
        or "%2fcs%2fenhanced-claim-status-ui" in normalized
    )


def _cdp_tab_urls():
    """Return open tab URLs from the CDP HTTP list (no Playwright attach)."""
    data = _cdp_http_get("/json/list", timeout_s=_CDP_POLL_HTTP_TIMEOUT_S)
    if not isinstance(data, list):
        data = _cdp_http_get("/json", timeout_s=_CDP_POLL_HTTP_TIMEOUT_S)
    if not isinstance(data, list):
        return []

    urls = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tab_type = item.get("type")
        if tab_type not in (None, "", "page"):
            continue
        url = item.get("url") or ""
        if not url or url.startswith(
            ("chrome://", "edge://", "devtools://", "chrome-extension://")
        ):
            continue
        urls.append(url)
    return urls


def cdp_has_availity_homepage():
    """True when a CDP tab is on the Availity homepage / logged-in shell."""
    return any(is_availity_homepage_url(url) for url in _cdp_tab_urls())


def is_claim_search_page(page):
    """Return True when the tab is on an Availity claim-status search app."""
    try:
        if page is None or page.is_closed():
            return False
        url = page.url.lower()
        return (
            "enhanced-claim-status-ui" in url
            or "%2fcs%2fenhanced-claim-status-ui" in url
        )
    except Exception:
        return False


def is_remittance_viewer_page(page):
    """Return True when the tab is the Availity remittance viewer."""
    try:
        if page is None or page.is_closed():
            return False
        url = (page.url or '').lower()
        return (
            'remitviewerui' in url
            or '%2frm%2fremitviewerui' in url
        )
    except Exception:
        return False


def _get_all_open_pages(candidate_page=None):
    """Return deduplicated open pages from current and browser-wide contexts."""
    pages = []
    seen_ids = set()

    def _add(page):
        try:
            if page is None or page.is_closed():
                return
            pid = id(page)
            if pid in seen_ids:
                return
            seen_ids.add(pid)
            pages.append(page)
        except Exception:
            return

    # Include candidate tab/context first.
    _add(candidate_page)
    if candidate_page is not None:
        try:
            for p in candidate_page.context.pages:
                _add(p)
        except Exception:
            pass

    # Always include every page from every connected browser context.
    if state.current_browser is not None:
        try:
            for ctx in state.current_browser.contexts:
                for p in ctx.pages:
                    _add(p)
        except Exception:
            pass

    return pages


def _find_best_availity_page(pages):
    """Pick the best Availity tab when several browser tabs are open."""
    for checker in (
        is_claim_search_page,
        lambda p: (
            is_logged_in_navigation(getattr(p, "url", ""))
            and not is_remittance_viewer_page(p)
        ),
        lambda p: AVAILITY_LOGIN_URL in (getattr(p, "url", "") or ""),
    ):
        for p in reversed(pages):
            try:
                if p.is_closed():
                    continue
                if checker(p):
                    return p
            except Exception:
                continue
    return None


def _close_extra_tabs(keep_page, pages=None):
    """Close every open tab except ``keep_page``."""
    if keep_page is None:
        return 0

    if pages is None:
        pages = _get_all_open_pages(keep_page)

    keep_id = id(keep_page)
    closed = 0
    for p in pages:
        if id(p) == keep_id:
            continue
        try:
            if p.is_closed():
                continue
            p.close()
            closed += 1
        except Exception:
            continue
    return closed


def _pick_startup_page(page=None):
    """Choose the best Availity tab after CDP attach and close the rest."""
    pages = _get_all_open_pages(page)
    if not pages:
        if page is not None:
            return page
        context = (
            state.current_browser.contexts[0]
            if state.current_browser and state.current_browser.contexts
            else None
        )
        if context is not None:
            return context.new_page()
        raise RuntimeError("No browser pages available after CDP connect")

    preferred = _find_best_availity_page(pages) or pages[-1]
    tab_count = len(pages)
    if tab_count > 1:
        log_to_gui(
            f"ℹ️ {tab_count} tab(s) open — selected Availity tab: {preferred.url}\n",
            "info",
        )
    else:
        log_to_gui(f"ℹ️ Using startup tab: {preferred.url}\n", "info")

    closed = _close_extra_tabs(preferred, pages)
    if closed:
        log_to_gui(f"ℹ️ Closed {closed} extra tab(s)\n", "info")

    try:
        preferred.bring_to_front()
    except Exception:
        pass
    return preferred


def reset_tabs_for_session_start(page):
    """Normalize browser tabs at startup to avoid stale-tab context issues."""
    return _pick_startup_page(page)


def validate_navigation_page_ready(page, timeout_ms=5000):
    """Validate that a candidate page is truly logged-in and fully loaded."""
    try:
        if page is None:
            return False, "page is None"
        if page.is_closed():
            return False, "page is closed"

        url = page.url
        if not is_logged_in_navigation(url):
            return False, "url is not navigation shell"

        # Ensure document has loaded enough for shell interactions.
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 4000))
        except Exception:
            # networkidle may not settle on SPAs; domcontentloaded is sufficient.
            pass
        return True, "ready"
    except Exception as e:
        return False, f"navigation readiness check failed: {e}"


def _find_logged_in_page(candidate_page):
    """Return an authenticated, fully loaded navigation-shell page.

  Prefers a tab already on claim search so batch processing does not drop
  back to the empty navigation shell after session verification.
    """
    if candidate_page is not None:
        try:
            if is_claim_search_page(candidate_page):
                return candidate_page
        except Exception:
            pass

    pages = _get_all_open_pages(candidate_page)
    for p in reversed(pages):
        try:
            if is_claim_search_page(p):
                return p
        except Exception:
            continue

    if candidate_page is not None:
        try:
            if not is_remittance_viewer_page(candidate_page):
                ready, _ = validate_navigation_page_ready(candidate_page, timeout_ms=2000)
                if ready:
                    return candidate_page
        except Exception:
            pass

    for p in reversed(pages):
        try:
            if is_remittance_viewer_page(p):
                continue
            ready, _ = validate_navigation_page_ready(p, timeout_ms=2000)
            if ready:
                return p
        except Exception:
            continue
    return None


def _interruptible_sleep(seconds):
    """Sleep up to `seconds`, returning early if is_running becomes False."""
    end = time.time() + float(seconds)
    while state.is_running and time.time() < end:
        time.sleep(min(0.5, end - time.time()))
    return state.is_running
