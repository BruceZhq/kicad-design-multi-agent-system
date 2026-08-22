"""Failure-tolerant web research tools for the RatsNestPro agents."""

import io
import ipaddress
import json
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from ddgs import DDGS
from langchain_core.tools import tool
from pypdf import PdfReader

logger = logging.getLogger(__name__)

_SEARCH_BACKENDS = "duckduckgo,brave,google,mojeek"
_ST_DOCUMENT_ID = re.compile(r"\bdm\d{8,}\b", re.IGNORECASE)
_MAX_PDF_BYTES = 25 * 1024 * 1024
_MAX_PROXY_TEXT_BYTES = 8 * 1024 * 1024
_MAX_EXCERPT_CHARS = 8_000
_PDF_TEXT_PROXY_PREFIX = "https://r.jina.ai/"
_PROXY_PAGE_COUNT = re.compile(r"Number of Pages:\s*(\d{1,4})", re.IGNORECASE)
_PROXY_PAGE_MARKER = re.compile(r"(?<!\d)(\d{1,4})/(\d{1,4})(?!\d)")
_QUERY_STOP_WORDS = {
    "and",
    "datasheet",
    "exact",
    "for",
    "from",
    "official",
    "page",
    "pdf",
    "pin",
    "pins",
    "definition",
    "definitions",
    "the",
    "with",
}


def _st_official_results(query: str) -> list[dict[str, str]]:
    """Recover canonical ST document URLs when the query contains an ST document ID."""
    if "st.com" not in query.lower() and "stmicroelectronics" not in query.lower():
        return []

    return [
        {
            "title": f"STMicroelectronics official document {document_id.upper()}",
            "href": (
                "https://www.st.com/resource/en/datasheet/"
                f"{document_id.lower()}.pdf"
            ),
            "body": (
                "Canonical STMicroelectronics datasheet URL reconstructed from the "
                f"document identifier {document_id.upper()} in the query."
            ),
        }
        for document_id in dict.fromkeys(_ST_DOCUMENT_ID.findall(query))
    ]


def _normalise_result(result: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(result.get("title", "")),
        "href": str(result.get("href") or result.get("url") or ""),
        "body": str(result.get("body") or result.get("description") or ""),
    }


def _merge_results(
    official_results: list[dict[str, str]],
    search_results: list[dict[str, Any]],
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for result in [*official_results, *map(_normalise_result, search_results)]:
        url = result["href"].strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(result)
    return merged


def _provider_search(query: str) -> list[dict[str, Any]]:
    return DDGS(timeout=10).text(
        query,
        backend=_SEARCH_BACKENDS,
        max_results=6,
    )


def _validate_public_https_url(
    url: str,
    *,
    allow_mixed_dns: bool = False,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only public HTTPS URLs are supported.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not supported.")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".local", ".internal")):
        raise ValueError("Local network URLs are not supported.")

    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        addresses = [
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        ]
    global_addresses = [address for address in addresses if address.is_global]
    if (
        not global_addresses
        or (not allow_mixed_dns and len(global_addresses) != len(addresses))
    ):
        raise ValueError("Private or non-routable network URLs are not supported.")


def _download_pdf(url: str) -> tuple[bytes, str]:
    def validate_request(request: httpx.Request) -> None:
        _validate_public_https_url(str(request.url))

    headers = {"User-Agent": "RatsNestPro/0.1 datasheet reader"}
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(25, connect=10),
        headers=headers,
        event_hooks={"request": [validate_request]},
    ) as client, client.stream("GET", url) as response:
        response.raise_for_status()
        content_length = int(response.headers.get("content-length", "0") or 0)
        if content_length > _MAX_PDF_BYTES:
            raise ValueError("The PDF exceeds the 25 MiB download limit.")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_PDF_BYTES:
                raise ValueError("The PDF exceeds the 25 MiB download limit.")
        final_url = str(response.url)

    pdf_bytes = bytes(content)
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("The URL did not return a PDF document.")
    return pdf_bytes, final_url


def _download_proxy_text(url: str) -> tuple[str, str]:
    """Fetch a bounded text rendering while retaining the official source URL."""
    _validate_public_https_url(url)
    proxy_url = f"{_PDF_TEXT_PROXY_PREFIX}{url}"
    # r.jina.ai currently publishes a normal global IPv4 address together with
    # a Teredo-range IPv6 address that Python classifies as non-global. The
    # hostname is fixed by this module, so require at least one global address
    # without weakening validation for the caller-provided source URL.
    _validate_public_https_url(proxy_url, allow_mixed_dns=True)
    headers = {
        "Accept": "text/plain",
        "User-Agent": "RatsNestPro/0.1 datasheet reader",
    }
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(60, connect=10),
        headers=headers,
    ) as client, client.stream("GET", proxy_url) as response:
        response.raise_for_status()
        content_length = int(response.headers.get("content-length", "0") or 0)
        if content_length > _MAX_PROXY_TEXT_BYTES:
            raise ValueError("The datasheet text exceeds the 8 MiB limit.")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_PROXY_TEXT_BYTES:
                raise ValueError("The datasheet text exceeds the 8 MiB limit.")

    text = bytes(content).decode("utf-8", errors="replace")
    if not text.strip():
        raise ValueError("The datasheet text proxy returned an empty response.")
    return text, proxy_url


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-z0-9][a-z0-9_+-]{2,}", query.lower())
    return list(dict.fromkeys(term for term in terms if term not in _QUERY_STOP_WORDS))


