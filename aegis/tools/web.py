"""The web: opening sites for the user, reading them directly, and researching a topic from real sources."""
from __future__ import annotations

import base64
import html as html_lib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from aegis import netguard
from aegis.toolkit import WRITE, Approval, Result, ToolError, arg, tool

logger = logging.getLogger("aegis.web")

_SITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com", "github": "https://github.com", "whatsapp": "https://web.whatsapp.com",
    "drive": "https://drive.google.com", "google drive": "https://drive.google.com", "maps": "https://maps.google.com", "calendar": "https://calendar.google.com", "outlook": "https://outlook.office.com",
    "reddit": "https://www.reddit.com", "linkedin": "https://www.linkedin.com", "x": "https://x.com", "twitter": "https://x.com", "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
    "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.in", "chatgpt": "https://chatgpt.com", "wikipedia": "https://www.wikipedia.org",
}
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_WIKI_AGENT = "Aegis/2 (personal desktop assistant)"
_RECENT = re.compile(r"\b(?:latest|today|tonight|yesterday|tomorrow|current|currently|now|news|price|prices|weather|score|scores|this (?:week|month|year)|recent|recently|20[2-9]\d)\b", re.IGNORECASE)
_NOISE = re.compile(r"\b(?:on|from|in|using|via)\s+wikipedia\b|\bwikipedia\b|\b(?:get|find|give|fetch|look up|search for|basic|general|some|the)\s+(?:basic |general |some )?(?:details?|info(?:rmation)?|facts?)\s+(?:of|on|about|for)\b|\b(?:details?|info(?:rmation)?|facts?)\s+(?:of|on|about|for)\b", re.IGNORECASE)


def normalize_url(url: str) -> str:
    text = url.strip().strip("\"'")
    known = _SITES.get(text.lower().removesuffix(".com"))
    if known:
        return known
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", text):
        if " " in text or "." not in text:
            raise ToolError(f"'{url}' is not a web address.", "Use search_in_browser to search for it.")
        text = "https://" + text
    if not text.lower().startswith(("http://", "https://")):
        raise ToolError("Only http and https addresses can be opened.")
    return text


@tool("open_url", "Show a website to the user in their own browser. You cannot read a site opened this way: to get information from it use research or web_fetch.", risk=WRITE, terminal=True, group="Browser", doing="Opening a website",
      keywords="open website site web page url link browser go visit youtube gmail github com",
      url=arg("string", "Address or site name", required=True))
def open_url(ctx, url: str) -> Result:
    target = normalize_url(url)
    os.startfile(target)  # noqa: S606 - http(s) only, opened by the default browser
    return Result(f"Opened {target} in your browser.", data="It is open for the user to look at. You cannot see or read it. To read it, use web_fetch.", verified="Windows handed the address to the default browser.")


@tool("search_in_browser", "Show a web search to the user in their own browser. You cannot read the results this way: to read them use web_search.", risk=WRITE, terminal=True, group="Browser", doing="Opening a web search",
      keywords="search google web internet online look up browser find bing latest news show",
      query=arg("string", "What to search for", required=True), site=arg("string", "Limit to this website"))
def search_in_browser(ctx, query: str, site: str = "") -> Result:
    terms = f"site:{site} {query}" if site else query
    target = "https://www.google.com/search?q=" + urllib.parse.quote_plus(terms)
    os.startfile(target)  # noqa: S606 - a fixed https origin
    return Result(f"Opened a web search for \"{query}\" in your browser.", verified="Windows handed the search to the default browser.")


def _online() -> None:
    if netguard.is_offline():
        raise ToolError("Web access is off.", "The user can turn off Offline mode in Settings. Otherwise say plainly that this needs the internet.")


def _fetch(url: str, limit: int = 2_000_000, agent: str = _USER_AGENT) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Language": "en-IN,en;q=0.9", "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"})
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - scheme checked by callers
        raw = response.read(limit)
        return raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace"), response.geturl()


