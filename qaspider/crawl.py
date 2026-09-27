"""Path-scoped, same-origin breadth-first crawler using rendered browser DOM."""

from __future__ import annotations

import gzip
import io
import os
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import contextmanager
from time import monotonic, sleep
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .inventory import extract_navigable_links, inspect_page
from .urls import (
    normalize_redirect_url,
    normalize_url,
    path_is_in_scope,
    path_scope_from_url,
    same_origin,
)

MAX_REDIRECT_HOPS = 10
MAX_SITEMAP_BYTES = 5 * 1024 * 1024
MAX_SITEMAP_EXPANDED_BYTES = 25 * 1024 * 1024
MAX_REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.05
# Rendered documents are buffered once by the fetch and again by `route.fulfill`,
# so an oversized document is refused instead of being materialized.
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
# Media embeds are not needed to render the DOM and can exhaust the browser's
# network buffer, so they are aborted while every other resource type passes.
BLOCKED_RESOURCE_TYPES = frozenset({"media"})
# Upper bound on browser relaunches for a single crawl, so a site that kills the
# browser on every page cannot turn into an unbounded relaunch loop.
MAX_BROWSER_RECOVERIES = 3

STOP_BROWSER_RECOVERY_BUDGET = "browser-recovery-budget-exhausted"
STOP_BROWSER_RELAUNCH_FAILED = "browser-relaunch-failed"


_TRANSIENT_NETWORK_MARKERS = (
    "net::err_",
    "socket hang up",
    "econnreset",
    "econnrefused",
    "etimedout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network error",
    "failed to fetch",
    "timed out",
    "timeout",
)
# Diagnostics that mean the crawl target is gone (page, context, or browser),
# as opposed to a single request failing. Matched against a lowercased copy of
# the message and never serialized.
_CLOSED_TARGET_MARKERS = (
    "target page, context or browser has been closed",
    "target closed",
    "target crashed",
    "page crashed",
    "browser has been closed",
    "browser is closed",
    "browser is not connected",
    "context has been closed",
    "context is closed",
    "page has been closed",
    "page is closed",
    "request context disposed",
)
_SAFE_POLICY_MESSAGES = {
    "Redirect response was missing a Location header.",
    "Redirect response contained an invalid Location header.",
    "off-origin redirect was blocked.",
    "Redirect loop was blocked.",
    "Redirect hop limit was reached.",
    "Navigation outside the crawl origin was blocked.",
    "Redirect outside the crawl path was blocked.",
    "Navigation outside the crawl path was blocked.",
    "Browser closed unexpectedly.",
    "Document exceeded the size limit.",
}


class _SafeCrawlError(Exception):
    """Crawler-owned policy failure with a fixed, safe diagnostic."""

    def __init__(self, safe_message: str):
        super().__init__()
        self.safe_message = (
            safe_message if safe_message in _SAFE_POLICY_MESSAGES else "Crawler request failed."
        )


def _is_transient_network_error(error: Exception) -> bool:
    if not isinstance(error, PlaywrightError):
        return False
    # Playwright keeps the diagnostic on `message`; it must never be serialized.
    diagnostic = getattr(error, "message", "")
    if not isinstance(diagnostic, str):
        return False
    diagnostic = diagnostic[:4_096].lower()
    return any(marker in diagnostic for marker in _TRANSIENT_NETWORK_MARKERS)


def _target_is_closed(page) -> bool:
    """Report whether the page object is already known to be closed."""
    if page is None:
        return False
    try:
        return bool(page.is_closed())
    except Exception:
        # A page that cannot answer is not a page that can be crawled.
        return True


def _is_closed_target_error(error: Exception, page=None) -> bool:
    """Distinguish a lost browser target from a failed single request.

    A dead page, context, or browser must not be recorded as a page-level
    network error: it is recoverable by relaunching. Three signals are used,
    in order, because each covers a different failure: Playwright's own closed
    target error class, its diagnostic text, and the page's liveness flag (the
    only one that still works when the process died mid-flight).
    """
    if not isinstance(error, PlaywrightError):
        return False
    if type(error).__name__ == "TargetClosedError":
        return True
    diagnostic = getattr(error, "message", "")
    if isinstance(diagnostic, str):
        lowered = diagnostic[:4_096].lower()
        if any(marker in lowered for marker in _CLOSED_TARGET_MARKERS):
            return True
    return _target_is_closed(page)