def _page_score(text: str, terms: list[str]) -> int:
    lower_text = text.lower()
    score = 0
    matched_terms = 0
    for term in terms:
        count = min(lower_text.count(term), 5)
        if not count:
            continue
        matched_terms += 1
        weight = 10 if any(character.isdigit() for character in term) else 4
        score += count * weight

    if terms and matched_terms == len(terms):
        score += 20
    if re.search(r"figure[^\n]{0,100}lqfp64[^\n]{0,50}pinout", lower_text):
        score += 60
    if "list of figures" in lower_text:
        score -= 200
    if "revision history" in lower_text:
        score -= 200
    return score


def _proxy_sections(text: str) -> tuple[int | None, list[tuple[int | None, str]]]:
    """Split a converted PDF into page-like sections using its printed markers."""
    page_count_match = _PROXY_PAGE_COUNT.search(text)
    document_pages = (
        int(page_count_match.group(1)) if page_count_match else None
    )
    body = text.partition("Markdown Content:")[2] or text
    markers = [
        match
        for match in _PROXY_PAGE_MARKER.finditer(body)
        if (
            1 <= int(match.group(1)) <= int(match.group(2))
            and (
                document_pages is None
                or int(match.group(2)) == document_pages
            )
        )
    ]
    if not markers:
        chunk_size = 12_000
        return document_pages, [
            (None, body[start:start + chunk_size])
            for start in range(0, len(body), chunk_size)
        ]

    page_text: dict[int, list[str]] = {}
    current_page = int(markers[0].group(1))
    cursor = 0
    for marker in markers:
        segment = body[cursor:marker.start()]
        if segment.strip():
            page_text.setdefault(current_page, []).append(segment)
        current_page = int(marker.group(1))
        cursor = marker.end()
    if body[cursor:].strip():
        page_text.setdefault(current_page, []).append(body[cursor:])
    return document_pages, [
        (page, "\n".join(segments))
        for page, segments in sorted(page_text.items())
    ]


def _read_datasheet_via_text_proxy(
    url: str,
    query: str,
    max_pages: int,
) -> dict[str, Any]:
    text, proxy_url = _download_proxy_text(url)
    document_pages, sections = _proxy_sections(text)
    terms = _query_terms(query)
    ranked_pages = [
        (_page_score(section, terms), page, section)
        for page, section in sections
    ]
    ranked_pages = [item for item in ranked_pages if item[0] > 0]
    ranked_pages.sort(
        key=lambda item: (
            -item[0],
            item[1] if item[1] is not None else 10_000,
        )
    )
    selected = ranked_pages[:max_pages]
    return {
        "status": "partial" if selected else "no_matches",
        "source_url": url,
        "retrieval_url": proxy_url,
        "retrieval_method": "official_document_text_proxy",
        "document_pages": document_pages,
        "query": query,
        "matched_pages": [
            {
                "page": page_number,
                "score": score,
                "text": section[:_MAX_EXCERPT_CHARS],
            }
            for score, page_number, section in selected
        ],
        "message": (
            "Direct PDF retrieval failed, so text was extracted through a bounded "
            "proxy from the exact official source URL. Treat image-only content as "
            "unverified and keep the official source URL in all citations."
        ),
    }


