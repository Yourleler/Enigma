"""Small zero-config web search backend for the demo web_search tool."""

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def parse_domain_list(value):
    return {
        item.strip().lower()
        for item in str(value or "").split(",")
        if item.strip()
    }


def result_domain(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def filter_results(results, allowed_domains=None, blocked_domains=None):
    allowed = set(allowed_domains or ())
    blocked = set(blocked_domains or ())
    filtered = []
    for result in results:
        domain = result_domain(result.url)
        if allowed and domain not in allowed:
            continue
        if blocked and domain in blocked:
            continue
        filtered.append(result)
    return filtered


class SearchClient:
    def search(self, query, max_results, allowed_domains=None, blocked_domains=None):
        raise NotImplementedError


class DuckDuckGoHTMLSearchClient(SearchClient):
    endpoint = "https://html.duckduckgo.com/html/"
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )

    def search(self, query, max_results, allowed_domains=None, blocked_domains=None):
        url = f"{self.endpoint}?q={quote_plus(query)}"
        request = Request(url, headers={"User-Agent": self.user_agent})
        with urlopen(request, timeout=8) as response:
            html = response.read().decode("utf-8", errors="replace")
        parser = _DuckDuckGoHTMLParser()
        parser.feed(html)
        results = filter_results(parser.results, allowed_domains, blocked_domains)
        return results[:max_results]


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._current = None
        self._capture = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(str(attrs.get("class", "")).split())
        if tag == "a" and "result__a" in classes:
            self._current = SearchResult(title="", url=_clean_duckduckgo_url(attrs.get("href", "")), snippet="")
            self._capture = "title"
            self._parts = []
        elif self._current and tag in {"a", "div"} and "result__snippet" in classes:
            self._capture = "snippet"
            self._parts = []

    def handle_data(self, data):
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if not self._current or not self._capture:
            return
        if self._capture == "title" and tag == "a":
            title = _clean_text(" ".join(self._parts))
            self._current = SearchResult(title=title, url=self._current.url, snippet=self._current.snippet)
            self._capture = None
            self._parts = []
            if self._current.url:
                self.results.append(self._current)
        elif self._capture == "snippet" and tag in {"a", "div"}:
            snippet = _clean_text(" ".join(self._parts))
            if self.results and self.results[-1] == self._current:
                self.results[-1] = SearchResult(
                    title=self._current.title,
                    url=self._current.url,
                    snippet=snippet,
                )
                self._current = self.results[-1]
            self._capture = None
            self._parts = []


def _clean_text(value):
    return " ".join(unescape(value).split())


def _clean_duckduckgo_url(value):
    url = unescape(str(value or "").strip())
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else url
    return url


default_search_client = DuckDuckGoHTMLSearchClient()