def _abort_route(route) -> bool:
    """Abort a routed request without letting the failure escape the handler."""
    try:
        route.abort("blockedbyclient")
    except Exception:
        return False
    return True


def _unroute_page(page, route_handler) -> bool:
    """Remove a page route, reporting failure instead of raising.

    Teardown runs in a `finally` block, where a raise would replace the result
    the crawl already produced.
    """
    try:
        page.unroute("**/*", route_handler)
    except Exception:
        return False
    return True


def _declared_content_length(response) -> int | None:
    """Return the declared body size, or None when it is absent or unparsable."""
    declared = response.headers.get("content-length")
    if not declared:
        return None
    try:
        return int(str(declared).strip())
    except (TypeError, ValueError):
        return None


class _BrowserSession:
    """Browser, context, and page that can be torn down and relaunched safely.

    Closing a browser that already died raises, so every close is isolated: a
    failure is counted, never propagated, and never allowed to discard an
    already-inventoried page record.
    """

    def __init__(self, playwright):
        self._playwright = playwright
        self.browser = None
        self.context = None
        self.page = None

    def start(self) -> None:
        """Launch a browser and open a context and page on it."""
        self.browser = self._playwright.chromium.launch()
        try:
            self.context = self.browser.new_context()
            self.page = self.context.new_page()
        except Exception:
            self.close()
            raise

    def close(self) -> int:
        """Close the context then the browser, returning the failed close count."""
        context, self.context = self.context, None
        browser, self.browser = self.browser, None
        self.page = None
        failures = 0
        if context is not None:
            try:
                context.close()
            except Exception:
                failures += 1
        if browser is not None:
            try:
                browser.close()
            except Exception:
                failures += 1
        return failures


@contextmanager
def _playwright_driver():
    """Start Playwright with system CA roots when not explicitly configured."""
    variable = "NODE_USE_SYSTEM_CA"
    configured_here = variable not in os.environ
    if configured_here:
        os.environ[variable] = "1"

    try:
        with sync_playwright() as playwright:
            yield playwright
    finally:
        if configured_here:
            os.environ.pop(variable, None)


def _request_with_retries(page, requested_url: str, deadline: float):
    """Retry transient Playwright transport failures without exceeding a deadline."""
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        remaining_ms = int((deadline - monotonic()) * 1_000)
        if remaining_ms <= 0:
            raise PlaywrightTimeoutError("Request deadline exceeded.")
        try:
            return page.request.get(
                requested_url, max_redirects=0, timeout=remaining_ms
            )
        except PlaywrightError as error:
            # A lost browser is not transient: it needs a relaunch, not a retry.
            if (
                _is_closed_target_error(error, page)
                or not _is_transient_network_error(error)
                or attempt == MAX_REQUEST_ATTEMPTS - 1
            ):
                raise
            backoff = RETRY_BACKOFF_SECONDS * (2 ** attempt)
            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0:
                raise
            sleep(min(backoff, remaining_seconds))


def _error_record(error: Exception) -> str:
    if isinstance(error, _SafeCrawlError):
        return error.safe_message
    if _is_closed_target_error(error):
        return "Browser closed unexpectedly."
    if isinstance(error, PlaywrightTimeoutError):
        return "Request timed out."
    if isinstance(error, PlaywrightError):
        if _is_transient_network_error(error):
            return "Network request failed."
        return "Browser request failed."
    if isinstance(error, (ET.ParseError, ValueError)):
        return "Invalid sitemap response."
    return "Crawler request failed."