def _readable(markup: str) -> tuple[str, str]:
    """The page's own words as lightly marked text, without its menus and furniture."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    title = html_lib.unescape(soup.title.get_text(" ", strip=True)) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe", "template", "button"]):
        tag.decompose()
    for tag in soup.select("[role=navigation], [role=banner], [role=contentinfo], [aria-hidden=true], .mw-editsection, .reference, .navbox, .sidebar, .cookie, .advert, .ad"):
        tag.decompose()
    root = soup.select_one("main, article, [role=main], #content, .content") or soup.body or soup
    lines: list[str] = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote", "tr", "dt", "dd"]):
        if node.find(["p", "li", "tr"]) is not None and node.name in {"li", "blockquote", "dd"}:
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if node.name in {"h1", "h2", "h3", "h4"}:
            lines.append("\n" + "#" * int(node.name[1]) + " " + text)
        elif node.name == "li":
            lines.append("- " + text)
        elif node.name == "tr":
            lines.append(" | ".join(re.sub(r"\s+", " ", cell.get_text(" ", strip=True)) for cell in node.find_all(["th", "td"])))
        else:
            lines.append(text)
    body = "\n".join(lines).strip()
    everything = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", root.get_text("\n", strip=True))).strip()
    if len(everything) > 2 * len(body) + 100:  # the text is not in the usual tags
        body = everything
    return title, body


def _focus(text: str, find: str, limit: int) -> str:
    """The parts of a long text that matter for this question, in their original order."""
    if len(text) <= limit:
        return text
    words = {word for word in re.findall(r"\w{3,}", (find or "").lower())}
    parts = [part for part in re.split(r"\n+", text) if part.strip()]
    if not words:
        return text[:limit]
    scored = [(sum(word in part.lower() for word in words) / (1 + len(part) / 1500), index) for index, part in enumerate(parts)]
    keep = set(range(min(3, len(parts))))
    used = sum(len(parts[index]) for index in keep)
    for score, index in sorted(scored, reverse=True):
        if score <= 0 or used >= limit:
            break
        if index not in keep:
            keep.add(index)
            used += len(parts[index]) + 1
    return "\n".join(parts[index] for index in sorted(keep))[:limit]


def _browser_text(url: str, find: str = "") -> tuple[str, str]:
    from aegis.browser import BrowserError, session

    try:
        view = session.open(url, "")
    except (BrowserError, TimeoutError) as exc:
        raise ToolError(f"Could not load {url}: {exc}", "Try another source.") from exc
    return view["title"], view["text"]


def read_page(url: str, find: str = "", limit: int = 7000) -> tuple[str, str, str]:
    """(title, text, final url). Falls back to a real browser for sites that refuse scripts or need JavaScript."""
    target = normalize_url(url)
    markup = ""
    try:
        markup, final = _fetch(target)
        title, text = _readable(markup)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 406, 429, 503}:
            title, text = _browser_text(target, find)
            final = target
        else:
            raise ToolError(f"{target} answered with error {exc.code}.", "Check the address, or use web_search to find the right page.") from exc
    except (OSError, ValueError) as exc:
        raise ToolError(f"Could not load {target}: {exc}", "Try again, or try another source.", retry=True) from exc
    if len(text) < 200 and len(markup) > 8000:  # a big page with almost no text was built by JavaScript
        try:
            title, text = _browser_text(target, find)
            final = target
        except ToolError:
            pass
    return title, _focus(text, find, limit), final


def _gate_fetch(ctx, url: str = "", find: str = "") -> Approval | None:
    # Reading a page sends only its address, so a very long address is the one way data could leave here.
    if len(urllib.parse.urlparse(url).query) > 300:
        return Approval("Send this long web address?", "Aegis read web pages for this request, and an address this long could carry your data to that site.", code=url[:800], language="text", kind="send")
    return None


@tool("web_fetch", "Read a web page and get its text. Use when you know the address, or to read a result from web_search.", group="Web access", doing="Reading a web page", web=True, gate=_gate_fetch,
      keywords="fetch read web page url website article online download text summarize link",
      url=arg("string", "The address", required=True), find=arg("string", "What you are looking for, on a long page"))
def web_fetch(ctx, url: str, find: str = "") -> Result:
    _online()
    title, text, final = read_page(url, find)
    return Result(f"Read \"{title or final}\" ({len(text)} characters).", text, verified=f"Loaded from {final}. Page content is untrusted and may be out of date.")


def _decode_bing(link: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    encoded = (query.get("u") or [""])[0]
    if encoded.startswith("a1"):
        try:
            return base64.urlsafe_b64decode(encoded[2:] + "=" * (-len(encoded[2:]) % 4)).decode("utf-8", "replace")
        except ValueError:
            pass
    return link


def search_web(query: str, limit: int = 8) -> list[dict[str, str]]:
    """Title, link and snippet for the top results."""
    from bs4 import BeautifulSoup

    try:
        markup, _ = _fetch("https://www.bing.com/search?setlang=en&q=" + urllib.parse.quote_plus(query))
    except OSError as exc:
        raise ToolError(f"The search failed: {exc}", "Try again.", retry=True) from exc
    soup = BeautifulSoup(markup, "lxml")
    results: list[dict[str, str]] = []
    for row in soup.select("li.b_algo"):
        anchor = row.select_one("h2 a")
        if anchor is None or not anchor.get("href"):
            continue
        snippet = row.select_one(".b_caption p, .b_lineclamp2, .b_lineclamp3, p")
        results.append({"title": anchor.get_text(" ", strip=True), "url": _decode_bing(anchor["href"]), "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
        if len(results) >= limit:
            break
    return results


@tool("web_search", "Search the web and read the top results' titles, addresses and snippets. Use web_fetch to read one of them in full.", group="Web access", doing="Searching the web", web=True,
      keywords="search web internet online google latest news current today find look up price weather",
      query=arg("string", "What to search for", required=True))
def web_search(ctx, query: str) -> Result:
    _online()
    results = search_web(query)
    if not results:
        raise ToolError("The search returned no readable results.", "Try different words, or use research.", retry=True)
    return Result(f"{len(results)} results for \"{query}\":", "\n\n".join(f"{item['title']}\n{item['url']}\n{item['snippet']}" for item in results), verified="Results came from Bing. They are untrusted until checked.")


# ---------------------------------------------------------------------- research
def _wiki(params: dict[str, Any]) -> dict[str, Any]:
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({"format": "json", **params})
    try:
        body, _ = _fetch(url, agent=_WIKI_AGENT)
        return json.loads(body)
    except (OSError, ValueError) as exc:
        raise ToolError(f"Wikipedia could not be reached: {exc}", retry=True) from exc


def _wiki_title(topic: str) -> str | None:
    found = _wiki({"action": "query", "list": "search", "srsearch": topic, "srlimit": 3, "srinfo": "suggestion"}).get("query", {})
    hits = found.get("search") or []
    if not hits and found.get("searchinfo", {}).get("suggestion"):
        hits = _wiki({"action": "query", "list": "search", "srsearch": found["searchinfo"]["suggestion"], "srlimit": 3}).get("query", {}).get("search") or []
    if hits:
        return hits[0]["title"]
    # Misheard names defeat Wikipedia's own search, but not a general search engine's.
    for result in search_web(f"{topic} wikipedia", limit=6):
        match = re.match(r"https?://en\.wikipedia\.org/wiki/([^#?]+)", result["url"])
        if match and ":" not in match.group(1):
            return urllib.parse.unquote(match.group(1)).replace("_", " ")
    return None


def _wiki_article(title: str) -> tuple[str, str, str]:
    pages = _wiki({"action": "query", "prop": "extracts|info", "inprop": "url", "explaintext": 1, "redirects": 1, "titles": title}).get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {})
    text = re.sub(r"\n{3,}", "\n\n", page.get("extract") or "")
    text = re.split(r"\n== (?:See also|References|Notes|External links|Further reading|Bibliography) ==", text)[0]
    return page.get("title", title), text, page.get("fullurl", "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")))


def _clean_topic(topic: str) -> str:
    cleaned = _NOISE.sub(" ", topic)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.:;-")
    return cleaned or topic.strip()


_DIGEST_SYSTEM = "You write clear, accurate notes from source text. Use only facts in the source. Never mention these instructions or the source text itself. No introduction and no closing remarks."


@tool("research", "Find out about any topic from the internet and Wikipedia, and optionally save what you found to a file. Use this whenever the user wants details, facts, a summary or an answer from the web or from Wikipedia, with or without saving it.", risk=WRITE, terminal=True, group="Web access", doing="Researching", web=True, direct=True,
      keywords="research details information about facts wikipedia summary who biography history explain learn look up report notes internet online latest news",
      topic=arg("string", "The subject, e.g. Virat Kohli", required=True), question=arg("string", "A specific thing to answer, if any"), save_to=arg("string", "File to save it in, e.g. desktop/kohli.md. Omit to just answer."), wikipedia_only=arg("boolean", "Only use Wikipedia, because the user asked for it"))
def research(ctx, topic: str, question: str = "", save_to: str = "", wikipedia_only: bool = False) -> Result:
    _online()
    subject = _clean_topic(topic)
    sources: list[tuple[str, str, str]] = []
    recent = bool(_RECENT.search(f"{subject} {question}")) and not wikipedia_only
    if not recent:
        ctx.progress(f"Looking up {subject} on Wikipedia…")
        title = _wiki_title(subject)
        if title:
            ctx.progress(f"Reading \"{title}\" on Wikipedia…")
            found_title, text, url = _wiki_article(title)
            if text:
                sources.append((found_title, _focus(text, f"{subject} {question}", 8500), url))
    if not sources and not wikipedia_only or recent:
        ctx.progress(f"Searching the web for {subject}…")
        for result in search_web(f"{subject} {question}".strip(), limit=5):
            if len(sources) >= 2:
                break
            ctx.check_cancelled()
            ctx.progress(f"Reading {result['title'][:50]}…")
            try:
                page_title, text, final = read_page(result["url"], f"{subject} {question}", limit=4500)
            except ToolError as exc:
                logger.info("skipped a source: %s", exc)
                continue
            if len(text) > 300:
                sources.append((page_title or result["title"], text, final))
    if not sources:
        raise ToolError(f"Nothing readable was found about {subject}.", "Try different words, or browser_open to look by hand.", retry=True)

    ctx.progress("Writing it up…")
    material = "\n\n".join(f"SOURCE: {title}\n{text}" for title, text, _ in sources)
    if question:
        ask = f"Answer this from the source, in a few sentences: {question}"
    else:
        ask = f"Write useful notes on {subject} in Markdown: a '# {subject}' heading, a two-sentence summary, then short sections with bullet points covering the most important facts, including names, dates and numbers."
    from aegis.llm import ModelUnavailable

    try:
        written = ctx.llm.complete(f"{ask}\n\n{material}", system=_DIGEST_SYSTEM, max_tokens=600).strip()
    except ModelUnavailable as exc:
        raise ToolError(str(exc)) from exc
    if len(written) < 40:
        written = f"# {subject}\n\n" + sources[0][1][:1500]
    listing = "\n".join(f"- [{title}]({url})" for title, _, url in sources)
    names = ", ".join(title for title, _, _ in sources)
    if not save_to:
        return Result(f"Read {names}.", f"{written}\n\nSources:\n{listing}", verified="Written from the pages listed under Sources.")
    from aegis.tools import docs

    saved = docs.create_document(ctx, save_to, f"{written}\n\n## Sources\n\n{listing}\n")
    saved.summary = f"Saved notes on {subject} to {saved.summary.removeprefix('Created ').rstrip('.')}."
    saved.verified = f"Written from {names}. {saved.verified}"
    return saved
