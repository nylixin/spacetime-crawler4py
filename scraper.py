import re
import json
import os
import hashlib
from collections import defaultdict
from urllib.parse import urlparse, urljoin, urldefrag, urlencode, parse_qsl, unquote
from bs4 import BeautifulSoup

# Dumps the stats that we are looking for into a json file so that it can be accessed later for 
# making our report. We are saving the number of unique page, longest page, list of all domains/subdomains 
# and the top 50 common words (ignoring stop words) 
STATS_FILE = "crawler_stats.json"

# Stop words that we are filtering out for. Hard coded for preventing bugs
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

# load the stats from STATS_FILE that we have been collecting and return it as a dict
def _load_stats():
    if not os.path.exists(STATS_FILE):
        return {
            "unique_pages":     set(),
            "word_frequencies": {},
            "longest_page":     {"url": "", "count": 0},
            "subdomains":       {},
        }
    with open(STATS_FILE, "r") as f:
        data = json.load(f)
    data["unique_pages"] = set(data["unique_pages"])
    data["subdomains"]   = {k: set(v) for k, v in data["subdomains"].items()}
    return data

def _save_stats(stats):
    data = {
        "unique_pages":     list(stats["unique_pages"]),
        "word_frequencies": stats["word_frequencies"],
        "longest_page":     stats["longest_page"],
        "subdomains":       {k: list(v) for k, v in stats["subdomains"].items()},
    }
    with open(STATS_FILE, "w") as f:
        json.dump(data, f)

# Tokenizer 
def tokenize_text(text: str) -> list:
    """Tokenize a raw string (not a file path) into lowercase alphanumeric tokens."""
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

# Detecting near duplicates
_seen_fingerprints = set()

def _simhash(tokens: list) -> int:
    v = [0] * 64
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    fingerprint = 0
    for i in range(64):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint

def _is_near_duplicate(fingerprint: int, threshold: int = 3) -> bool:
    """Return True if fingerprint is within `threshold` bits of any seen fingerprint."""
    for seen in _seen_fingerprints:
        diff = bin(fingerprint ^ seen).count("1")
        if diff <= threshold:
            return True
    return False

# URL canonicalization for Defragmenting 
def _canonicalize(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    scheme = p.scheme.lower()
    host   = p.netloc.lower()
    # remove default ports
    host = re.sub(r":80$", "", host) if scheme == "http" else re.sub(r":443$", "", host)
    # strip trailing slash (except root)
    path = p.path.rstrip("/") or "/"
    # sort query params for consistency
    query = urlencode(sorted(parse_qsl(p.query))) if p.query else ""
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")

# Trap and Low Info Detection
# Calendar, Repeating Paths, Session ID Identifiers, Path Depth, Parameter Limiter, Content Length, 
def _has_low_info_content(soup, min_words=25) -> bool:
    """Return True if the page has too little textual content to be worth indexing."""
    text = soup.get_text(separator=" ")
    words = [w for w in tokenize_text(text) if w not in STOP_WORDS]
    return len(words) < min_words

def _is_calendar_trap(url: str) -> bool:
    """Detect common infinite calendar / date-pagination traps."""
    patterns = [
        r"/calendar/",
        r"\?date=",
        r"\?month=",
        r"[?&](year|month|day)=\d+",
        r"/\d{4}/\d{2}/\d{2}/",        # /yyyy/mm/dd/ paths
        r"/page/\d+",                    # pagination (any depth)
        r"[?&](ical|outlook-ical)=",     # iCal / Outlook export URLs
        r"/(day|week|month)/\d",        # date-based views
        r"[?&]tribe-bar-date=",         # The Events Calendar plugin
        r"[?&]tribe_events_cat=",       # Events Calendar category
    ]
    return any(re.search(p, url, re.IGNORECASE) for p in patterns)

def _has_repeated_path_segments(parsed) -> bool:
    """Detect URLs whose path has repeating directory segments (crawler traps)."""
    segments = [s for s in parsed.path.split("/") if s]
    # If any segment appears more than twice in the path, it's likely a trap
    seen = defaultdict(int)
    for seg in segments:
        seen[seg] += 1
        if seen[seg] > 2:
            return True
    return False

def _has_session_id(url: str) -> bool:
    """Detect URLs with common session ID parameters that could lead to infinite loops."""
    session_patterns = [
        r"[?&](jsessionid|sessionid|sid|session|php_sessid|aspsessionid)=",
        r"[?&](id|uid)=[a-f0-9]{32,}",  # long hex IDs (likely session tokens)
    ]
    return any(re.search(p, url, re.IGNORECASE) for p in session_patterns)

def _has_excessive_path_depth(parsed) -> bool:
    """Detect URLs with unusually deep path hierarchies (potential infinite crawler traps)."""
    segments = [s for s in parsed.path.split("/") if s]
    # Reject if path is deeper than 12 levels
    return len(segments) > 12

def _has_too_many_params(parsed) -> bool:
    if not parsed.query:
        return False
    params = unquote(parsed.query).split("&")
    return len(params) > 4

def _check_content_length(resp) -> bool:
    """Check for content-length mismatches that indicate truncated responses."""
    if not resp.raw_response or not hasattr(resp.raw_response, 'headers'):
        return True  # Assume valid if we can't check
    
    content_length_header = resp.raw_response.headers.get('content-length', '')
    if not content_length_header:
        return True  # No header, assume valid
    
    try:
        declared = int(content_length_header)
        actual = len(resp.raw_response.content) if resp.raw_response.content else 0
        # If actual is significantly less than declared, it may be truncated
        if actual > 0 and declared > 0 and actual < (declared * 0.8):
            return False  # Content truncation detected
    except (ValueError, TypeError):
        return True  # Invalid header, assume valid
    
    return True

# Run Scraper
def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]