def _fetch_same_origin_response(
    page,
    requested_url: str,
    origin_text: str,
    path_scope: str,
    deadline: float,
    page_record: dict,
):
    """Fetch a page without allowing the HTTP client to follow unchecked redirects."""
    current_url = requested_url
    visited = {normalize_redirect_url(current_url)}
    redirects_followed = 0

    while True:
        remaining_ms = int((deadline - monotonic()) * 1_000)
        if remaining_ms <= 0:
            raise PlaywrightTimeoutError("Timed out while fetching the page and its redirects.")

        response = _request_with_retries(page, current_url, deadline)
        if not 200 <= response.status < 300:
            page_record["statusCode"] = response.status
        if not 300 <= response.status < 400:
            declared_length = _declared_content_length(response)
            if declared_length is not None and declared_length > MAX_DOCUMENT_BYTES:
                # The body would be held twice (fetch buffer plus fulfill), which
                # is what exhausts the browser on media-heavy pages. Refuse it
                # here so the page becomes an agent-check record instead.
                raise _SafeCrawlError("Document exceeded the size limit.")
            if 200 <= response.status < 300:
                page_record.pop("statusCode", None)
            return current_url, response

        location = response.headers.get("location")
        if not location:
            raise _SafeCrawlError("Redirect response was missing a Location header.")

        try:
            redirect_url = urljoin(current_url, location)
            normalized_redirect_url = normalize_redirect_url(redirect_url)
        except ValueError as error:
            raise _SafeCrawlError("Redirect response contained an invalid Location header.") from error

        if normalized_redirect_url is None:
            raise _SafeCrawlError("off-origin redirect was blocked.")
        if not same_origin(normalized_redirect_url, origin_text):
            raise _SafeCrawlError("off-origin redirect was blocked.")
        if not path_is_in_scope(normalized_redirect_url, path_scope):
            raise _SafeCrawlError("Redirect outside the crawl path was blocked.")
        if normalized_redirect_url in visited:
            raise _SafeCrawlError("Redirect loop was blocked.")
        if redirects_followed >= MAX_REDIRECT_HOPS:
            raise _SafeCrawlError("Redirect hop limit was reached.")

        visited.add(normalized_redirect_url)
        current_url = normalized_redirect_url
        redirects_followed += 1


def _fetch_auxiliary_response(page, requested_url: str, origin_text: str, timeout_ms: int):
    """Fetch robots or sitemap resources with the same-origin redirect policy."""
    current_url = requested_url
    visited = {normalize_redirect_url(requested_url)}
    deadline = monotonic() + timeout_ms / 1_000
    for redirects_followed in range(MAX_REDIRECT_HOPS + 1):
        response = _request_with_retries(page, current_url, deadline)
        if not 300 <= response.status < 400:
            return current_url, response
        location = response.headers.get("location")
        if not location:
            raise _SafeCrawlError("Redirect response was missing a Location header.")
        normalized = normalize_redirect_url(urljoin(current_url, location))
        if normalized is None or not same_origin(normalized, origin_text):
            raise _SafeCrawlError("off-origin redirect was blocked.")
        if normalized in visited:
            raise _SafeCrawlError("Redirect loop was blocked.")
        if redirects_followed >= MAX_REDIRECT_HOPS:
            raise _SafeCrawlError("Redirect hop limit was reached.")
        visited.add(normalized)
        current_url = normalized
    raise _SafeCrawlError("Redirect hop limit was reached.")


