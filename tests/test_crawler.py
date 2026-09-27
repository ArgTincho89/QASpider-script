"""Local-only tests for URL normalization and rendered-page crawling."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from qaspider.cli import build_parser, main
from qaspider.crawl import (
    MAX_BROWSER_RECOVERIES,
    MAX_DOCUMENT_BYTES,
    STOP_BROWSER_RECOVERY_BUDGET,
    _playwright_driver,
    crawl_site,
)
from qaspider.inventory import CANONICAL_INVENTORY_KEYS, INVENTORY_KEYS, empty_inventory
from qaspider.urls import normalize_redirect_url, normalize_url, same_origin

# One byte larger than the document bound, so the smallest honest oversize case
# is exercised without shipping megabytes of fixture data.
OVERSIZED_DOCUMENT_BYTES = MAX_DOCUMENT_BYTES + 1
TRANSPARENT_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\xda\x63\xfc\xff\x9f\x05\x00"
    b"\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FixtureHandler(BaseHTTPRequestHandler):
    external_origin = ""
    external_target_requests = 0
    external_target_lock = threading.Lock()
    sitemap_enabled = False
    auxiliary_redirect_enabled = False
    scoped_sitemap_enabled = False
    relative_links_enabled = False
    scoped_metadata_requests = 0
    scoped_outside_requests = 0
    post_target_requests = 0
    retry_target_requests = 0
    retry_target_lock = threading.Lock()
    media_asset_requests = 0
    image_asset_requests = 0
    style_asset_requests = 0
    script_asset_requests = 0

    def _send_static(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {"/demo/mars2.html", "/demo/other"} or self.path.startswith(
            "/demo/mars2.html?"
        ):
            type(self).scoped_outside_requests += 1
            body = b"<html><body><main><h1>Out of scope</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/demo/mars/redirect-outside":
            self.send_response(302)
            self.send_header("Location", "/demo/mars2.html")
            self.end_headers()
            return
        if self.path in {"/demo/mars", "/demo/mars/"}:
            if type(self).relative_links_enabled:
                body = b"""<!doctype html><html><body><main>
                  <a href='mars2.html?a=send_me_to_mars'>Mars</a>
                  <a href='mars2.html?a=hotels'>Hotels</a>
                  <a href='mars2.html?a=things_to_do'>Things to do</a>
                  <a href='mars2.html?a=mars_map'>Mars map</a>
                  <a href='mars2.html?a=sign_in'>Sign in</a>
                  <a href='mars2.html?a=cart'>Cart</a>
                  </main></body></html>"""
            else:
                body = b"""<!doctype html><html><body><main>
              <a href='/demo/mars'>Base</a>
              <a href='/demo/mars/child?view=one'>Scoped query</a>
              <a href='/demo/mars2.html'>Sibling prefix</a>
              <a href='/demo/other'>Outside sibling</a>
              <a href='/outside'>Outside root path</a>
              </main></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in {
            "/demo/mars/child?view=one",
            "/demo/mars/sitemap-page",
            "/demo/mars/sitemap-page/?source=xml%20one",
        }:
            body = b"<html><body><main><h1>Scoped page</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        relative_pages = {
            f"/demo/mars/mars2.html?a={query}": query
            for query in (
                "send_me_to_mars",
                "hotels",
                "things_to_do",
                "mars_map",
                "sign_in",
                "cart",
            )
        }
        if self.path in relative_pages:
            body = (
                f"<html><body><main><h1>{relative_pages[self.path]}</h1></main></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/slash-target":
            self.send_response(301)
            self.send_header("Location", "/slash-target/")
            self.end_headers()
            return
        if self.path == "/slash-target/":
            body = b"<html><body><main><h1>Slash redirect target</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in {"/redirect-loop-a", "/redirect-loop-a/redirect-loop-b"}:
            destination = (
                "/redirect-loop-a/redirect-loop-b"
                if self.path == "/redirect-loop-a"
                else "/redirect-loop-a"
            )
            self.send_response(301)
            self.send_header("Location", destination)
            self.end_headers()
            return
        if self.path == "/robots.txt/" and type(self).auxiliary_redirect_enabled:
            body = f"Sitemap: {self.server.origin}/aux-sitemap.xml\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/aux-sitemap.xml":
            self.send_response(301)
            self.send_header("Location", "/aux-sitemap.xml/")
            self.end_headers()
            return
        if self.path == "/aux-sitemap.xml/":
            body = f"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
              <url><loc>{self.server.origin}/aux-redirect-page</loc></url></urlset>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/aux-redirect-page":
            body = b"<html><body><main><h1>Auxiliary redirect target</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/retry-once":
            with self.retry_target_lock:
                type(self).retry_target_requests += 1
                should_drop = type(self).retry_target_requests == 1
            if should_drop:
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            body = b"<html><body><main><h1>Recovered page</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/outside"):
            with self.external_target_lock:
                type(self).external_target_requests += 1
        if self.path == "/robots.txt":
            if type(self).auxiliary_redirect_enabled:
                self.send_response(301)
                self.send_header("Location", "/robots.txt/")
                self.end_headers()
                return
            if type(self).scoped_sitemap_enabled:
                body = f"Sitemap: {self.server.origin}/sitemap-index.xml\n".encode()
                self.send_response(200)
            elif type(self).sitemap_enabled:
                body = (
                    f"Sitemap: {self.server.origin}/sitemap-index.xml\n"
                    f"Sitemap: {self.server.origin}/sitemap.xml\n"
                    f"Sitemap: {self.external_origin}/outside-sitemap.xml\n"
                ).encode()
                self.send_response(200)
            else:
                body = b""
                self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/sitemap.xml":
            if type(self).sitemap_enabled:
                body = f"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
                  <url><loc>{self.server.origin}/sitemap-start/from-urlset?source=xml</loc></url>
                  <url><loc>{self.external_origin}/outside-sitemap-page</loc></url>
                  <url><loc>mailto:skip@example.test</loc></url></urlset>""".encode()
                self.send_response(200)
            else:
                body = b""
                self.send_response(404)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/sitemap-index.xml":
            if type(self).scoped_sitemap_enabled:
                type(self).scoped_metadata_requests += 1
                body = f"""<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
                  <sitemap><loc>{self.server.origin}/sitemap-child.xml</loc></sitemap>
                  </sitemapindex>""".encode()
            else:
                body = f"""<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
                  <sitemap><loc>{self.server.origin}/sitemap-child.xml</loc></sitemap>
                  <sitemap><loc>{self.external_origin}/outside-index.xml</loc></sitemap>
                  </sitemapindex>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/sitemap-child.xml":
            if type(self).scoped_sitemap_enabled:
                body = f"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
                  <url><loc>{self.server.origin}/demo/mars/sitemap-page/?source=xml%20one</loc></url>
                  <url><loc>{self.server.origin}/demo/mars2.html</loc></url>
                  <url><loc>{self.server.origin}/demo/other</loc></url>
                  </urlset>""".encode()
            else:
                body = f"""<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
                  <url><loc>{self.server.origin}/sitemap-start/indexed</loc></url></urlset>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/chain/"):
            index = int(self.path.rsplit("/", 1)[1])
            child = f"<a href='{self.path}/{index + 1}'>Next</a>" if index < 104 else ""
            body = f"<html><body><main>{child}</main></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # The children must be path descendants of the start page, otherwise the
        # scope policy correctly excludes them and the loss is never exercised.
        if self.path in {"/browser-loss", "/browser-loss/"}:
            links = "".join(
                f"<a href='/browser-loss/{index}'>Page {index}</a>"
                for index in range(1, 7)
            )
            body = (
                f"<html><body><main><h1>Browser loss index</h1>{links}"
                "</main></body></html>"
            ).encode()
            self._send_static(body, "text/html; charset=utf-8")
            return
        if self.path.startswith("/browser-loss/"):
            body = f"<html><body><main><h1>{self.path}</h1></main></body></html>".encode()
            self._send_static(body, "text/html; charset=utf-8")
            return
        if self.path == "/oversized-document":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(OVERSIZED_DOCUMENT_BYTES))
            self.end_headers()
            chunk = b"x" * 65_536
            remaining = OVERSIZED_DOCUMENT_BYTES
            try:
                while remaining > 0:
                    block = chunk[: min(remaining, len(chunk))]
                    self.wfile.write(block)
                    remaining -= len(block)
            except OSError:
                return
            return
        if self.path == "/media-page":
            body = b"""<!doctype html><html><body><main>
              <h1>Media page</h1>
              <video autoplay muted playsinline preload='auto' src='/media/sample.mp4'></video>
              <audio autoplay preload='auto' src='/media/sample.wav'></audio>
              <img src='/media/pixel.png' alt='Pixel'>
              <link rel='stylesheet' href='/media/site.css'>
              <script src='/media/app.js'></script>
              </main></body></html>"""
            self._send_static(body, "text/html; charset=utf-8")
            return
        if self.path in {"/media/sample.mp4", "/media/sample.wav"}:
            type(self).media_asset_requests += 1
            self._send_static(b"media-bytes", "application/octet-stream")
            return
        if self.path == "/media/pixel.png":
            type(self).image_asset_requests += 1
            self._send_static(TRANSPARENT_PIXEL, "image/png")
            return
        if self.path == "/media/site.css":
            type(self).style_asset_requests += 1
            self._send_static(b"main{color:#000}", "text/css")
            return
        if self.path == "/media/app.js":
            type(self).script_asset_requests += 1
            self._send_static(b"window.mediaPageLoaded=true;", "text/javascript")
            return
        if self.path == "/graph" or self.path == "/graph?view=one":
            body = b"""<html><body><main>
              <a href='/graph?view=one'>Query route</a>
              <map><area href='/graph/area-target' alt='Area route'></map>
              <div role='link' href='/graph/role-target'>Role link</div>
              <div role='link'>Not a route</div>
              <a href='/graph'>Cycle</a>
              </main></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in {
            "/graph/area-target",
            "/graph/role-target",
            "/sitemap-start",
            "/sitemap-start/indexed",
        } or self.path.startswith("/sitemap-start/from-urlset?"):
            body = b"<html><body><main><h1>Inventory target</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/no-submit":
            body = b"<html><body><main><form method='post' action='/post-target'><button>Submit</button></form></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/missing":
            body = b"""<!doctype html><html lang='en'><body><main>
              <h1>Not found</h1><img src='/missing.png' alt='Missing page illustration'>
              <a href='/missing/child'>Related page</a>
              </main></body></html>"""
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/missing/child":
            body = b"<html><body><main><h1>Found from error page</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/delayed":
            body = b"""<!doctype html><html lang='en'><body><main>
              <script>setTimeout(() => {
                const section = document.createElement('section');
                section.innerHTML = '<a href="/delayed/child">Delayed child</a>';
                document.querySelector('main').append(section);
              }, 100);</script>
              </main></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/delayed/child":
            body = b"<html><body><main><h1>Delayed child</h1></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/redirect-index":
            body = b"""<!doctype html><html><body><main>
              <a href='/redirect-index/child'>Canonical page</a>
              <a href='/redirect-index/redirect-same-origin'>Redirect alias</a>
              </main></body></html>"""
        elif self.path == "/redirect-index/redirect-same-origin":
            self.send_response(302)
            self.send_header("Location", "/redirect-index/child")
            self.end_headers()
            return
        elif self.path == "/redirect-same-origin":
            self.send_response(302)
            self.send_header("Location", "/redirect-same-origin/child")
            self.end_headers()
            return
        elif self.path.startswith("/redirect-off-origin"):
            self.send_response(302)
            self.send_header("Location", self.external_origin + "/outside")
            self.end_headers()
            return
        elif self.path in {"/redirect-index/child", "/redirect-same-origin/child"} or self.path.startswith("/child"):
            body = b"<html><body><main><h1>Child</h1></main></body></html>"
        else:
            body = b"""<!doctype html><html lang='en'><body>
              <header></header><nav><a href='/child#part'>Child</a></nav><main>
              <a href='/child/'>Duplicate child</a><a href='/missing'>Missing</a>
              <a href='https://example.invalid/offsite'>Off origin</a>
              <img src='/image.png'><picture></picture><svg><circle></circle></svg><canvas></canvas>
              <h1>Home</h1><h2>Section</h2><p>Text</p><section></section><article></article><aside></aside>
              <form><fieldset><legend>Details</legend><label>Name<input></label>
                <textarea></textarea><select><option>One</option></select><button>Save</button></fieldset></form>
              <table><caption>Data</caption><tr><th>Value</th></tr></table>
              <ol><li>Item</li></ol><ul><li>Other</li></ul><dl><dt>Term</dt><dd>Definition</dd></dl>
              <video><track kind='captions'></video><audio></audio>
              <iframe srcdoc='<main>Frame</main>'></iframe><object></object><embed>
              <details><summary>More</summary></details>
              <dialog></dialog><progress></progress><meter></meter><time></time>
              <abbr title='Example'>Ex</abbr><mark>Marked</mark>
              <div aria-label='Named' role='region' tabindex='0'></div><div role='search'></div>
              <div contenteditable='true'></div><x-widget></x-widget>
              <div id='shadow-host'></div>
              <script>document.querySelector('#shadow-host').attachShadow({mode:'open'});</script>
              </main><footer></footer></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/post-target":
            type(self).post_target_requests += 1
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format, *_args):
        pass


@contextlib.contextmanager
def context_death_on_navigations(navigation_numbers, *, state=None):
    """Close the crawl context before the given navigation attempts (1-based).

    The real `Page.goto` still runs, so the crawler meets the closed target
    Playwright itself reports -- no fabricated exception and no stubbed
    crawler code on the path under test.
    """
    kill_at = frozenset(navigation_numbers)
    original_goto = Page.goto
    observations = {"navigations": 0, "closed": 0} if state is None else state

    def wrapped_goto(page, *args, **kwargs):
        observations["navigations"] += 1
        if observations["navigations"] in kill_at:
            observations["closed"] += 1
            page.context.close()
        return original_goto(page, *args, **kwargs)

    with patch.object(Page, "goto", wrapped_goto):
        yield observations


class CrawlerCliTests(unittest.TestCase):
    def test_cli_page_and_depth_bounds_are_unset_by_default(self):
        args = build_parser().parse_args(["http://example.test/"])

        self.assertIsNone(args.max_pages)
        self.assertIsNone(args.max_depth)

    def test_depth_flag_sets_the_depth_bound(self):
        args = build_parser().parse_args(["http://example.test/", "--depth", "2"])

        self.assertEqual(args.max_depth, 2)

    def test_max_depth_remains_accepted_as_an_alias(self):
        args = build_parser().parse_args(["http://example.test/", "--max-depth", "3"])

        self.assertEqual(args.max_depth, 3)

    def test_last_depth_spelling_wins_when_both_are_given(self):
        args = build_parser().parse_args(
            ["http://example.test/", "--depth", "5", "--max-depth", "1"]
        )

        self.assertEqual(args.max_depth, 1)

    def test_depth_rejects_negative_values(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                build_parser().parse_args(["http://example.test/", "--depth", "-1"])

    def test_main_writes_json_and_succeeds_with_minimal_stats(self):
        result = {
            "startUrl": "https://example.test/",
            "pages": [{"url": "https://example.test/", "elements": {}}],
            "stats": {
                "discovered": 1,
                "processed": 1,
                "failed": 0,
                "maxPages": 10,
                "maxDepth": 2,
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            output_path = f"{directory}/inventory.json"
            with patch("qaspider.cli.crawl_site", return_value=result):
                exit_code = main(["https://example.test/", "--output", output_path])

            with open(output_path, encoding="utf-8") as output_file:
                written_result = json.load(output_file)

        self.assertEqual(exit_code, 0)
        self.assertEqual(written_result, result)
        self.assertEqual(
            set(written_result["stats"]),
            {"discovered", "processed", "failed", "maxPages", "maxDepth"},
        )

    def test_unexpected_crawl_failure_still_writes_a_minimal_safe_report(self):
        canary = "COOKIE_CANARY_DO_NOT_LEAK"
        failure = PlaywrightError(
            "Target page, context or browser has been closed. "
            f"Call log: Cookie: {canary}; Authorization: BEARER_CANARY_DO_NOT_LEAK"
        )

        with tempfile.TemporaryDirectory() as directory:
            output_path = f"{directory}/inventory.json"
            stderr = io.StringIO()
            with patch("qaspider.cli.crawl_site", side_effect=failure):
                with contextlib.redirect_stderr(stderr):
                    exit_code = main(["https://example.test/", "--output", output_path])

            self.assertTrue(os.path.exists(output_path))
            with open(output_path, encoding="utf-8") as output_file:
                written_result = json.load(output_file)

        self.assertEqual(exit_code, 1)
        self.assertEqual(written_result["pages"], [])
        self.assertEqual(written_result["startUrl"], "https://example.test/")
        self.assertEqual(written_result["stats"]["stopReason"], "crawler-failed")
        self.assertFalse(written_result["stats"]["complete"])
        self.assertEqual(written_result["stats"]["maxBrowserRecoveries"], MAX_BROWSER_RECOVERIES)
        observable = json.dumps(written_result) + stderr.getvalue()
        self.assertNotIn(canary, observable)
        self.assertNotIn("BEARER_CANARY_DO_NOT_LEAK", observable)
        self.assertNotIn("Call log", observable)


class PlaywrightEnvironmentTests(unittest.TestCase):
    def test_system_ca_is_set_before_startup_and_restored_after_close(self):
        observations = []

        class PlaywrightContext:
            def __enter__(self):
                observations.append(("start", os.environ.get("NODE_USE_SYSTEM_CA")))
                return self

            def __exit__(self, *_args):
                observations.append(("close", os.environ.get("NODE_USE_SYSTEM_CA")))

        with patch.dict(os.environ):
            os.environ.pop("NODE_USE_SYSTEM_CA", None)
            with patch("qaspider.crawl.sync_playwright", return_value=PlaywrightContext()):
                with _playwright_driver():
                    self.assertEqual(os.environ.get("NODE_USE_SYSTEM_CA"), "1")

            self.assertNotIn("NODE_USE_SYSTEM_CA", os.environ)

        self.assertEqual(observations, [("start", "1"), ("close", "1")])

    def test_system_ca_preserves_explicit_setting(self):
        observations = []

        class PlaywrightContext:
            def __enter__(self):
                observations.append(("start", os.environ.get("NODE_USE_SYSTEM_CA")))
                return self

            def __exit__(self, *_args):
                observations.append(("close", os.environ.get("NODE_USE_SYSTEM_CA")))

        with patch.dict(os.environ, {"NODE_USE_SYSTEM_CA": "0"}):
            with patch("qaspider.crawl.sync_playwright", return_value=PlaywrightContext()):
                with _playwright_driver():
                    self.assertEqual(os.environ.get("NODE_USE_SYSTEM_CA"), "0")

            self.assertEqual(os.environ.get("NODE_USE_SYSTEM_CA"), "0")

        self.assertEqual(observations, [("start", "0"), ("close", "0")])


class CrawlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.external_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.external_thread = threading.Thread(target=cls.external_server.serve_forever, daemon=True)
        cls.external_thread.start()
        FixtureHandler.external_origin = f"http://127.0.0.1:{cls.external_server.server_port}"

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.server.origin = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.start_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.external_server.shutdown()
        cls.thread.join()
        cls.external_thread.join()

    def test_normalize_http_urls_and_preserve_query(self):
        self.assertEqual(normalize_url("HTTP://Example.COM:80/a#fragment"), "http://example.com/a")
        self.assertEqual(normalize_url("/route?q=one%20two#part", "https://example.com/base"), "https://example.com/route?q=one%20two")
        self.assertIsNone(normalize_url("mailto:test@example.com"))

    def test_redirect_url_normalization_preserves_slash_distinctions(self):
        self.assertEqual(
            normalize_redirect_url("HTTP://Example.COM:80/path/?q=one#fragment"),
            "http://example.com/path/?q=one",
        )
        self.assertNotEqual(
            normalize_redirect_url("http://example.com/path"),
            normalize_redirect_url("http://example.com/path/"),
        )

    def test_same_origin_compares_scheme_host_and_effective_port(self):
        self.assertTrue(same_origin("http://example.com/a", "http://EXAMPLE.com:80/b"))
        self.assertFalse(same_origin("http://example.com", "https://example.com"))
        self.assertFalse(same_origin("http://example.com:8080", "http://example.com"))

    def test_inventory_keys_are_central_and_boolean_only(self):
        inventory = empty_inventory()
        self.assertEqual(tuple(inventory), INVENTORY_KEYS)
        self.assertTrue(set(CANONICAL_INVENTORY_KEYS).issubset(inventory))
        self.assertTrue(all(value is False for value in inventory.values()))

    def test_crawl_scopes_links_records_http_failures_and_respects_limits(self):
        result = crawl_site(self.start_url, max_pages=4, max_depth=2, timeout_ms=5_000)
        pages = result["pages"]
        self.assertEqual(len(pages), 4)
        self.assertEqual(result["stats"]["discovered"], 4)
        self.assertEqual(result["stats"]["processed"], 4)
        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(result["stats"]["maxPages"], 4)
        self.assertEqual(result["stats"]["maxDepth"], 2)
        self.assertFalse(result["stats"]["complete"])
        self.assertFalse(result["stats"]["truncated"])
        self.assertEqual(result["stats"]["pending"], 0)
        failed_page = next(item for item in pages if item["url"].endswith("/missing"))
        self.assertEqual(failed_page, {
            "url": self.start_url.rstrip("/") + "/missing",
            "statusCode": 404,
            "action": "agent-check",
            "error": "HTTP 404",
        })
        self.assertIn(self.start_url.rstrip("/") + "/missing/child", {item["url"] for item in pages})
        home_page = next(page for page in pages if page["url"] == self.start_url)
        self.assertEqual(set(home_page), {"url", "elements"})
        elements = home_page["elements"]
        self.assertEqual(tuple(elements), INVENTORY_KEYS)
        self.assertTrue(set(CANONICAL_INVENTORY_KEYS).issubset(elements))
        self.assertTrue(all(isinstance(value, bool) for page in pages
                            for value in page.get("elements", {}).values()))
        self.assertTrue(all(tuple(page["elements"]) == INVENTORY_KEYS for page in pages
                            if "elements" in page))
        for category in (
            "links", "internalLinks", "externalLinks", "svg", "canvas", "fieldsets",
            "objects", "embeds", "track", "embeddedContent", "header", "nav", "main",
            "footer", "aside", "section", "article", "aria", "roles", "landmarks",
            "paragraphs", "shadowDom",
        ):
            self.assertTrue(elements[category], category)

    def test_non_2xx_html_is_agent_check_and_only_discovers_links(self):
        missing_url = self.start_url.rstrip("/") + "/missing"

        result = crawl_site(missing_url, max_pages=2, max_depth=1, timeout_ms=5_000)

        missing_page = next(page for page in result["pages"] if page["url"] == missing_url)
        self.assertEqual(missing_page, {
            "url": missing_url,
            "statusCode": 404,
            "action": "agent-check",
            "error": "HTTP 404",
        })
        child_url = self.start_url.rstrip("/") + "/missing/child"
        self.assertIn(child_url, {page["url"] for page in result["pages"]})
        self.assertEqual(result["stats"]["failed"], 1)
        self.assertFalse(result["stats"]["complete"])

    def test_transport_failure_has_no_fabricated_inventory(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            unreachable_port = listener.getsockname()[1]

        result = crawl_site(
            f"http://127.0.0.1:{unreachable_port}/unreachable",
            max_pages=1,
            max_depth=0,
            timeout_ms=2_000,
        )

        page = result["pages"][0]
        self.assertEqual(page["url"], f"http://127.0.0.1:{unreachable_port}/unreachable")
        self.assertEqual(page, {
            "url": f"http://127.0.0.1:{unreachable_port}/unreachable",
            "action": "agent-check",
            "error": "Network request failed.",
        })
        self.assertEqual(result["stats"]["failed"], 1)
        serialized_result = json.dumps(result).lower()
        self.assertNotIn("call log", serialized_result)
        self.assertNotIn("cookie", serialized_result)
        self.assertNotIn("authorization", serialized_result)

    def test_transient_connection_drop_is_retried_and_page_is_inventoried(self):
        FixtureHandler.retry_target_requests = 0
        retry_url = self.start_url.rstrip("/") + "/retry-once"

        result = crawl_site(retry_url, max_pages=1, max_depth=0, timeout_ms=5_000)

        self.assertEqual(FixtureHandler.retry_target_requests, 2)
        page = result["pages"][0]
        self.assertEqual(page["url"], retry_url)
        self.assertTrue(page["elements"]["main"])
        self.assertTrue(page["elements"]["headings"])
        self.assertNotIn("error", page)

    def test_playwright_diagnostics_are_sanitized_before_serialization(self):
        error = PlaywrightError(
            "Request failed. Call log: Cookie: COOKIE_CANARY_DO_NOT_LEAK; "
            "Authorization: Bearer AUTHORIZATION_CANARY_DO_NOT_LEAK"
        )
        with patch("qaspider.crawl._fetch_same_origin_response", side_effect=error):
            result = crawl_site(
                self.start_url.rstrip("/") + "/sanitized-error",
                max_pages=1,
                max_depth=0,
                timeout_ms=5_000,
            )

        serialized_result = json.dumps(result)
        self.assertNotIn("COOKIE_CANARY_DO_NOT_LEAK", serialized_result)
        self.assertNotIn("AUTHORIZATION_CANARY_DO_NOT_LEAK", serialized_result)
        self.assertNotIn("Call log", serialized_result)
        self.assertEqual(result["pages"][0], {
            "url": self.start_url.rstrip("/") + "/sanitized-error",
            "action": "agent-check",
            "error": "Browser request failed.",
        })

    def test_page_limit_is_visible_in_minimal_stats(self):
        result = crawl_site(self.start_url, max_pages=2, max_depth=2, timeout_ms=5_000)

        stats = result["stats"]
        self.assertEqual(stats["maxPages"], 2)
        self.assertEqual(stats["processed"], 2)
        self.assertGreater(stats["discovered"], stats["processed"])
        self.assertGreater(stats["pending"], 0)
        self.assertTrue(stats["truncated"])
        self.assertFalse(stats["complete"])

    def test_depth_zero_does_not_discover_children(self):
        result = crawl_site(self.start_url, max_pages=10, max_depth=0, timeout_ms=5_000)
        self.assertEqual(result["stats"]["processed"], 1)
        self.assertGreater(result["stats"]["discovered"], 1)
        self.assertEqual(result["stats"]["depthSuppressed"], result["stats"]["discovered"] - 1)
        self.assertTrue(result["stats"]["truncated"])

    def test_crawl_includes_content_rendered_after_domcontentloaded(self):
        delayed_url = self.start_url.rstrip("/") + "/delayed"
        result = crawl_site(delayed_url, max_pages=1, max_depth=1, timeout_ms=5_000)

        page = result["pages"][0]
        self.assertTrue(page["elements"]["section"])
        self.assertEqual(result["stats"]["discovered"], 2)

    def test_off_origin_redirect_is_blocked_and_recorded(self):
        redirect_url = self.start_url.rstrip("/") + "/redirect-off-origin"
        with FixtureHandler.external_target_lock:
            FixtureHandler.external_target_requests = 0

        result = crawl_site(redirect_url, max_pages=1, max_depth=0, timeout_ms=5_000)

        page = result["pages"][0]
        self.assertEqual(page, {
            "url": redirect_url,
            "statusCode": 302,
            "action": "agent-check",
            "error": "off-origin redirect was blocked.",
        })
        with FixtureHandler.external_target_lock:
            self.assertEqual(FixtureHandler.external_target_requests, 0)

    def test_same_origin_redirect_is_followed_and_resolved(self):
        redirect_url = self.start_url.rstrip("/") + "/redirect-same-origin"

        result = crawl_site(redirect_url, max_pages=1, max_depth=0, timeout_ms=5_000)

        page = result["pages"][0]
        self.assertEqual(
            page["url"], self.start_url.rstrip("/") + "/redirect-same-origin/child"
        )
        self.assertNotIn("statusCode", page)
        self.assertNotIn("finalUrl", page)
        self.assertTrue(page["elements"]["headings"])

    def test_start_path_is_a_segment_safe_scope_with_or_without_trailing_slash(self):
        expected_base = self.start_url.rstrip("/") + "/demo/mars"
        expected_child = expected_base + "/child?view=one"

        for start_path in ("/demo/mars/", "/demo/mars"):
            with self.subTest(start_path=start_path):
                FixtureHandler.scoped_outside_requests = 0
                result = crawl_site(
                    self.start_url.rstrip("/") + start_path, timeout_ms=5_000
                )

                urls = {record["url"] for record in result["pages"]}
                self.assertIn(expected_base, urls)
                self.assertIn(expected_child, urls)
                self.assertEqual(urls, {expected_base, expected_child})
                self.assertEqual(result["stats"]["discovered"], 2)
                self.assertEqual(FixtureHandler.scoped_outside_requests, 0)

    def test_trailing_slash_base_resolves_relative_mars_pages_inside_scope(self):
        FixtureHandler.relative_links_enabled = True
        FixtureHandler.scoped_outside_requests = 0
        try:
            result = crawl_site(
                self.start_url.rstrip("/") + "/demo/mars/", timeout_ms=5_000
            )
        finally:
            FixtureHandler.relative_links_enabled = False

        expected_urls = {
            self.start_url.rstrip("/") + "/demo/mars",
            *(
                self.start_url.rstrip("/")
                + f"/demo/mars/mars2.html?a={query}"
                for query in (
                    "send_me_to_mars",
                    "hotels",
                    "things_to_do",
                    "mars_map",
                    "sign_in",
                    "cart",
                )
            ),
        }
        pages = {page["url"]: page for page in result["pages"]}

        self.assertEqual(set(pages), expected_urls)
        self.assertEqual(result["stats"]["discovered"], 7)
        for url in expected_urls - {self.start_url.rstrip("/") + "/demo/mars"}:
            self.assertTrue(pages[url]["elements"]["main"], url)
            self.assertTrue(pages[url]["elements"]["headings"], url)
            self.assertNotIn("action", pages[url], url)
        self.assertEqual(FixtureHandler.scoped_outside_requests, 0)

    def test_slashless_base_excludes_the_same_relative_sibling_links(self):
        FixtureHandler.relative_links_enabled = True
        FixtureHandler.scoped_outside_requests = 0
        try:
            result = crawl_site(
                self.start_url.rstrip("/") + "/demo/mars", timeout_ms=5_000
            )
        finally:
            FixtureHandler.relative_links_enabled = False

        base_url = self.start_url.rstrip("/") + "/demo/mars"
        self.assertEqual([page["url"] for page in result["pages"]], [base_url])
        self.assertEqual(result["stats"]["discovered"], 1)
        self.assertEqual(FixtureHandler.scoped_outside_requests, 0)

    def test_sitemap_index_metadata_can_be_outside_scope_but_pages_cannot(self):
        FixtureHandler.scoped_sitemap_enabled = True
        FixtureHandler.scoped_metadata_requests = 0
        FixtureHandler.scoped_outside_requests = 0
        try:
            result = crawl_site(
                self.start_url.rstrip("/") + "/demo/mars/", timeout_ms=5_000
            )
        finally:
            FixtureHandler.scoped_sitemap_enabled = False

        urls = {record["url"] for record in result["pages"]}
        self.assertIn(
            self.start_url.rstrip("/") + "/demo/mars/child?view=one", urls
        )
        self.assertIn(
            self.start_url.rstrip("/") + "/demo/mars/sitemap-page?source=xml%20one",
            urls,
        )
        self.assertFalse(
            any(url.endswith(("/demo/mars2.html", "/demo/other")) for url in urls)
        )
        self.assertEqual(result["stats"]["discovered"], 3)
        self.assertGreater(FixtureHandler.scoped_metadata_requests, 0)
        self.assertEqual(FixtureHandler.scoped_outside_requests, 0)

    def test_same_origin_redirect_outside_path_is_blocked_before_target_request(self):
        redirect_url = self.start_url.rstrip("/") + "/demo/mars/redirect-outside"
        FixtureHandler.scoped_outside_requests = 0

        result = crawl_site(redirect_url, max_pages=1, max_depth=0, timeout_ms=5_000)

        self.assertEqual(result["pages"][0], {
            "url": redirect_url,
            "statusCode": 302,
            "action": "agent-check",
            "error": "Redirect outside the crawl path was blocked.",
        })
        self.assertEqual(FixtureHandler.scoped_outside_requests, 0)

    def test_trailing_slash_redirect_is_inventoried_under_canonical_url(self):
        redirect_url = self.start_url.rstrip("/") + "/slash-target"

        result = crawl_site(redirect_url, max_pages=1, max_depth=0, timeout_ms=5_000)

        page = result["pages"][0]
        self.assertEqual(page["url"], redirect_url)
        self.assertNotIn("action", page)
        self.assertNotIn("error", page)
        self.assertNotIn("statusCode", page)
        self.assertTrue(page["elements"]["main"])
        self.assertTrue(page["elements"]["headings"])
        self.assertEqual(result["stats"]["failed"], 0)

    def test_actual_redirect_cycle_is_blocked(self):
        redirect_url = self.start_url.rstrip("/") + "/redirect-loop-a"

        result = crawl_site(redirect_url, max_pages=1, max_depth=0, timeout_ms=5_000)

        self.assertEqual(result["pages"][0], {
            "url": redirect_url,
            "statusCode": 301,
            "action": "agent-check",
            "error": "Redirect loop was blocked.",
        })
        self.assertEqual(result["stats"]["failed"], 1)

    def test_redirect_alias_does_not_duplicate_inventoried_canonical_page(self):
        result = crawl_site(
            self.start_url.rstrip("/") + "/redirect-index",
            max_pages=10,
            max_depth=1,
            timeout_ms=5_000,
        )

        urls = [page["url"] for page in result["pages"]]
        canonical_url = self.start_url.rstrip("/") + "/redirect-index/child"
        self.assertEqual(urls.count(canonical_url), 1)
        self.assertNotIn(
            self.start_url.rstrip("/") + "/redirect-index/redirect-same-origin", urls
        )

    def test_default_limits_exhaust_a_finite_chain_longer_than_one_hundred_pages(self):
        chain_url = self.start_url.rstrip("/") + "/chain/0"
        with patch.object(Page, "wait_for_load_state", return_value=None):
            result = crawl_site(chain_url, timeout_ms=5_000)

        self.assertEqual(result["stats"]["maxPages"], None)
        self.assertEqual(result["stats"]["maxDepth"], None)
        self.assertEqual(result["stats"]["processed"], 105)
        self.assertEqual(result["stats"]["pending"], 0)
        self.assertTrue(result["stats"]["complete"])
        self.assertFalse(result["stats"]["truncated"])

    def test_query_cycles_area_links_and_role_links_are_discovered_once(self):
        result = crawl_site(self.start_url.rstrip("/") + "/graph", timeout_ms=5_000)

        urls = {record["url"] for record in result["pages"]}
        self.assertEqual(len(urls), 4)
        self.assertIn(self.start_url.rstrip("/") + "/graph?view=one", urls)
        self.assertIn(self.start_url.rstrip("/") + "/graph/area-target", urls)
        self.assertIn(self.start_url.rstrip("/") + "/graph/role-target", urls)
        self.assertTrue(next(page for page in result["pages"] if page["url"].endswith("/graph"))["elements"]["links"])
        self.assertEqual(result["stats"]["pending"], 0)
        self.assertTrue(result["stats"]["complete"])

    def test_robots_and_sitemap_indexes_discover_only_same_origin_locations(self):
        FixtureHandler.sitemap_enabled = True
        try:
            result = crawl_site(self.start_url.rstrip("/") + "/sitemap-start", timeout_ms=5_000)
        finally:
            FixtureHandler.sitemap_enabled = False

        urls = {record["url"] for record in result["pages"]}
        self.assertIn(
            self.start_url.rstrip("/") + "/sitemap-start/from-urlset?source=xml", urls
        )
        self.assertIn(self.start_url.rstrip("/") + "/sitemap-start/indexed", urls)
        self.assertFalse(any("outside" in url for url in urls))
        self.assertEqual(result["stats"]["issues"], [])
        self.assertTrue(result["stats"]["complete"])

    def test_robots_and_sitemap_redirects_preserve_trailing_slashes(self):
        FixtureHandler.auxiliary_redirect_enabled = True
        try:
            result = crawl_site(self.start_url, max_pages=2, max_depth=1, timeout_ms=5_000)
        finally:
            FixtureHandler.auxiliary_redirect_enabled = False

        pages = {record["url"]: record for record in result["pages"]}
        auxiliary_page_url = self.start_url.rstrip("/") + "/aux-redirect-page"
        self.assertIn(auxiliary_page_url, pages)
        self.assertTrue(pages[auxiliary_page_url]["elements"]["main"])
        self.assertEqual(result["stats"]["issues"], [])

    def test_crawler_does_not_submit_forms_or_interact_with_controls(self):
        FixtureHandler.post_target_requests = 0

        result = crawl_site(self.start_url.rstrip("/") + "/no-submit", timeout_ms=5_000)

        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(FixtureHandler.post_target_requests, 0)

    def test_browser_loss_keeps_inventoried_pages_and_resumes_the_queue(self):
        base = self.start_url.rstrip("/")
        deaths = {"navigations": 0, "closed": 0}

        with context_death_on_navigations({2}, state=deaths):
            result = crawl_site(base + "/browser-loss/", timeout_ms=15_000)

        # The fixture must have really killed the target, otherwise this test
        # would pass without exercising any loss at all.
        self.assertEqual(deaths["closed"], 1)
        stats = result["stats"]
        self.assertEqual(stats["browserLosses"], 1)
        self.assertEqual(stats["browserRecoveries"], 1)
        self.assertIsNone(stats["stopReason"])
        self.assertEqual(stats["teardownFailures"], 1)

        pages = {page["url"]: page for page in result["pages"]}
        self.assertEqual(len(pages), len(result["pages"]), "a page was inventoried twice")
        self.assertTrue(pages[base + "/browser-loss"]["elements"]["headings"])
        # The document answered 200 before the target died, so the status is
        # kept and the page is an agent-check instead of a lost result.
        self.assertEqual(pages[base + "/browser-loss/1"], {
            "url": base + "/browser-loss/1",
            "statusCode": 200,
            "action": "agent-check",
            "error": "Browser closed unexpectedly.",
        })
        for index in (2, 3, 4, 5, 6):
            page = pages[base + f"/browser-loss/{index}"]
            self.assertTrue(page["elements"]["headings"], index)
            self.assertNotIn("action", page, index)
            self.assertNotIn("error", page, index)

        self.assertEqual(stats["processed"], 7)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertFalse(stats["truncated"])
        self.assertFalse(stats["complete"])

    def test_browser_recovery_budget_is_bounded_and_reports_the_stop(self):
        deaths = {"navigations": 0, "closed": 0}

        with context_death_on_navigations({2, 3, 4, 5}, state=deaths):
            result = crawl_site(
                self.start_url.rstrip("/") + "/browser-loss/", timeout_ms=15_000
            )

        self.assertEqual(deaths["closed"], MAX_BROWSER_RECOVERIES + 1)
        stats = result["stats"]
        self.assertEqual(stats["maxBrowserRecoveries"], MAX_BROWSER_RECOVERIES)
        self.assertEqual(stats["browserRecoveries"], MAX_BROWSER_RECOVERIES)
        self.assertEqual(stats["browserLosses"], MAX_BROWSER_RECOVERIES + 1)
        self.assertEqual(stats["stopReason"], STOP_BROWSER_RECOVERY_BUDGET)
        self.assertTrue(stats["truncated"])
        self.assertFalse(stats["complete"])
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["processed"], 5)
        # The start page was inventoried before the browser died and is still
        # in the report.
        self.assertIn(
            self.start_url.rstrip("/") + "/browser-loss",
            {page["url"] for page in result["pages"]},
        )

    def test_oversized_document_is_refused_instead_of_buffered(self):
        oversized_url = self.start_url.rstrip("/") + "/oversized-document"

        result = crawl_site(oversized_url, max_pages=1, max_depth=0, timeout_ms=30_000)

        self.assertEqual(result["pages"], [{
            "url": oversized_url,
            "action": "agent-check",
            "error": "Document exceeded the size limit.",
        }])
        self.assertEqual(result["stats"]["failed"], 1)
        self.assertFalse(result["stats"]["complete"])

    def test_media_is_blocked_while_the_rest_of_the_page_still_loads(self):
        FixtureHandler.media_asset_requests = 0
        FixtureHandler.image_asset_requests = 0
        FixtureHandler.style_asset_requests = 0
        FixtureHandler.script_asset_requests = 0

        result = crawl_site(
            self.start_url.rstrip("/") + "/media-page", max_pages=1, max_depth=0,
            timeout_ms=15_000,
        )

        self.assertEqual(FixtureHandler.media_asset_requests, 0)
        # Interception is proven to be active and selective: the other resource
        # types still reached the fixture.
        self.assertGreaterEqual(FixtureHandler.image_asset_requests, 1)
        self.assertGreaterEqual(FixtureHandler.style_asset_requests, 1)
        self.assertGreaterEqual(FixtureHandler.script_asset_requests, 1)
        page = result["pages"][0]
        self.assertNotIn("action", page)
        self.assertTrue(page["elements"]["video"])
        self.assertTrue(page["elements"]["audio"])
        self.assertTrue(page["elements"]["images"])


class NestedTreeHandler(BaseHTTPRequestHandler):
    """Serves a fixed nesting chain: / > /n1 > /n1/n2 > /n1/n2/n3 > /n1/n2/n3/n4.

    Child links are absolute and the parent-to-children map is explicit on
    purpose: relative hrefs would make the depth semantics depend on trailing
    slashes, and deriving children from slash counts invites off-by-one errors.
    """

    CHILDREN = {
        "/": ["/n1"],
        "/n1": ["/n1/n2"],
        "/n1/n2": ["/n1/n2/n3"],
        "/n1/n2/n3": ["/n1/n2/n3/n4"],
        "/n1/n2/n3/n4": [],
    }

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/robots.txt" or path == "/sitemap.xml":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in self.CHILDREN:
            links = "".join(
                f'<a href="{child}">link</a>' for child in self.CHILDREN[path]
            )
            body = (
                f"<html><head><title>depth</title></head>"
                f"<body><main><h1>{path}</h1>{links}</main></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class NestingDepthTests(unittest.TestCase):
    """`--depth N` means N path segments below the start URL, not from the root."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), NestedTreeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def _crawled_paths(self, start_path, **kwargs):
        result = crawl_site(f"{self.origin}{start_path}", timeout_ms=15_000, **kwargs)
        paths = sorted(
            page["url"][len(self.origin) :] or "/" for page in result["pages"]
        )
        return paths, result["stats"]

    def test_depth_counts_segments_below_the_start_url(self):
        cases = {
            0: ["/"],
            1: ["/", "/n1"],
            2: ["/", "/n1", "/n1/n2"],
            3: ["/", "/n1", "/n1/n2", "/n1/n2/n3"],
            4: ["/", "/n1", "/n1/n2", "/n1/n2/n3", "/n1/n2/n3/n4"],
        }
        for depth, expected in cases.items():
            with self.subTest(depth=depth):
                paths, _ = self._crawled_paths("/", max_depth=depth)
                self.assertEqual(paths, expected)

    def test_depth_is_relative_to_a_base_url_that_itself_has_a_path(self):
        start = "/n1"

        paths, _ = self._crawled_paths(start, max_depth=1)
        self.assertEqual(paths, ["/n1", "/n1/n2"])

        paths, _ = self._crawled_paths(start, max_depth=2)
        self.assertEqual(paths, ["/n1", "/n1/n2", "/n1/n2/n3"])

    def test_omitting_depth_follows_every_discovered_link(self):
        paths, stats = self._crawled_paths("/")

        self.assertEqual(
            paths, ["/", "/n1", "/n1/n2", "/n1/n2/n3", "/n1/n2/n3/n4"]
        )
        self.assertIsNone(stats["maxDepth"])
        self.assertEqual(stats["depthSuppressed"], 0)
        self.assertTrue(stats["complete"])
        self.assertFalse(stats["truncated"])

    def test_depth_limit_reports_suppressed_and_truncated_state(self):
        paths, stats = self._crawled_paths("/", max_depth=2)

        self.assertEqual(paths, ["/", "/n1", "/n1/n2"])
        self.assertEqual(stats["maxDepth"], 2)
        self.assertEqual(stats["pending"], 0)
        # Suppression is a frontier, not a whole subtree: /n1/n2/n3 is rejected at
        # depth 3, so it is never crawled and its own child /n1/n2/n3/n4 is never
        # discovered at all. Only the boundary counts as suppressed.
        self.assertEqual(stats["discovered"], 4)
        self.assertEqual(stats["depthSuppressed"], 1)
        self.assertTrue(stats["truncated"])
        self.assertFalse(stats["complete"])


if __name__ == "__main__":
    unittest.main()
