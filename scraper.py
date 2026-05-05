import re
import shelve
from collections import defaultdict
from urllib.parse import urlparse, urljoin, urldefrag
from bs4 import BeautifulSoup

# ─── Constants ───────────────────────────────────────────────────────────────
STATS_FILE = "crawler_stats.shelve"

# English stop words (common words to ignore in frequency analysis)
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "for", "from", "had", "has", "have", "he", "her", "hers", "him", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "just", "me", "might",
    "my", "myself", "no", "nor", "not", "of", "on", "or", "our", "ours",
    "ourselves", "out", "over", "own", "so", "some", "such", "than", "that",
    "the", "their", "theirs", "them", "themselves", "then", "there", "these",
    "they", "this", "those", "to", "too", "under", "until", "up", "was", "we",
    "were", "what", "which", "while", "who", "whom", "why", "with", "you",
    "your", "yours", "yourself", "yourselves", "is", "it", "will", "should",
    "could", "would", "should", "may", "can", "might", "must", "shall"
}

def _load_stats():
    with shelve.open(STATS_FILE) as db:
        return {
            "unique_pages":    set(db.get("unique_pages", set())),
            "word_frequencies": dict(db.get("word_frequencies", {})),
            "longest_page":    db.get("longest_page", {"url": "", "count": 0}),
            "subdomains":      dict(db.get("subdomains", {})),
        }

def _save_stats(stats):
    with shelve.open(STATS_FILE) as db:
        db["unique_pages"]     = stats["unique_pages"]
        db["word_frequencies"] = stats["word_frequencies"]
        db["longest_page"]     = stats["longest_page"]
        db["subdomains"]       = stats["subdomains"]

def tokenize_text(text: str) -> list:
    tokens = []
    token = []
    for ch in text:
        if ch.isalnum():
            token.append(ch.lower())
        else:
            if token:
                tokens.append("".join(token))
                token = []
    if token:
        tokens.append("".join(token))
    return tokens

def compute_word_frequencies(tokens: list) -> dict:
    frequencies = {}
    for token in tokens:
        frequencies[token] = frequencies.get(token, 0) + 1
    return frequencies

def _has_low_info_content(soup, min_words=50) -> bool:
    text = soup.get_text(separator=" ")
    words = [w for w in tokenize_text(text) if w not in STOP_WORDS]
    return len(words) < min_words

def _is_calendar_trap(url: str) -> bool:
    patterns = [
        r"/calendar/",
        r"\?date=",
        r"\?month=",
        r"[?&](year|month|day)=\d+",
        r"/\d{4}/\d{2}/\d{2}/",
        r"/page/\d{3,}",
    ]
    return any(re.search(p, url, re.IGNORECASE) for p in patterns)

def _has_repeated_path_segments(parsed) -> bool:
    segments = [s for s in parsed.path.split("/") if s]
    seen = defaultdict(int)
    for seg in segments:
        seen[seg] += 1
        if seen[seg] > 2:
            return True
    return False

def _has_session_id(url: str) -> bool:
    session_patterns = [
        r"[?&](jsessionid|sessionid|sid|session|php_sessid|aspsessionid)=",
        r"[?&](id|uid)=[a-f0-9]{32,}",
    ]
    return any(re.search(p, url, re.IGNORECASE) for p in session_patterns)

def _has_excessive_path_depth(parsed) -> bool:
    segments = [s for s in parsed.path.split("/") if s]
    return len(segments) > 12

def _has_too_many_params(parsed) -> bool:
    if not parsed.query:
        return False
    params = parsed.query.split("&")
    return len(params) > 8

def _check_content_length(resp) -> bool:
    if not resp.raw_response or not hasattr(resp.raw_response, 'headers'):
        return True
    content_length_header = resp.raw_response.headers.get('content-length', '')
    if not content_length_header:
        return True
    try:
        declared = int(content_length_header)
        actual = len(resp.raw_response.content) if resp.raw_response.content else 0
        if actual > 0 and declared > 0 and actual < (declared * 0.8):
            return False
    except (ValueError, TypeError):
        return True
    return True

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    if resp.status != 200 or resp.raw_response is None:
        return []
    content = resp.raw_response.content
    if not content or len(content) < 100:
        return []
    if len(content) > 10 * 1024 * 1024:
        return []
    if not _check_content_length(resp):
        return []
    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        return []
    if _has_low_info_content(soup):
        return []
    canonical_url, _ = urldefrag(url)
    stats = _load_stats()
    stats["unique_pages"].add(canonical_url)
    raw_text = soup.get_text(separator=" ")
    tokens   = tokenize_text(raw_text)
    for token in tokens:
        if token not in STOP_WORDS and len(token) > 1:
            stats["word_frequencies"][token] = (
                stats["word_frequencies"].get(token, 0) + 1
            )
    word_count = len(tokens)
    if word_count > stats["longest_page"]["count"]:
        stats["longest_page"] = {"url": canonical_url, "count": word_count}
    parsed_url = urlparse(canonical_url)
    hostname   = parsed_url.netloc.lower()
    if hostname.endswith(".uci.edu"):
        if hostname not in stats["subdomains"]:
            stats["subdomains"][hostname] = set()
        if not isinstance(stats["subdomains"][hostname], set):
            stats["subdomains"][hostname] = set(stats["subdomains"][hostname])
        stats["subdomains"][hostname].add(canonical_url)
    _save_stats(stats)
    extracted = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        absolute = urljoin(resp.raw_response.url, href)
        absolute, _ = urldefrag(absolute)
        if absolute:
            extracted.append(absolute)
    return extracted