def extract_next_links(url, resp):
    # Pass Bad Response Pages
    if resp.status != 200 or resp.raw_response is None:
        return []

    content_type = resp.raw_response.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        return []

    content = resp.raw_response.content
    if not content or len(content) < 100:          # empty / near-empty page
        return []
    if len(content) > 10 * 1024 * 1024:            # skip files > 10 MB
        return []

    # Truncated Response Pass
    if not _check_content_length(resp):
        return []

    # HTML Parsing with BeautifulSoup
    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception:
            return []

    # Calling Canonicalize
    canonical_url = _canonicalize(url)

    # SimHash Near Duplication Function
    raw_text = soup.get_text(separator=" ")
    tokens   = tokenize_text(raw_text)
    fingerprint = _simhash(tokens)
    is_duplicate = _is_near_duplicate(fingerprint)
    _seen_fingerprints.add(fingerprint)

    # Skipping Duplicate and Low Info Pages
    if is_duplicate or _has_low_info_content(soup):
        return []

    # Persistent Stat Updater
    stats = _load_stats()

    # 1. Adding Unique Pages
    stats["unique_pages"].add(canonical_url)

    # 2. Word Frequencies
    for token in tokens:
        if token not in STOP_WORDS and len(token) > 1:
            stats["word_frequencies"][token] = (
                stats["word_frequencies"].get(token, 0) + 1
            )

    # 3. Finding Longest Page
    word_count = len(tokens)
    if word_count > stats["longest_page"]["count"]:
        stats["longest_page"] = {"url": canonical_url, "count": word_count}

    # 4. Checking SubDomains for UCI
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
        # Removing whitespace from href attr
        href = tag["href"].strip()
        # Skipping empty, email links and JS actions
        if not href or href.startswith("mailto:") or href.startswith("javascript:"):
            continue

        # Converting relative links to absolute URLS, then making sure that it is normalized to get rid of duplicated fragment links
        absolute = urljoin(resp.raw_response.url, href)
        absolute = _canonicalize(absolute)
        # Keep canonicalized valid links
        if absolute:
            extracted.append(absolute)

    return extracted


