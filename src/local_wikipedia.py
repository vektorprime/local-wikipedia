#!/usr/bin/env python3

import re
import yaml
import asyncio
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
import logging
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount
import uvicorn
from typing import Optional, Tuple, List, Literal
from collections import OrderedDict
from dataclasses import dataclass, field

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s' # Timestamps are handled by Docker logs
)
logger = logging.getLogger(__name__)

DB_PARAMS = dict(host="localhost", port=5432, dbname="finewiki", user="dbuser", password="dbpass")
connection_pool = None  # Initialized at startup

# Load config.yaml
try:
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)
        LANGUAGES = config["source"]["language"]
        server = config.get("server", {})
        logger.info(f"Available languages from config: {LANGUAGES}")
        PORT = server.get("port", 29423)
        logger.info(f"Server port from config: {PORT}")
        MAX_SEARCH_RESULTS = server.get("max_search_results", 20)
        logger.info(f"Max search results from config: {MAX_SEARCH_RESULTS}")
    logger.info(f"Loaded languages from config: {LANGUAGES}")
except Exception as e:
    logger.error(f"Failed to load config.yaml: {e}")
    raise

# Stringify available language list
AVAILABLE_LANGUAGES_STR = "[" + ", ".join(LANGUAGES) + "]"

# CJK language code set
CJK_LANGUAGES = {'ja', 'zh', 'ko'}

mcp = FastMCP("wikipedia-mcp", host="0.0.0.0", port=PORT)


# ========================================
# Utility functions
# ========================================
def is_cjk_language(lang: str) -> bool:
    """
    Determine if language code is CJK
    
    Args:
        lang: Language code
    
    Returns:
        True if CJK language
    """
    return lang in CJK_LANGUAGES


def count_text_units(text: str, is_cjk: bool) -> int:
    """
    Count characters or words in text
    
    Args:
        text: Text
        is_cjk: Whether it's a CJK language
    
    Returns:
        Character count for CJK, word count otherwise
    """
    if is_cjk:
        return len(text)
    else:
        return len(text.split())


# ========================================
# Database connection helper
# ========================================

def init_connection_pool(min_conn=2, max_conn=10):
    """Initialize the threaded connection pool (call once at startup)"""
    global connection_pool
    connection_pool = ThreadedConnectionPool(min_conn, max_conn, **DB_PARAMS)
    logger.info(f"DB connection pool initialized: {min_conn}-{max_conn} connections")


@contextmanager
def db_cursor(autocommit: bool = False):
    """
    Provide database cursor from connection pool (thread-safe)

    Args:
        autocommit: Auto-commit (default: False)

    Yields:
        psycopg2.cursor: Database cursor
    """
    conn = connection_pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
            if not autocommit:
                conn.commit()
    except Exception as e:
        logger.error(f"Database error: {e}")
        conn.rollback()
        raise
    finally:
        connection_pool.putconn(conn)


# ========================================
# Data access layer
# ========================================

