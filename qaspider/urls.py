"""URL normalization, origin checks, and crawl-path scope helpers."""

from __future__ import annotations

from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit


def _origin(parts: SplitResult) -> tuple[str, str, int | None]:
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError(f"Invalid URL port: {error}") from error
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    return scheme, hostname, port


def _normalize_http_url(
    value: str, base: str | None, *, preserve_trailing_slash: bool
) -> str | None:
    candidate = urljoin(base, value) if base else value
    try:
        parts = urlsplit(candidate)
        origin = _origin(parts)
    except ValueError as error:
        raise ValueError(f"Invalid URL: {value!r}") from error

    scheme, hostname, port = origin
    if scheme not in {"http", "https"} or not hostname:
        return None

    path = parts.path if preserve_trailing_slash else parts.path.rstrip("/")
    path = path or "/"
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    if parts.username is not None:
        credentials = parts.username
        if parts.password is not None:
            credentials += f":{parts.password}"
        netloc = f"{credentials}@{netloc}"

    return urlunsplit((scheme, netloc, path, parts.query, ""))


def normalize_url(value: str, base: str | None = None) -> str | None:
    """Return a canonical HTTP(S) URL, or None for unsupported protocols."""
    return _normalize_http_url(value, base, preserve_trailing_slash=False)


def normalize_redirect_url(value: str, base: str | None = None) -> str | None:
    """Normalize a request URL without collapsing trailing slashes."""
    return _normalize_http_url(value, base, preserve_trailing_slash=True)


def path_scope_from_url(value: str) -> str:
    """Return the canonical path floor for a crawl's original start URL."""
    try:
        path = urlsplit(value).path
    except ValueError as error:
        raise ValueError(f"Invalid URL: {value!r}") from error
    return path.rstrip("/") or "/"


def path_is_in_scope(value: str, path_scope: str) -> bool:
    """Check whether a URL path is the crawl floor or a segment descendant."""
    try:
        path = urlsplit(value).path.rstrip("/") or "/"
    except ValueError:
        return False
    if path_scope == "/":
        return True
    return path == path_scope or path.startswith(f"{path_scope}/")


def same_origin(left: str, right: str) -> bool:
    """Compare effective HTTP(S) origins, including normalized default ports."""
    try:
        left_parts = urlsplit(left)
        right_parts = urlsplit(right)
        left_origin = _origin(left_parts)
        right_origin = _origin(right_parts)
    except ValueError:
        return False
    return left_origin == right_origin and left_origin[0] in {"http", "https"} and bool(left_origin[1])