# URL Validator 
def is_valid(url):
    try:
        parsed = urlparse(url)

        # Must be http or https
        if parsed.scheme not in {"http", "https"}:
            return False

        # Blocking Long URLS
        if len(url) > 200:
            return False

        hostname = parsed.netloc.lower()
        query    = unquote(parsed.query)

        # Domain Whitelist
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

        # Filtering out dynamic trap pages by blocking query parameters on .php pages
        if parsed.path.lower().endswith(".php") and query:
            return False

        # Blocking URLs with semicolons in the query 
        if ";" in query:
            return False

        # Blocking UI State / Dashboard parameter traps 
        if query and any(p in query for p in (
            "filter[", "action=", "skin=", "lang=",
            "from=now", "to=now", "refresh=", "orgId=", "var-",
            "do=", "idx=", "tab_files=", "tab_details=", "image=",
            "format=",
        )):
            return False

        # Blocking Infinite pagination through timestamps 
        if re.search(r"(^|&)from=\d{4}-\d{2}-\d{2}T", query):
            return False
        if "/timeline" in parsed.path and parsed.query:
            return False

        # Blocking Event URLs
        if "eventDisplay=" in query:
            return False
        if "ical" in query or "outlook-ical" in query:
            return False
        if "/events/" in parsed.path:
            return False

        # Blocking Numeric ID enumeration traps
        if re.search(r"^id=\d+$", query):
            return False

        # Blocking non-content subdomains 
        if re.search(r"(intranet|grafana|observium|kibana|mailman|gitlab)\.ics\.uci\.edu", hostname):
            return False
        if "mailman" in hostname or "pipermail" in parsed.path.lower():
            return False

        # Blocking fano's ca/rules enum trap 
        if hostname == "fano.ics.uci.edu" and parsed.path.startswith("/ca/rules"):
            return False

        # Blocking numeric publication page expansions 
        if re.search(r"/publications/r\d+[a-z]?\.html?$", parsed.path, re.IGNORECASE):
            return False

        # Blocking login / auth pages 
        if re.search(r"/(login|logout|signin|signout|wp-login|auth|oauth|sso|cas|admin|account|my-account)(\.php)?(/|$|\?)", parsed.path, re.IGNORECASE):
            return False

        # Blocking wiki wwhen it has query params 
        if hostname == "wiki.ics.uci.edu" and parsed.query:
            return False

        # Blocking RSS/Atom feed URLs
        if re.search(r"/(feed|rss|atom)(\.xml)?(/|$)", parsed.path, re.IGNORECASE):
            return False

        # Blocking Doku PHP
        if "doku.php" in parsed.path.lower():
            return False

        # Blocking genealogy due to low information
        if "genealogy" in parsed.path.lower():
            return False

        # Blocking numeric ID expansion traps 
        if re.search(r":\d{4,}", parsed.path):
            return False

        # Blocking low value paths 
        if re.search(
            r"/(archive|deprecated|legacy|backup|old|tmp|temp|test|dev|staging|cache|trash|junk)/",
            parsed.path, re.IGNORECASE
        ):
            return False

        # Skipping archival links (<2020)
        if re.search(r"/(200\d|201\d)/", parsed.path):
            return False
        # Block old course / quarter pages like:
        # cs122b-2017-winter, cs122b-2017-spring-project3, winter-2016
        if re.search(r'\b\d{4}-(spring|summer|fall|winter)\b', parsed.path, re.IGNORECASE):
            return False
        if re.search(r"(19\d{2}|20[01]\d|2020)[-_](spring|summer|fall|winter)", parsed.path, re.IGNORECASE):
            return False
        if re.search(r"(spring|summer|fall|winter)[-_](19\d{2}|20[01]\d|2020)", parsed.path, re.IGNORECASE):
            return False

        # Detecting traps
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

        # Blocking UCI Archive
        if hostname == "archive.ics.uci.edu" and parsed.path.startswith("/datasets") and parsed.query:
            return False

        # Blocking non HTML pages
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            r"|png|tiff?|mid|mp2|mp3|mp4"
            r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            r"|ps|eps|tex|ppt|pptx|pps|ppsx|doc|docx|xls|xlsx|names"
            r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            r"|epub|dll|cnf|tgz|sha1"
            r"|thmx|mso|arff|rtf|jar|csv"
            r"|rm|smil|wmv|swf|wma|zip|rar|gz"
            r"|txt|m|mat|r|rdata|rds|sas|sav|spss|sql|db)$",
            parsed.path.lower()
        )

    except (TypeError, ValueError):
        return False


# Report Generator
def generate_report(output_path="report.txt"):
    stats = _load_stats()

    lines = []

    # Q1 – Unique pages
    lines.append("=" * 60)
    lines.append(f"Q1: Unique pages found: {len(stats['unique_pages'])}")

    # Q2 – Longest page
    lp = stats["longest_page"]
    lines.append("=" * 60)
    lines.append(f"Q2: Longest page: {lp['url']}")
    lines.append(f"    Word count:   {lp['count']}")

    # Q3 – Top 50 words
    lines.append("=" * 60)
    lines.append("Q3: Top 50 most common words (stop words excluded):")
    sorted_words = sorted(
        stats["word_frequencies"].items(), key=lambda x: -x[1]
    )[:50]
    for rank, (word, freq) in enumerate(sorted_words, 1):
        lines.append(f"    {rank:>2}. {word:<30} {freq}")

    # Q4 – Subdomains
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