def get_document_by_title(cur, title: str, lang: str) -> Optional[Tuple[str, str]]:
    """
    Fetch article by title
    
    Args:
        cur: Database cursor
        title: Article title
        lang: Language code
    
    Returns:
        (title, text_body) or None
    """
    logger.debug(f"DB Query: get_document_by_title with title='{title}', lang='{lang}'")
    # Wikipedia uses '_' in titles, but finewiki stores them with spaces, so convert
    title = title.replace("_", " ")
    cur.execute(
        "SELECT title, text_body FROM documents WHERE title = %s AND language_code = %s LIMIT 1",
        (title, lang)
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def get_page_id_by_title(cur, title: str, lang: str) -> Optional[int]:
    """
    Get page_id by title
    
    Args:
        cur: Database cursor
        title: Article title
        lang: Language code
    
    Returns:
        page_id or None
    """
    normalized_title = normalize_title_for_page(title)
    logger.debug(f"DB Query: get_page_id_by_title with title='{normalized_title}', lang='{lang}'")
    cur.execute(
        "SELECT page_id FROM pages WHERE page_title = %s AND language_code = %s LIMIT 1",
        (normalized_title, lang)
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_redirect_target(cur, page_id: int, lang: str) -> Optional[str]:
    """
    Get redirect target title from page_id
    
    Args:
        cur: Database cursor
        page_id: Page ID
        lang: Language code
    
    Returns:
        Redirect target title or None
    """
    logger.debug(f"DB Query: get_redirect_target with page_id='{page_id}', lang='{lang}'")
    cur.execute(
        "SELECT to_title FROM redirections WHERE from_page_id = %s AND language_code = %s LIMIT 1",
        (page_id, lang)
    )
    row = cur.fetchone()
    return row[0] if row else None


def search_exact_match(cur, query: str, lang: str) -> Optional[Tuple[str, str]]:
    """
    Exact match search
    
    Args:
        cur: Database cursor
        query: Search query
        lang: Language code
    
    Returns:
        (title, text_body) or None
    """
    logger.debug(f"DB Query: search_exact_match with query='{query}', lang='{lang}'")
    cur.execute(
        "SELECT title, text_body FROM documents WHERE title = %s AND language_code = %s LIMIT 1",
        (query, lang)
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def search_title_match(cur, query: str, lang: str, limit: int) -> List[Tuple[str, str]]:
    """
    Title partial match
    
    Args:
        cur: Database cursor
        query: Search query
        lang: Language code
        limit: Maximum results
    
    Returns:
        List of (title, snippet) tuples
    """
    if not query: return [] # Prevent errors from empty queries
    logger.debug(f"DB Query: search_title_match with query='{query}', lang='{lang}', limit={limit}")
    cur.execute(
        "SELECT title, pgroonga_snippet_html(title, pgroonga_query_extract_keywords(%s)) "
        "FROM documents WHERE title &@~ %s AND language_code = %s LIMIT %s",
        (query, query, lang, limit)
    )
    return [(row[0], row[1][0]) for row in cur.fetchall()]


def search_redirect_match(cur, query: str, lang: str, limit: int) -> List[Tuple[str, str]]:
    """
    Redirect match search
    
    Args:
        cur: Database cursor
        query: Search query
        lang: Language code
        limit: Maximum results
    
    Returns:
        List of (from_title, to_title) tuples
    """
    normalized_query = normalize_title_for_page(query)
    logger.debug(f"DB Query: search_redirect_match with query='{normalized_query}', lang='{lang}', limit={limit}")
    cur.execute(
        """
        SELECT p.page_title, r.to_title
        FROM pages p
        JOIN redirections r ON p.page_id = r.from_page_id AND p.language_code = r.language_code
        WHERE (p.page_title ILIKE %s OR p.page_title = %s) AND p.language_code = %s
        LIMIT %s
        """,
        (f"%{normalized_query}%", normalized_query, lang, limit)
    )
    return [(row[0].replace("_", " "), row[1]) for row in cur.fetchall()]


def search_body_match(cur, query: str, lang: str, exclude_title: str, limit: int) -> List[Tuple[str, str]]:
    """
    Body text match search
    
    Args:
        cur: Database cursor
        query: Search query
        lang: Language code
        exclude_title: Title to exclude (to avoid exact match duplicates)
        limit: Maximum results
    
    Returns:
        List of (title, snippet) tuples
    """
    if not query: return [] # Prevent errors from empty queries
    logger.debug(f"DB Query: search_body_match with query='{query}', lang='{lang}', exclude_title='{exclude_title}', limit={limit}")
    cur.execute(
        "SELECT title, pgroonga_snippet_html(text_body, pgroonga_query_extract_keywords(%s)) "
        "FROM documents WHERE text_body &@~ %s AND language_code = %s AND title != %s LIMIT %s",
        (query, query, lang, exclude_title, limit)
    )
    return [(row[0], row[1][0]) for row in cur.fetchall()]


def get_random_article(cur, langs: List[str]) -> Optional[Tuple[str, str]]:
    """
    Fetch random article
    
    Args:
        cur: Database cursor
        langs: Language code
    
    Returns:
        (title, text_body) or None
    """
    logger.debug(f"DB Query: get_random_article with langs='{langs}'")
    # Fetch one random document from the given languages
    cur.execute(
        "SELECT title, text_body FROM documents "
        "WHERE language_code = ANY(%s) "
        "ORDER BY RANDOM() LIMIT 1",
        (langs,)
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


# ========================================
# Heuristic search logic
# ========================================
def generate_heuristic_queries(query: str, search_languages: List[str]) -> List[str]:
    """
    Generate multiple search candidates from the original query using heuristics.
    Return a list of queries sorted by priority.

    Args:
        query: Original search query
        search_languages: Target language list

    Returns:
        List of search query candidates
    """
    if not query:
        return []

    # Use OrderedDict to maintain order while deduplicating
    queries = OrderedDict()
    queries[query.strip()] = None  # Original query gets highest priority

    # 1. Add capitalized version
    capitalized_q = query.strip().capitalize()
    if capitalized_q != query:
        queries[capitalized_q] = None
    
    # Check if CJK languages are included
    contains_cjk = any(lang in CJK_LANGUAGES for lang in search_languages)
    meaningful_length = 3 if contains_cjk else 6

    # 2. Remove language code suffixes like (ja)
    lang_code_pattern = re.compile(r'(.+?)\s+\(([a-z]{2,3})\)$', re.IGNORECASE)
    match = lang_code_pattern.match(query)
    if match:
        stripped_query = match.group(1).strip()
        queries[stripped_query] = None

    # 3. Bracket handling
    # e.g., from 'Article_(City)' extract 'Article_' (high priority) and 'City' (low priority)
    bracket_pairs = [('「', '」'), ('『', '』'), ('(', ')'), ('[', ']'), ('【', '】')]
    # Hold bracket contents for later (lower priority)
    bracket_contents = []

    # Copy and iterate current candidates
    current_queries = list(queries.keys())
    for q in current_queries:
        # a. First extract bracket contents for later addition
        for start, end in bracket_pairs:
            escaped_start = re.escape(start)
            escaped_end = re.escape(end)
            inner_matches = re.findall(f'{escaped_start}(.+?){escaped_end}', q)
            for inner in inner_matches:
                inner_stripped = inner.strip()
                # Ignore extracted strings that are too short
                if inner_stripped and len(inner_stripped) >= 2:
                    bracket_contents.append(inner_stripped)
        
        # b. Version with brackets simply removed (add first, higher priority)
        stripped_q = q
        for start, end in bracket_pairs:
            stripped_q = stripped_q.replace(start, "").replace(end, "")
        
        stripped_q = stripped_q.strip()
        if stripped_q and stripped_q != q:
            queries[stripped_q] = None
    
    # 4. Remove verbose patterns that LLMs tend to produce
    current_queries = list(queries.keys())
    for q in current_queries:
        modified_q = q
        
        # Multi-language prefix/suffix patterns
        prefix_patterns = [
            r"^(?:tell me about|explain|what is|what's|describe)\s+", # en
            r"^(?:was ist|erkläre mir|erzähl mir von|beschreibe)\s+", # de
            r"^(?:qu'est-ce que|qu'est-ce qu'un|qu'est-ce qu'une|parlez-moi de|expliquez-moi|décris-moi)\s+", # fr
            r"^(?:что такое|расскажи(?:те)? о|опиши(?:те)?)\s+", # ru
            r"^(?:qué es|explícame|háblame de|descríbeme)\s+", # es
            r"^(?:che cos'è|cos'è|spiegami|parlami di|descrivimi)\s+", # it
        ]
        suffix_patterns = [
            r"(?:について(?:教えて|おしえて)?|とは|を(?:教えて|おしえて|調べて|しらべて))$", # ja (Japanese suffix patterns)
            r"\s+(?:about|on the topic of|regarding)$", # en
            r"\s+(?:über|bezüglich|hinsichtlich)$", # de
            r"\s+(?:sur|à propos de|concernant)$", # fr
            r"\s+(?:о|об|про)$", # ru (limited)
            r"\s+(?:sobre|acerca de)$", # es
            r"\s+(?:su|riguardo a)$", # it
        ]
        split_patterns = [
            r"\s+of\s+", # en
            r"[のでにをは]", # ja (Japanese particles)
            r"\s+von\s+", # de
            r"\s+de\s+", # fr, es
            r"\s+di\s+", # it
            r"\s+о\s+", # ru
            r"\s*[_\-<>|:;/\\]\s*", # symbols/punctuation
        ]
        
        # Remove prefixes
        for pattern in prefix_patterns:
            modified_q = re.sub(pattern, '', modified_q, flags=re.IGNORECASE)
        # Remove suffixes
        for pattern in suffix_patterns:
            modified_q = re.sub(pattern, '', modified_q, flags=re.IGNORECASE)

        # Split on "of"/"の" to extract key parts
        for pattern in split_patterns:
            parts = re.split(pattern, modified_q, flags=re.IGNORECASE)
            # Extract parts above minimum length
            valid_parts = []
            for part in parts:
                part = part.strip()
                # Also remove brackets from split parts
                for start, end in bracket_pairs:
                    part = part.replace(start, "").replace(end, "")
                part = part.strip()
                if len(part) >= meaningful_length:
                    valid_parts.append(part)
            
            # Front-first for CJK (order of appearance), longest-first otherwise
            if contains_cjk:
                # Add in order of appearance (earlier = higher priority)
                for part in valid_parts:
                    if part and part != q:
                        queries[part] = None
            else:
                # Sort by length descending
                valid_parts.sort(key=len, reverse=True)
                for part in valid_parts:
                    if part and part != q:
                        queries[part] = None

        # For CJK languages, also try space-splitting
        if contains_cjk:
            space_parts = modified_q.split()
            # Add front to back (order of appearance)
            for part in space_parts:
                part = part.strip()
                # Also remove brackets from space-split parts
                for start, end in bracket_pairs:
                    part = part.replace(start, "").replace(end, "")
                part = part.strip()
                if len(part) >= meaningful_length and part not in queries:
                    queries[part] = None
        
        # Strip remaining bracket/quote chars from edges
        bracket_chars = "「」『』\"'()[]【】"
        modified_q = modified_q.strip(bracket_chars)

        modified_q = modified_q.strip()
        if modified_q and modified_q != q:
            queries[modified_q] = None

    # 5. Finally add bracket contents (lowest priority)
    for content in bracket_contents:
        if content not in queries:
            queries[content] = None

    final_queries = list(queries.keys())
    logger.info(f"Generated heuristic queries for '{query}': {final_queries}")
    return final_queries


# ========================================
# Business logic layer
# ========================================

def normalize_title_for_page(title: str) -> str:
    """
    Normalize to Wikipedia page_title format (spaces→underscores, capitalize first letter)
    
    Args:
        title: Original title
    
    Returns:
        Normalized title
    """
    normalized = title.replace(" ", "_")
    if normalized:
        normalized = normalized[0].upper() + normalized[1:]
    return normalized


def resolve_redirect(cur, title: str, lang: str) -> Optional[Tuple[str, str]]:
    """
    Get redirect target and original title
    
    Args:
        cur: Database cursor
        title: Original title
        lang: Language code
    
    Returns:
        (redirect_target_title, original_title) or None
    """
    page_id = get_page_id_by_title(cur, title, lang)
    if not page_id:
        return None
    
    redirect_target = get_redirect_target(cur, page_id, lang)
    if redirect_target:
        logger.info(f"Redirect found: {title} -> {redirect_target} (lang: {lang})")
        return (redirect_target, title)
    
    return None


def validate_languages(languages: Optional[List[str]]) -> Tuple[bool, List[str], str]:
    """
    Validate language list and determine search languages
    
    Args:
        languages: Language code list (optional)
    
    Returns:
        (is_valid, search_languages, error_message)
    """
    if languages is None or languages == []:
        return (True, LANGUAGES, "")
    
    validated_languages = [lang.lower() for lang in languages]
    for lang in validated_languages:
        if lang not in LANGUAGES:
            error_msg = f"Error: Language '{lang}' is not available. Available languages: {AVAILABLE_LANGUAGES_STR}"
            return (False, [], error_msg)
    return (True, validated_languages, "")


def normalize_languages_input(languages: Optional[list[str] | str]) -> Optional[list[str]]:
    """
    Normalize languages argument to list[str].
    
    Args:
        languages: Language specification (list, str, or None)
    
    Returns:
        Normalized list[str] or None
    """
    if languages is None:
        return None
    
    # Already a list
    if isinstance(languages, list):
        return languages
    
    # If string
    if isinstance(languages, str):
        # Strip whitespace first
        languages = languages.strip()
        
        if not languages:
            return None
        
        # Comma-separated (with or without spaces: 'en,ja' or 'en, ja, de')
        if ',' in languages:
            return [lang.strip() for lang in languages.split(',') if lang.strip()]
        
        # Space-separated (safe since language codes contain no spaces)
        if ' ' in languages:
            return [lang.strip() for lang in languages.split() if lang.strip()]
        
        # Single language code
        return [languages]
    
    # Log and return None for unexpected types
    logger.warning(f"Unexpected type for languages: {type(languages)}, value: {languages}")
    return None


# ========================================
# Presentation layer
# ========================================
@dataclass
class Paragraph:
    """Dataclass representing a paragraph or block element"""
    text: str
    line_start: int  # Line number where this block starts
    priority: int = 0
    parent: Optional['HeadingBlock'] = None


@dataclass
class HeadingBlock:
    """Tree node representing a heading and its content"""
    level: int
    title: str
    content: str
    line_start: int  # Line number where this heading appears
    is_special: bool = False
    paragraphs: List[Paragraph] = field(default_factory=list)
    children: List['HeadingBlock'] = field(default_factory=list)
    parent: Optional['HeadingBlock'] = None


def parse_markdown(text: str) -> Tuple[HeadingBlock, List[HeadingBlock], List[Paragraph]]:
    """Parse a Markdown document into a hierarchical structure"""
    root = HeadingBlock(level=0, title="root", content="", line_start=-1)
    all_headings: List[HeadingBlock] = []
    all_paragraphs: List[Paragraph] = []
    
    current = root
    first_level2_seen = False
    
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]

        # 1. Heading processing
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            is_special = (level == 2 and not first_level2_seen)
            if level == 2:
                first_level2_seen = True
            
            heading = HeadingBlock(level=level, title=title, content=line, line_start=i, is_special=is_special)
            all_headings.append(heading)
            
            while current.level >= level and current.parent is not None:
                current = current.parent
            
            heading.parent = current
            current.children.append(heading)
            current = heading
            i += 1
            continue

        # 2. Skip blank lines
        if not line.strip():
            i += 1
            continue

        # 3. Block element processing
        start_index = i
        
        if line.strip().startswith('```'):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                i += 1
        elif re.match(r'^\s*([-*+]|\d+\.)\s+', line):
            i += 1
            while i < len(lines) and lines[i].strip():
                # Treat consecutive non-blank lines as one list item
                i += 1
            i -= 1
        elif '|' in line:
            i += 1
            while i < len(lines) and '|' in lines[i]:
                i += 1
            i -= 1

        block_lines = lines[start_index : i + 1]
        para_text = "\n".join(block_lines)
        if para_text.strip():
            para = Paragraph(text=para_text, line_start=start_index, parent=current)
            current.paragraphs.append(para)
            all_paragraphs.append(para)
        
        i += 1

    return root, all_headings, all_paragraphs


def assign_priorities(root: HeadingBlock, all_headings: List[HeadingBlock]) -> None:
    """Assign priorities using hierarchical round-robin algorithm"""
    priority = 1
    max_level = max([h.level for h in all_headings], default=0)
    
    # Process paragraphs directly under root (treated as level 0)
    if root.paragraphs:
        for para in root.paragraphs:
            para.priority = priority
            priority += 1

    # Process from level 1 upward
    for level in range(1, max_level + 1):
        nodes = [h for h in all_headings if h.level == level]
        if not nodes:
            continue
        
        special = [n for n in nodes if n.is_special]
        for node in special:
            for para in node.paragraphs:
                para.priority = priority
                priority += 1
        
        normal = [n for n in nodes if not n.is_special]
        if normal:
            idx = 0
            while True:
                processed = False
                for node in normal:
                    if idx < len(node.paragraphs):
                        node.paragraphs[idx].priority = priority
                        priority += 1
                        processed = True
                if not processed:
                    break
                idx += 1


def reconstruct_markdown(selected_paragraphs: List[Paragraph], 
                        all_headings: List[HeadingBlock]) -> Tuple[str, List[HeadingBlock]]:
    """Reconstruct Markdown from selected paragraphs and required headings"""
    
    # 1. Identify headings needed for output
    required_headings: List[HeadingBlock] = []
    for para in selected_paragraphs:
        h = para.parent
        while h and h.level > 0:
            if h not in required_headings:
                required_headings.append(h)
            h = h.parent

    # 2. Combine all output elements (headings and paragraphs) into one list
    output_elements: List[Union[HeadingBlock, Paragraph]] = required_headings + selected_paragraphs

    # 3. Sort by original document order (line number)
    output_elements.sort(key=lambda elem: elem.line_start)

    # 4. Join sorted elements into text
    result_parts = []
    for elem in output_elements:
        text = elem.content if isinstance(elem, HeadingBlock) else elem.text
        result_parts.append(text)
    
    result_text = "\n\n".join(result_parts)

    # 5. Compute omitted headings
    omitted = [h for h in all_headings if h not in required_headings and h.level >= 2]
    # Sort by original document order (all_headings already sorted, index-based)
    omitted.sort(key=lambda h: all_headings.index(h))
    
    return result_text, omitted


def extract_article_by_length(text_body: str, length: Literal["very-short", "short", "medium", "full"], is_cjk: bool) -> str:
    """
    Extract article at the specified length
    
    Args:
        text_body: Article body
        length: Extraction length ('very-short', 'short', 'medium', 'full')
        is_cjk: Whether it's a CJK language
    
    Returns:
        Extracted text
    """
    if length == "full":
        return text_body
    
    if length == "short":
        limit = 300 if is_cjk else 150
    elif length == "medium":
        limit = 3000 if is_cjk else 1500
    else:  # very-short
        limit = 100 if is_cjk else 50
    
    root, all_headings, all_paragraphs = parse_markdown(text_body)
    assign_priorities(root, all_headings)
    
    # Sort by priority (fallback to original order)
    sorted_paras = sorted(all_paragraphs, key=lambda p: (p.priority, p.line_start))
    
    selected: List[Paragraph] = []
    total_units = 0
    
    # Track already-included headings
    temp_included_headings: List[HeadingBlock] = []
    
    for para in sorted_paras:
        if not para.text.strip(): continue
        
        # Compute cost of adding this paragraph
        prospective_units = count_text_units(para.text, is_cjk)
        
        # Also include cost of newly-needed headings
        h = para.parent
        while h and h.level > 0:
            if h not in temp_included_headings:
                prospective_units += count_text_units(h.content, is_cjk)
            h = h.parent

        if total_units + prospective_units <= limit:
            selected.append(para)
            total_units += prospective_units
            
            # Actually added, so update heading list
            h = para.parent
            while h and h.level > 0:
                if h not in temp_included_headings:
                    temp_included_headings.append(h)
                h = h.parent
        else:
            # Include at least one paragraph even if it exceeds the limit
            if not selected:
                selected.append(para)
            break
            
    result_text, omitted_headings = reconstruct_markdown(selected, all_headings)

    if length == "very-short":
        # For very-short, return without extra info
        # Also, replace header markup with plain text
        simple_text = []
        for line in result_text.split('\n'):
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if heading_match:
                title = heading_match.group(2).strip()
                simple_text.append(f"{title}")
            else:
                simple_text.append(line)
        return "\n".join(simple_text)

    # 1. Determine if any paragraphs were omitted
    total_non_empty_paragraphs = sum(1 for p in all_paragraphs if p.text.strip())
    has_omitted_paragraphs = len(selected) < total_non_empty_paragraphs

    # 2. Append Omitted Headings section
    if omitted_headings:
        omitted_text_parts = ["\n\n## Omitted Headings"]
        for heading_block in omitted_headings:
            level = heading_block.level
            title = heading_block.title
            indent = "  " * (level - 2)
            omitted_text_parts.append(f"{indent}- {title}")
        result_text += "\n".join(omitted_text_parts)
    
    # 3. If paragraphs were omitted, append the hint message
    if has_omitted_paragraphs:
        larger_length = "medium" if length == "short" else "full"
        result_text += f"\n\nIf you want to read more, please use the `search_local_wikipedia` tool with `length='{larger_length}'` to get a more detailed article.\n"

    return result_text


def format_html_snippet(html_snippet: str, max_length: int = 200) -> str:
    """
    Format HTML snippet (strip tags, convert emphasis, limit length)
    
    Args:
        html_snippet: HTML snippet
        max_length: Maximum characters
    
    Returns:
        Formatted text
    """
    formatted = html_snippet.replace('<span class="keyword">', '**').replace('</span>', '**')
    if len(formatted) > max_length:
        formatted = formatted[:max_length] + "..."
    return formatted


def format_article_with_redirect_notice(text_body: str, from_title: str, to_title: str, length: Literal["very-short", "short", "medium", "full"], lang: str) -> str:
    """
    Format article with redirect notice
    
    Args:
        text_body: Article body
        from_title: Redirect source title
        to_title: Redirect target title
        length: Extraction length
        lang: Language code
    
    Returns:
        Formatted article text
    """
    redirect_notice = f"(Redirected from '{from_title}' to '{to_title}')\n\n"
    is_cjk = is_cjk_language(lang)
    snippet = extract_article_by_length(text_body, length, is_cjk)
    return redirect_notice + snippet


# ========================================
# MCP tools
# ========================================

def _search_sync(
    title: str,
    length: str,
    languages: Optional[list[str] | str],
) -> str:
    """
    Search and read a Wikipedia article by title. The search process includes exact title match, redirect resolution, partial title match, and full-text search.
    
    Args:
        title: Article title to read. such as "Wikipedia"
        length: Length of the article to extract. Defaults to "medium". Set "very-short" for a brief snippet, "short" for a summary, "medium" for a detailed summary, and "full" or "long" for the entire article.
        languages: Specific language code list (optional). Available languages: {AVAILABLE_LANGUAGES_STR}.
    
    If user want to search in detail, **please set `length='full'`** to read the full text of the article.
    
    **Be careful when setting arguments when using the tool**.
    """
    logger.info(f"search_local_wikipedia called with title: {title}, languages: {languages}, length: {length}")

    if length == "long":
        length = "full"

    # Normalize language parameter
    normalized_languages = normalize_languages_input(languages)
    logger.info(f"Normalized languages: {normalized_languages}")

    # Validate language parameter
    is_valid, search_languages, error_msg = validate_languages(normalized_languages)
    if not is_valid:
        return error_msg
    
    # Generate query variations using heuristics
    queries_to_try = generate_heuristic_queries(title, search_languages)
    if not queries_to_try:
        return f"Article not found: Invalid title '{title}'"
        
    try:
        with db_cursor() as cur:

            # Try each query variant against each language in order
            for query_variant in queries_to_try:
                for lang in search_languages:

                    # 1. Exact title match
                    logger.info(f"Trying exact match for '{query_variant}' in {lang}")
                    result = get_document_by_title(cur, query_variant, lang)
                    if result:
                        found_title, text_body = result
                        is_cjk = is_cjk_language(lang)
                        snippet = extract_article_by_length(text_body, length, is_cjk)
                        notice = ""
                        if query_variant != title:
                            notice = f"(Found article '{found_title}' based on your query '{title}')\n\n"
                        logger.info(f"Article found: {found_title} in {lang}")
                        return notice + snippet
                    
                    # 2. Redirect exact match
                    logger.info(f"Checking redirect for '{query_variant}' in {lang}")
                    redirect_info = resolve_redirect(cur, query_variant, lang)
                    if redirect_info:
                        redirect_title, original_title_from_redirect = redirect_info
                        logger.info(f"Following redirect: {query_variant} -> {redirect_title} in {lang}")
                        
                        article_result = get_document_by_title(cur, redirect_title, lang)
                        if article_result:
                            _, text_body = article_result
                            logger.info(f"Article found via redirect: {query_variant} -> {redirect_title} in {lang}")
                            
                            from_display = f"'{original_title_from_redirect}'"
                            if query_variant != title:
                                from_display = f"'{query_variant}' (from query '{title}')"

                            return format_article_with_redirect_notice(text_body, from_display, redirect_title, length, lang)

            # If still not found, fall back to partial match search
            results = []
            for query_variant in queries_to_try:
                for lang in search_languages:

                    # 3. Title partial match
                    logger.info(f"Trying title match for '{query_variant}' in {lang}")
                    title_matches = search_title_match(cur, query_variant, lang, MAX_SEARCH_RESULTS - len(results))
                    for match_title, snippet in title_matches:
                        doc = get_document_by_title(cur, match_title, lang)
                        summary = extract_article_by_length(doc[1], "very-short", is_cjk_language(lang)) if doc else "No summary available."
                        results.append(f"## [Title Match] {match_title} ({lang})\n{summary}\n")
                        if len(results) >= MAX_SEARCH_RESULTS:
                            break

                    # 4. Redirect partial match
                    logger.info(f"Trying redirect match for '{query_variant}' in {lang}")
                    redirect_matches = search_redirect_match(cur, query_variant, lang, MAX_SEARCH_RESULTS - len(results))
                    for from_t, to_t in redirect_matches:
                        doc = get_document_by_title(cur, to_t, lang)
                        summary = extract_article_by_length(doc[1], "very-short", is_cjk_language(lang)) if doc else "No summary available."
                        results.append(f"## [Redirect Match] {from_t} -> {to_t} ({lang})\n{summary}\n")
                        if len(results) >= MAX_SEARCH_RESULTS:
                            break
            
            # If still fewer than 20 results, full-text body search
            for query_variant in queries_to_try:
                for lang in search_languages:
                    logger.info(f"Trying body match for '{query_variant}' in {lang}")
                    body_matches = search_body_match(cur, query_variant, lang, query_variant, MAX_SEARCH_RESULTS - len(results))
                    for match_title, snippet in body_matches:
                        results.append(f"## [Body Match] {match_title} ({lang})\n{format_html_snippet(snippet)}\n")
                        if len(results) >= MAX_SEARCH_RESULTS:
                            break
            
            if results:
                logger.info(f"Partial matches found for title: {title}")
                return "The following articles were found in your search:\n\n" + "\n---\n".join(results)
            
            logger.warning(f"Article not found for any variation of: {title}")
            return f"Article not found: {title}\nPlease try different keywords."
    except Exception as e:
        logger.error(f"Error in search_local_wikipedia: {e}", exc_info=True)
        return f"Error reading article: {str(e)}"


# ========================================
# Async MCP tool wrapper
# ========================================

@mcp.tool()
async def search_local_wikipedia(
    title: str,
    length: Literal["very-short", "short", "medium", "full"] = "medium",
    languages: Optional[list[str] | str] = None,
) -> str:
    """
    Search and read a Wikipedia article by title. The search process includes exact title match, redirect resolution, partial title match, and full-text search.

    Args:
        title: Article title to read. such as "Wikipedia"
        length: Length of the article to extract. Defaults to "medium". Set "very-short" for a brief snippet, "short" for a summary, "medium" for a detailed summary, and "full" or "long" for the entire article.
        languages: Specific language code list (optional).

    If user want to search in detail, **please set ** to read the full text of the article.

    **Be careful when setting arguments when using the tool**.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _search_sync, title, length, languages)


# ========================================
# Application startup
# ========================================

# Streamable HTTP transport for OpenWebUI compatibility
# (replaces Starlette+Mount with native FastMCP streamable HTTP runner)


# Initialize connection pool for threaded access
init_connection_pool()

# Expose app for uvicorn workers
mcp_app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting MCP Wikipedia server (streamable HTTP)...")
    print("Starting MCP Wikipedia server (streamable HTTP)...")
    uvicorn.run(mcp_app, host="0.0.0.0", port=PORT)
