import re
from urllib.parse import urlparse, urljoin, parse_qs
from bs4 import BeautifulSoup
from collections import Counter

# Global data structures for tracking crawl state
skip = set()
# skip stores URLs with bad response codes, invalid content, or non-HTML files
visited = set()
# visited stores all successfully scraped and valid URLs
check_traps = {}
# check_traps tracks URL frequency by base URL and subdomain to detect crawler traps

# Global tracking variables for report generation
longest_page_url = None
longest_page_word_count = 0
subdomains = {}
# subdomains maps hostnames to sets of URLs found under each subdomain
word_counter = Counter()
# word_counter tracks word frequencies across all pages

# Size and length limits for crawling
MIN_FILE_SIZE = 100                # 100 bytes minimum
MAX_FILE_SIZE = 5 * 1024 * 1024    # 5 MB maximum
URL_MAXLEN = 225
SEGMENTS_MAXLEN = 10
QUERY_PARAMS_MAXLEN = 5

# Load stop words from file
try:
    with open("stop_words.txt") as f:
        stop_words = set(f.read().split())
except FileNotFoundError:
    # Fallback stop words if file not found
    stop_words = {
        "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
        "for", "from", "had", "has", "have", "he", "her", "hers", "him", "his",
        "how", "i", "if", "in", "into", "is", "it", "its", "just", "me", "might",
        "my", "myself", "no", "nor", "not", "of", "on", "or", "our", "ours",
        "ourselves", "out", "over", "own", "so", "some", "such", "than", "that",
        "the", "their", "theirs", "them", "themselves", "then", "there", "these",
        "they", "this", "those", "to", "too", "under", "until", "up", "was", "we",
        "were", "what", "which", "while", "who", "whom", "why", "with", "you",
        "your", "yours", "yourself", "yourselves", "will", "should",
        "could", "would", "may", "can", "might", "must", "shall"
    }



def scraper(url, resp):
    """
    Main scraper function that validates response and extracts links.
    Returns only valid links from the provided URL.
    """
    links = []
    if resp.status == 200:
        links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]




def extract_next_links(url, resp):
    """
    Scrapes a URL's response for content analysis and link extraction.
    Updates global statistics and returns all discovered links.
    """
    global skip, visited
    global longest_page_url, longest_page_word_count, subdomains, word_counter

    links = []
    cleaned_url = url.split("#")[0]  # Remove fragment identifier

    # Content validation and skip checks
    if resp.status != 200:
        skip.add(cleaned_url)
        return []
    
    if not resp.raw_response:
        skip.add(cleaned_url)
        return []
    
    # Check for HTML content type
    content_type = resp.raw_response.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        skip.add(cleaned_url)
        return []
    
    # Check file size constraints
    content_size = len(resp.raw_response.content)
    if content_size < MIN_FILE_SIZE or content_size > MAX_FILE_SIZE:
        skip.add(cleaned_url)
        return []

    # Mark as visited since it passed initial validation
    visited.add(cleaned_url)

    try:
        # Parse page content
        soup = BeautifulSoup(resp.raw_response.content, "lxml")
        words = re.findall(r'\w+', soup.get_text(separator=' ').lower())

        # Check minimum word count threshold
        if len(words) < 20:
            skip.add(cleaned_url)
            return []

        # Filter out stop words and update word counter
        filtered_words = [word for word in words if word not in stop_words]
        word_counter.update(filtered_words)

        # Track longest page
        word_count = len(words)
        if word_count > longest_page_word_count:
            longest_page_word_count = word_count
            longest_page_url = cleaned_url

        # Track subdomains under uci.edu
        parsed_url = urlparse(cleaned_url)
        hostname = parsed_url.netloc.lower()

        if hostname.endswith("ics.uci.edu") or \
           hostname.endswith("cs.uci.edu") or \
           hostname.endswith("informatics.uci.edu") or \
           hostname.endswith("stat.uci.edu"):
            if hostname not in subdomains:
                subdomains[hostname] = set()
            subdomains[hostname].add(cleaned_url)

        # Extract all links from the page
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith("mailto:") or href.startswith("javascript:"):
                continue
            
            absolute_url = urljoin(base_url, href)
            clean_link = absolute_url.split("#")[0]
            
            if clean_link and clean_link not in links:
                links.append(clean_link)

    except Exception as e:
        print(f"ERROR parsing {url}: {e}")
        return []

    return links