def is_valid(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        hostname = parsed.netloc.lower()
        path = parsed.path.lower()
        query = parsed.query.lower()
        query_keys = {part.split("=", 1)[0].lower() for part in parsed.query.lower().split("&") if part}
        allowed = (
            re.search(r"(^|\.)ics\.uci\.edu$",         hostname) or
            re.search(r"(^|\.)cs\.uci\.edu$",           hostname) or
            re.search(r"(^|\.)informatics\.uci\.edu$",  hostname) or
            re.search(r"(^|\.)stat\.uci\.edu$",         hostname) or
            (hostname == "today.uci.edu" and
             parsed.path.startswith("/department/information_computer_sciences"))
        )
        if not allowed:
            return False
        # Blocking common event traps
        event_trap_patterns = [
                r"/events?/list/",
                r"/events?/calendar/",
                r"/events?/category/",
                r"/events?/tag/",
                r"/events?/page/\d+",
                r"/events?/day/",
                r"/events?/month/",
                r"/events?/week/",
                r"/events?/archive",
                ]
        if any(re.search(p, parsed.path.lower()) for p in event_trap_patterns):
            return False
        # Blocking event query traps
        event_query_keys = {
                "tribe_event_display",
                "tribe_paged",
                "tribe__ecp_custom_49",
                "eventdate",
                "ical",
                }
        if "event" in path and (query_keys & event_query_keys):
            return False
        # Repeating Parameters
        params = parsed.query.split("&")
        param_names = [p.split("=")[0] for p in params if "=" in p]
        if len(param_names) != len(set(param_names)):
            return False
        if "status" in path and "action=update" in query:
            return False
        if len(url) > 200:
            return False
        # Wiki Trap Avoiding
        if hostname in {"wiki.ics.uci.edu", "swiki.ics.uci.edu"}:
            blocked_query_keys = {
                "do",
                "idx",
                "image",
                "ns",
                "tab_files",
                "tab_details",
                "sectok",
            }
            if query_keys & blocked_query_keys:
                return False 
            # Avoiding deep wiki namespaces
            if path.count(":") > 3:
                return False
        if len(query_keys) > 5:
            return False
        if not allowed:
            return False
        if _is_calendar_trap(url):
            return False
        if _has_repeated_path_segments(parsed):
            return False
        if _has_session_id(url):
            return False
        if _has_excessive_path_depth(parsed):
            return False
        if _has_too_many_params(parsed):
            return False
        if _has_session_id(url):
            return False
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            r"|png|tiff?|mid|mp2|mp3|mp4"
            r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            r"|epub|dll|cnf|tgz|sha1"
            r"|thmx|mso|arff|rtf|jar|csv"
            r"|rm|smil|wmv|swf|wma|zip|rar|gz)$",
            parsed.path.lower()
        )
    except (TypeError, ValueError):
        print("TypeError/ValueError for", url)
        raise

def generate_report(output_path="report.txt"):
    stats = _load_stats(
    lines = []
    lines.append("=" * 60)
    lines.append(f"Q1: Unique pages found: {len(stats['unique_pages'])}")
    lp = stats["longest_page"]
    lines.append("=" * 60)
    lines.append(f"Q2: Longest page: {lp['url']}")
    lines.append(f"    Word count:   {lp['count']}")
    lines.append("=" * 60)
    lines.append("Q3: Top 50 most common words (stop words excluded):")
    sorted_words = sorted(
        stats["word_frequencies"].items(), key=lambda x: -x[1]
    )[:50]
    for rank, (word, freq) in enumerate(sorted_words, 1):
        lines.append(f"    {rank:>2}. {word:<30} {freq}")
    lines.append("=" * 60)
    lines.append("Q4: Subdomains in uci.edu (alphabetical):")
    for subdomain in sorted(stats["subdomains"].keys()):
        pages = stats["subdomains"][subdomain]
        count = len(pages) if isinstance(pages, set) else pages
        lines.append(f"    {subdomain}, {count}")
    report_text = "\n".join(lines)
    print(report_text)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\nReport saved to {output_path}")