def _read_datasheet(url: str, query: str, max_pages: int) -> dict[str, Any]:
    pdf_bytes, final_url = _download_pdf(url)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    terms = _query_terms(query)
    ranked_pages: list[tuple[int, int, str]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        score = _page_score(text, terms)
        if score > 0:
            ranked_pages.append((score, page_number, text))

    ranked_pages.sort(key=lambda item: (-item[0], item[1]))
    selected = ranked_pages[:max_pages]
    return {
        "status": "ok" if selected else "no_matches",
        "source_url": final_url,
        "document_pages": len(reader.pages),
        "query": query,
        "matched_pages": [
            {
                "page": page_number,
                "score": score,
                "text": text[:_MAX_EXCERPT_CHARS],
            }
            for score, page_number, text in selected
        ],
        "message": (
            "Pin claims must be based on text in matched_pages. If a pinout is an "
            "image or a needed pin is absent, report that it was not extracted."
        ),
    }


def _search_web(query: str) -> str:
    """Run multi-provider search without allowing provider failures to abort the graph."""
    official_results = _st_official_results(query)
    try:
        search_results = _provider_search(query)
    except Exception as exc:  # External providers can fail independently of the agent.
        logger.warning("RatsNestPro web search failed for %r: %s", query, exc)
        status = "partial" if official_results else "temporarily_unavailable"
        return json.dumps(
            {
                "status": status,
                "query": query,
                "results": official_results,
                "message": (
                    "The external search providers are temporarily unavailable. "
                    "Continue with any official URL returned here, otherwise report "
                    "the research step as unavailable instead of aborting the task."
                ),
            },
            ensure_ascii=False,
        )

    results = _merge_results(official_results, search_results)
    return json.dumps(
        {
            "status": "ok" if results else "no_results",
            "query": query,
            "results": results,
        },
        ensure_ascii=False,
    )


@tool("web_search")
def web_search(query: str) -> str:
    """Search multiple web providers for official datasheets and reference designs."""
    return _search_web(query)


@tool("fetch_datasheet")
def fetch_datasheet(url: str, query: str, max_pages: int = 5) -> str:
    """Read matching pages from a public HTTPS PDF datasheet after web_search."""
    bounded_max_pages = max(1, min(max_pages, 8))
    try:
        result = _read_datasheet(url, query, bounded_max_pages)
    except Exception as direct_exc:
        logger.warning(
            "RatsNestPro direct datasheet fetch failed for %r: %s",
            url,
            direct_exc,
        )
        try:
            result = _read_datasheet_via_text_proxy(
                url,
                query,
                bounded_max_pages,
            )
            result["direct_fetch_error"] = (
                f"{type(direct_exc).__name__}: {direct_exc}"
            )
        except Exception as proxy_exc:
            logger.warning(
                "RatsNestPro datasheet text fallback failed for %r: %s",
                url,
                proxy_exc,
            )
            result = {
                "status": "temporarily_unavailable",
                "source_url": url,
                "query": query,
                "matched_pages": [],
                "message": (
                    "Neither the direct PDF nor its bounded text fallback could be "
                    "read. Report exact-source extraction as unavailable and return "
                    "control normally; do not infer pin facts."
                ),
                "direct_fetch_error": (
                    f"{type(direct_exc).__name__}: {direct_exc}"
                ),
                "fallback_error": f"{type(proxy_exc).__name__}: {proxy_exc}",
            }
    return json.dumps(result, ensure_ascii=False)