def _bounded_response_body(response, *, expanded_limit: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length and int(content_length) > MAX_SITEMAP_BYTES:
        raise ValueError(f"Response exceeds the {MAX_SITEMAP_BYTES}-byte download limit.")
    body = response.body()
    if len(body) > MAX_SITEMAP_BYTES:
        raise ValueError(f"Response exceeds the {MAX_SITEMAP_BYTES}-byte download limit.")
    if body.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            body = compressed.read(expanded_limit + 1)
        if len(body) > expanded_limit:
            raise ValueError(f"Expanded sitemap exceeds the {expanded_limit}-byte limit.")
    return body


def _discover_sitemap_pages(
    page, start_url: str, origin_text: str, path_scope: str, timeout_ms: int
):
    """Return in-scope page URLs listed by same-origin sitemaps."""
    sitemap_files: deque[tuple[str, bool]] = deque()
    issues: list[dict[str, str | int]] = []
    robots_url = normalize_redirect_url("/robots.txt", start_url)
    default_sitemap = normalize_redirect_url("/sitemap.xml", start_url)

    try:
        final_robots_url, robots_response = _fetch_auxiliary_response(
            page, robots_url, origin_text, timeout_ms
        )
        if 200 <= robots_response.status < 300:
            body = _bounded_response_body(robots_response, expanded_limit=MAX_SITEMAP_BYTES)
            for line in body.decode("utf-8-sig", errors="replace").splitlines():
                directive, separator, location = line.partition(":")
                if separator and directive.strip().lower() == "sitemap":
                    candidate = normalize_redirect_url(location.strip(), final_robots_url)
                    if candidate and same_origin(candidate, origin_text):
                        sitemap_files.append((candidate, True))
        elif robots_response.status >= 500:
            issues.append({
                "url": robots_url,
                "statusCode": robots_response.status,
                "error": f"HTTP {robots_response.status}",
            })
    except Exception as error:
        issues.append({"url": robots_url, "error": _error_record(error)})

    sitemap_files.append((default_sitemap, False))
    visited_sitemaps: set[str] = set()
    page_urls: list[str] = []
    discovered_pages: set[str] = set()

    while sitemap_files:
        sitemap_url, explicitly_declared = sitemap_files.popleft()
        sitemap_url = normalize_redirect_url(sitemap_url)
        if sitemap_url is None or not same_origin(sitemap_url, origin_text):
            continue
        if sitemap_url in visited_sitemaps:
            continue
        visited_sitemaps.add(sitemap_url)
        try:
            final_url, response = _fetch_auxiliary_response(page, sitemap_url, origin_text, timeout_ms)
            if not 200 <= response.status < 300:
                if explicitly_declared or response.status >= 500:
                    issues.append({
                        "url": sitemap_url,
                        "statusCode": response.status,
                        "error": f"HTTP {response.status}",
                    })
                continue
            body = _bounded_response_body(response, expanded_limit=MAX_SITEMAP_EXPANDED_BYTES)
            root = ET.fromstring(body)
            root_kind = root.tag.rsplit("}", 1)[-1].lower()
            if root_kind not in {"urlset", "sitemapindex"}:
                raise ValueError("XML document is not a sitemap urlset or sitemap index.")

            for entry in root:
                location = next(
                    (child.text.strip() for child in entry
                     if child.tag.rsplit("}", 1)[-1].lower() == "loc" and child.text),
                    None,
                )
                if not location:
                    continue
                candidate = normalize_redirect_url(location, final_url)
                if candidate is None or not same_origin(candidate, origin_text):
                    continue
                if root_kind == "sitemapindex":
                    sitemap_files.append((candidate, True))
                elif path_is_in_scope(candidate, path_scope):
                    identity = normalize_url(candidate)
                    if identity is None or identity in discovered_pages:
                        continue
                    discovered_pages.add(identity)
                    page_urls.append(candidate)
        except Exception as error:
            issues.append({"url": sitemap_url, "error": _error_record(error)})

    return page_urls, issues


def crawl_site(
    start_url: str,
    *,
    max_pages: int | None = None,
    max_depth: int | None = None,
    timeout_ms: int = 30_000,
) -> dict:
    """Crawl pages within the start URL's same-origin path scope.

    With no explicit page or depth caps, the crawler processes the breadth-first
    queue until it is exhausted. Pages that cannot be inventoried are represented
    as records with ``action: agent-check`` and no ``elements`` field. Navigable
    links may still be discovered from rendered non-2xx response documents.

    Losing the browser is survivable: the page in flight becomes an
    ``agent-check`` record, the browser is relaunched up to
    ``MAX_BROWSER_RECOVERIES`` times, and the crawl continues with the pages
    that are still queued. Already-inventoried pages are never discarded by
    teardown, so the caller always receives whatever was collected. Once the
    recovery budget is exhausted the queue is left unconsumed and reported via
    ``stats.stopReason``, ``stats.truncated``, and ``stats.complete``.
    """
    if max_pages is not None and max_pages <= 0:
        raise ValueError("max_pages must be greater than zero.")
    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth must be zero or greater.")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be greater than zero.")

    # Capture the original URL's path before canonicalization removes trailing slashes.
    path_scope = path_scope_from_url(start_url)
    request_start = normalize_redirect_url(start_url)
    normalized_start = normalize_url(start_url)
    if request_start is None or normalized_start is None:
        raise ValueError("The start URL must use HTTP or HTTPS.")
    origin = urlsplit(normalized_start)
    origin_text = f"{origin.scheme}://{origin.netloc}"
    queue = deque([(request_start, 0)])
    queued = {normalized_start}
    discovered: set[str] = {normalized_start}
    depth_suppressed: set[str] = set()
    canonical_completed: set[str] = set()
    pages: list[dict] = []
    failed = 0
    processed = 0
    browser_losses = 0
    browser_recoveries = 0
    teardown_failures = 0
    stop_reason: str | None = None
    result: dict | None = None

    def discover(url: str, depth: int) -> None:
        request_url = normalize_redirect_url(url)
        if (request_url is None or not same_origin(request_url, origin_text)
                or not path_is_in_scope(request_url, path_scope)):
            return
        identity = normalize_url(request_url)
        if identity is None:
            return
        discovered.add(identity)
        if identity in queued or identity in canonical_completed:
            depth_suppressed.discard(identity)
            return
        if max_depth is not None and depth > max_depth:
            depth_suppressed.add(identity)
            return
        depth_suppressed.discard(identity)
        queued.add(identity)
        queue.append((request_url, depth))

    def relaunch_browser(session: _BrowserSession) -> str | None:
        """Tear the session down and bring a new one up after a browser loss.

        Returns a stop reason when the crawl cannot continue, otherwise None.
        The recovery budget is bounded so a site that kills the browser on
        every page stops the crawl instead of relaunching forever.
        """
        nonlocal browser_recoveries, teardown_failures
        if browser_recoveries >= MAX_BROWSER_RECOVERIES:
            return STOP_BROWSER_RECOVERY_BUDGET
        teardown_failures += session.close()
        try:
            session.start()
        except Exception:
            teardown_failures += session.close()
            return STOP_BROWSER_RELAUNCH_FAILED
        browser_recoveries += 1
        return None

    try:
        with _playwright_driver() as playwright:
            session = _BrowserSession(playwright)
            try:
                session.start()
                page = session.page
                sitemap_urls, sitemap_issues = _discover_sitemap_pages(
                    page, request_start, origin_text, path_scope, timeout_ms
                )
                for sitemap_url in sitemap_urls:
                    discover(sitemap_url, 1)

                while queue and (max_pages is None or processed < max_pages):
                    requested_url, depth = queue.popleft()
                    processed += 1
                    requested_identity = normalize_url(requested_url)
                    page_record = {"url": requested_identity or requested_url}
                    duplicate_page = False
                    route_handler = None
                    blocked_main_navigation: list[tuple[str, str]] = []
                    target_lost = False
                    # Re-read the page every iteration: recovery replaces it.
                    page = session.page

                    try:
                        page.set_default_navigation_timeout(timeout_ms)
                        navigation_deadline = monotonic() + timeout_ms / 1_000
                        final_request_url, fetched_response = _fetch_same_origin_response(
                            page, requested_url, origin_text, path_scope,
                            navigation_deadline, page_record
                        )
                        # Keep the request URL's slash and query for navigation; only
                        # canonicalize the page identity used for output and dedupe.
                        final_url = normalize_url(final_request_url)
                        if final_url is None:
                            raise _SafeCrawlError("off-origin redirect was blocked.")
                        if not same_origin(final_url, origin_text):
                            raise _SafeCrawlError("off-origin redirect was blocked.")
                        if not path_is_in_scope(final_url, path_scope):
                            raise _SafeCrawlError("Redirect outside the crawl path was blocked.")
                        page_record["statusCode"] = fetched_response.status
                        discovered.add(final_url)
                        if final_url in canonical_completed:
                            duplicate_page = True
                        else:
                            page_record["url"] = final_url
                        if not duplicate_page:
                            main_document_fulfilled = False

                            def build_route_handler(target_page):
                                """Bind a route handler to the page it will serve.

                                The page is captured per handler instead of read
                                from the loop variable, so a handler left over
                                from a replaced browser cannot act on the new
                                page's main frame.
                                """

                                def fulfill_main_document(route):
                                    nonlocal main_document_fulfilled
                                    # A dead target can fail mid-handler; never
                                    # let that escape into route dispatch.
                                    try:
                                        request = route.request
                                        if (request.is_navigation_request()
                                                and request.frame == target_page.main_frame):
                                            if (not main_document_fulfilled
                                                    and normalize_redirect_url(request.url)
                                                    == normalize_redirect_url(final_request_url)):
                                                main_document_fulfilled = True
                                                route.fulfill(response=fetched_response)
                                                return
                                            navigation_url = normalize_redirect_url(request.url)
                                            if navigation_url is None or not same_origin(
                                                navigation_url, origin_text
                                            ):
                                                reason = "Navigation outside the crawl origin was blocked."
                                            elif not path_is_in_scope(navigation_url, path_scope):
                                                reason = "Navigation outside the crawl path was blocked."
                                            else:
                                                reason = "Unexpected main-frame navigation was blocked."
                                            blocked_main_navigation.append((request.url, reason))
                                            route.abort("blockedbyclient")
                                            return
                                        if request.resource_type in BLOCKED_RESOURCE_TYPES:
                                            # Media is not needed to inventory the DOM
                                            # and is the main driver of network-buffer
                                            # exhaustion. Every other resource type is
                                            # continued so the document still renders.
                                            _abort_route(route)
                                            return
                                        route.continue_()
                                    except Exception:
                                        _abort_route(route)

                                return fulfill_main_document

                            # Playwright picks the handler arity by inspecting its
                            # signature, so the handler must take the route alone.
                            route_handler = build_route_handler(page)
                            page.route("**/*", route_handler)
                            remaining_ms = max(1, int((navigation_deadline - monotonic()) * 1_000))
                            page.goto(final_request_url, wait_until="domcontentloaded", timeout=remaining_ms)
                            current_url = page.url
                            if not same_origin(current_url, origin_text):
                                raise _SafeCrawlError("Navigation outside the crawl origin was blocked.")
                            if not path_is_in_scope(current_url, path_scope):
                                raise _SafeCrawlError("Navigation outside the crawl path was blocked.")
                            if not 200 <= fetched_response.status < 300:
                                page_record["action"] = "agent-check"
                                page_record["error"] = f"HTTP {fetched_response.status}"

                            settle_timeout_ms = min(
                                1_000,
                                max(0, int((navigation_deadline - monotonic()) * 1_000)),
                            )
                            if settle_timeout_ms:
                                try:
                                    page.wait_for_load_state("networkidle", timeout=settle_timeout_ms)
                                except PlaywrightTimeoutError:
                                    pass
                            if 200 <= fetched_response.status < 300:
                                inventory, raw_links = inspect_page(page, origin_text)
                                page_record["elements"] = inventory
                                page_record.pop("statusCode", None)
                            else:
                                raw_links = extract_navigable_links(page)
                            # Dedupe state is crawl-scoped, so a relaunched browser
                            # can never re-inventoried or duplicate a page.
                            canonical_completed.add(final_url)

                            for raw_link in raw_links:
                                try:
                                    candidate = normalize_redirect_url(raw_link, current_url)
                                except ValueError:
                                    continue
                                if candidate is not None:
                                    discover(candidate, depth + 1)
                    except Exception as error:
                        page_record["action"] = "agent-check"
                        page_record.pop("elements", None)
                        target_lost = _is_closed_target_error(error, page)
                        if blocked_main_navigation:
                            page_record["error"] = blocked_main_navigation[0][1]
                        elif target_lost:
                            # The document may have answered before the target
                            # died, so report the loss with the fixed safe text
                            # rather than whatever Playwright put in the error.
                            page_record["error"] = "Browser closed unexpectedly."
                        else:
                            page_record["error"] = _error_record(error)
                    finally:
                        # Teardown must not raise: this runs after the record for
                        # the page is already decided, and a raise here would
                        # discard every page inventoried so far.
                        if route_handler is not None and not _unroute_page(page, route_handler):
                            teardown_failures += 1
                        if page_record.get("action") == "agent-check":
                            failed += 1
                        if not duplicate_page:
                            pages.append(page_record)

                    if not target_lost:
                        continue
                    browser_losses += 1
                    # Stop consuming the queue once recovery is no longer possible;
                    # whatever is left stays visible as `pending`.
                    stop_reason = relaunch_browser(session)
                    if stop_reason is not None:
                        break

                pending = len(queue)
                truncated = bool(pending or depth_suppressed)
                complete = (
                    stop_reason is None and not truncated and failed == 0
                    and not sitemap_issues
                )
                # Built before teardown so a failed close can be reported without
                # losing the pages that were already inventoried.
                result = {
                    "startUrl": normalized_start,
                    "pages": pages,
                    "stats": {
                        "discovered": len(discovered),
                        "processed": processed,
                        "failed": failed,
                        "maxPages": max_pages,
                        "maxDepth": max_depth,
                        "pending": pending,
                        "depthSuppressed": len(depth_suppressed),
                        "complete": complete,
                        "truncated": truncated,
                        "issues": sitemap_issues,
                        "browserLosses": browser_losses,
                        "browserRecoveries": browser_recoveries,
                        "maxBrowserRecoveries": MAX_BROWSER_RECOVERIES,
                        "stopReason": stop_reason,
                        "teardownFailures": 0,
                    },
                }
            finally:
                teardown_failures += session.close()
                if result is not None:
                    result["stats"]["teardownFailures"] = teardown_failures
    except Exception:
        # Only driver teardown can fail once `result` exists: the collected
        # report outranks a cleanup error, and a real crawl failure re-raises.
        if result is None:
            raise
        result["stats"]["teardownFailures"] += 1
    return result