def is_valid(url):
    """
    Validates a URL based on domain whitelist, length constraints,
    and various trap detection patterns.
    """
    global skip, visited, check_traps

    try:
        parsed = urlparse(url)

        # Validate scheme
        if parsed.scheme not in {"http", "https"}:
            return False

        # Check skip and visited
        if url in visited or url in skip:
            return False

        # URL length validation
        if len(url) > URL_MAXLEN:
            return False

        # Path segment validation
        path_segments = [s for s in parsed.path.split('/') if s]
        if len(path_segments) > SEGMENTS_MAXLEN:
            return False

        # Query parameter validation
        query_params = parsed.query.split('&') if parsed.query else []
        if len(query_params) > QUERY_PARAMS_MAXLEN:
            return False

        # Trap detection: track base URL frequency
        base_url = url.split("?")[0]
        check_traps[base_url] = check_traps.get(base_url, 0) + 1
        if check_traps[base_url] > 175:
            return False

        # Trap detection: track subdomain frequency
        subdomain = parsed.netloc.lower()
        check_traps[subdomain] = check_traps.get(subdomain, 0) + 1
        if check_traps[subdomain] > 500:
            return False

        # Calendar trap detection
        if (re.search(r'\b\d{4}[-/]\d{2}[-/]\d{2}\b|\b\d{2}[-/]\d{2}[-/]\d{4}\b', url) or
            re.search(r'\b\d{4}[-/]\d{2}(-\d{2})?\b', url) or
            re.search(r'[?&](date|year|month|day|view|do|tab_files|ical)=[^&]*', url, re.IGNORECASE)):
            return False

        # GitLab trap detection
        if re.search(r'gitlab\.ics\.uci\.edu.*(/-/|/users/|/blob/|/commits/|/tree/|/compare|/explore/|\.git$)', url):
            return False

        # Special file download traps
        if re.search(r'sli\.ics\.uci\.edu.*\?action=download&upname=', url):
            return False

        # Login redirect traps
        if re.search(r'wp-login\.php\?redirect_to=[^&]+', url):
            return False

        # Pagination trap
        if re.search(r'/page/\d+', url):
            return False

        # Version/format traps
        if (re.search(r'[\?&]version=\d+', url) or
            re.search(r'[\?&]action=diff&version=\d+', url) or
            re.search(r'[\?&]format=txt', url)):
            return False

        # Season-based time trap (e.g., 2024-spring)
        if re.search(r'\b\d{4}-(spring|summer|fall|winter)\b', parsed.path, re.IGNORECASE):
            return False

        # DokuWiki trap detection (doku.php creates infinite variations)
        if re.search(r'/doku\.php/', parsed.path, re.IGNORECASE):
            return False

        # Check for unwanted file extensions
        extension_pattern = (
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            r"|png|tiff?|mid|mp2|mp3|mp4"
            r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            r"|epub|dll|cnf|tgz|sha1"
            r"|thmx|mso|arff|rtf|jar|csv"
            r"|rm|smil|wmv|swf|wma|zip|rar|gz|bam)$")

        if re.match(extension_pattern, parsed.path.lower()):
            return False

        # Check query string values for unwanted file extensions
        queries = parse_qs(parsed.query)
        for values in queries.values():
            for value in values:
                if re.match(extension_pattern, value.lower()):
                    return False

        # Domain whitelist check
        hostname = parsed.netloc.lower()
        is_allowed_domain = re.match(
            r'^(.+\.)?(ics\.uci\.edu|cs\.uci\.edu|informatics\.uci\.edu|stat\.uci\.edu)$',
            hostname
        )

        return bool(is_allowed_domain)

    except TypeError:
        print(f"TypeError for {url}")
        raise




def output_report(output_path="report.txt"):
    """
    Generates a report file with crawl statistics including unique pages,
    longest page, top 50 most common words, and discovered subdomains.
    """
    with open(output_path, "w", encoding="utf-8") as file:
        # Q1: Unique pages
        unique_count = len(visited)
        file.write(f"Unique pages found: {unique_count}\n\n")

        # Q2: Longest page
        file.write(f"Longest page: {longest_page_url}\n")
        file.write(f"Word count: {longest_page_word_count}\n\n")

        # Q3: Top 50 most common words
        common_words = word_counter.most_common(50)
        file.write("Top 50 most common words (word, frequency):\n")
        for rank, (word, freq) in enumerate(common_words, 1):
            file.write(f"{rank:>2}. {word:<30} {freq}\n")
        file.write("\n")

        # Q4: Subdomains
        file.write("Subdomains under uci.edu:\n")
        for subdomain in sorted(subdomains.keys()):
            count = len(subdomains[subdomain])
            file.write(f"{subdomain}, {count}\n")

    print(f"\nReport saved to {output_path}")
