import streamlit as st
import pandas as pd
import sqlite3
import math
import os
import re
import unicodedata
import requests
import html
import io
import urllib.parse
import uuid
import threading
import time
from PIL import Image, ImageDraw, ImageFont
from typing import Dict, Optional, Union, List, Set, Tuple

DB_NAME = "catalog.db"
TABLE_NAME = "user_catalog"
FEEDBACK_DB = "feedback.db"
TEMPLATE_PATH = "goodreads-template.csv"


def init_feedback_db(db_name=FEEDBACK_DB):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recommendation_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT,
            title TEXT,
            author TEXT,
            taste_match_pct REAL,
            action TEXT,
            ol_key TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_feedback_batch(items: List[Dict], db_name=FEEDBACK_DB):
    if not items:
        return
    init_feedback_db(db_name)
    try:
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        for it in items:
            cursor.execute("""
                INSERT INTO recommendation_feedback (session_id, timestamp, title, author, taste_match_pct, action, ol_key)
                VALUES (?, datetime('now'), ?, ?, ?, ?, ?)
            """, (it.get("session_id"), it.get("title"), it.get("author"), float(it.get("taste_match_pct") or 0.0), it.get("action"), it.get("ol_key")))
        conn.commit()
        conn.close()
    except Exception:
        pass

    try:
        webhook_url = st.secrets.get("GSHEETS_WEBHOOK_URL", "")
        if webhook_url:
            def _worker(url=webhook_url, payload_list=list(items)):
                for p in payload_list:
                    try:
                        requests.post(url, json={
                            "session_id": p.get("session_id", ""),
                            "title": p.get("title", ""),
                            "author": p.get("author", ""),
                            "taste_match_pct": float(p.get("taste_match_pct") or 0.0),
                            "action": p.get("action", ""),
                            "ol_key": p.get("ol_key") or "",
                        }, timeout=8)
                        time.sleep(0.15)
                    except Exception:
                        pass

            threading.Thread(target=_worker, daemon=False).start()
    except Exception:
        pass


def log_recommendation_feedback(session_id: str, title: str, author: str, taste_match_pct: float, action: str, ol_key: str = None, db_name=FEEDBACK_DB):
    log_feedback_batch([{
        "session_id": session_id,
        "title": title,
        "author": author,
        "taste_match_pct": taste_match_pct,
        "action": action,
        "ol_key": ol_key,
    }], db_name=db_name)


def fetch_feedback_summary(db_name=FEEDBACK_DB) -> pd.DataFrame:
    init_feedback_db(db_name)
    try:
        conn = sqlite3.connect(db_name)
        df_feed = pd.read_sql_query("SELECT * FROM recommendation_feedback ORDER BY id DESC", conn)
        conn.close()
        return df_feed
    except Exception:
        return pd.DataFrame()


def clear_feedback_db(db_name=FEEDBACK_DB):
    init_feedback_db(db_name)
    try:
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM recommendation_feedback")
        conn.commit()
        cursor.execute("VACUUM")
        conn.commit()
        conn.close()
    except Exception:
        pass


GOODREADS_COLUMNS = [
    "Book Id", "Title", "Author", "Author l-f", "Additional Authors",
    "ISBN", "ISBN13", "My Rating", "Average Rating", "Publisher",
    "Binding", "Number of Pages", "Year Published", "Original Publication Year",
    "Date Read", "Date Added", "Bookshelves", "Bookshelves with positions",
    "Exclusive Shelf", "My Review", "Spoiler", "Private Notes",
    "Read Count", "Owned Copies",
]


# ── Text Parser Helpers ───────────────────────────────────────────────────────
def clean_field(text: str) -> str:
    """Clean a text field (strip quotes, bullets, leading numbering, multiple spaces)."""
    if not text or not isinstance(text, str):
        return ""
    s = text.strip()
    # Strip markdown checkbox markers: "- [ ]", "* [x]", "[ ]", etc.
    s = re.sub(r"^\s*[-*•]?\s*\[\s*[xX ]?\s*\]\s*", "", s)
    # Strip quotes, parenthesis, brackets from edges
    s = re.sub(r"^[\[\]\(\)'\"'\"\u201c\u201d\u2018\u2019]+|[\[\]\(\)'\"'\"\u201c\u201d\u2018\u2019]+$", "", s)
    # Strip numbered list or bullet markers
    s = re.sub(r"^\s*(?:\d+[.)\-]\s*|[\-•*]\s+)", "", s)
    # Strip stray hyphens, dashes, or colons from edges
    s = re.sub(r"^[-–—:]+|[-–—:]+$", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s.title()


def normalize_rating(raw: str) -> float:
    """Parse and normalize rating (handles /5, /10, clamps to 0-5)."""
    if not raw or not isinstance(raw, str):
        return 0.0
    s = raw.strip().lower()

    slash_match = re.search(r"(\d+\.?\d*)\s*/\s*(\d+)", s)
    if slash_match:
        numerator = float(slash_match.group(1))
        denominator = int(slash_match.group(2))
        if denominator == 10:
            value = numerator / 2.0
        elif denominator == 5:
            value = numerator
        else:
            value = (numerator / denominator) * 5.0 if denominator > 0 else 0.0
        return max(0.0, min(5.0, round(value, 2)))

    num_match = re.search(r"(\d+\.?\d*)", s)
    if not num_match:
        return 0.0

    value = float(num_match.group(1))
    if value > 5:
        value = value / 2.0
    return max(0.0, min(5.0, round(value, 2)))


def normalize_for_dedup(title: str, author: str) -> str:
    """Create lowercase alphanumeric deduplication key."""
    t = re.sub(r"[^a-z0-9]", "", title.lower())
    a = re.sub(r"[^a-z0-9]", "", author.lower())
    return f"{t}|{a}"


def parse_book_line(line: str) -> Optional[Dict[str, object]]:
    """Parse a single line using 'by', comma, pipe, tab, or dash delimiters."""
    if not line or not isinstance(line, str):
        return None
    s = line.strip()
    if not s:
        return None

    # Strip markdown checkbox items like "- [ ]", "* [x]", etc.
    s = re.sub(r"^\s*[-*•]?\s*\[\s*[xX ]?\s*\]\s*", "", s).strip()
    # Strip numbered or standard list markers
    s = re.sub(r"^\s*(?:\d+[.)\-]\s*|[•*]\s+)", "", s).strip()

    title = ""
    author = ""
    rating_raw = ""

    # Strategy 1: "Title by Author"
    by_match = re.match(r"^(.+?)\s+by\s+(.+)$", s, re.IGNORECASE)
    if by_match:
        title_part = by_match.group(1).strip()
        rest = by_match.group(2).strip()

        # Handle trailing rating with comma, semicolon, dash, hyphen, or pipe separator
        trailing_rating = re.search(
            r"[,;\-–—|]\s*(\d+\.?\d*(?:\s*/\s*\d+)?(?:\s*stars?)?)\s*$",
            rest,
            re.IGNORECASE,
        )
        if trailing_rating:
            rating_raw = trailing_rating.group(1)
            author_part = rest[:trailing_rating.start()].strip()
        else:
            end_rating = re.search(r"\s+(\d+\.?\d*(?:\s*/\s*\d+)?)\s*$", rest)
            if end_rating:
                rating_raw = end_rating.group(1)
                author_part = rest[:end_rating.start()].strip()
            else:
                author_part = rest

        title = clean_field(title_part)
        author = clean_field(author_part)
        rating = normalize_rating(rating_raw)
        if title and author:
            return {"Title": title, "Author": author, "My Rating": rating}

    # Strategy 2: Pipe-separated
    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        if len(parts) >= 2:
            title = clean_field(parts[0])
            author = clean_field(parts[1])
            rating_raw = parts[2] if len(parts) >= 3 else ""
            if title and author:
                return {"Title": title, "Author": author, "My Rating": normalize_rating(rating_raw)}

    # Strategy 3: Tab-separated
    if "\t" in s:
        parts = [p.strip() for p in s.split("\t") if p.strip()]
        if len(parts) >= 2:
            title = clean_field(parts[0])
            author = clean_field(parts[1])
            rating_raw = parts[2] if len(parts) >= 3 else ""
            if title and author:
                return {"Title": title, "Author": author, "My Rating": normalize_rating(rating_raw)}

    # Strategy 4: Comma-separated
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) >= 2:
            last = parts[-1].strip()
            if re.match(r"^\d+\.?\d*(?:\s*/\s*\d+)?(?:\s*stars?)?$", last, re.IGNORECASE):
                rating_raw = last
                if len(parts) >= 3:
                    title = clean_field(parts[0])
                    author = clean_field(", ".join(parts[1:-1]))
                else:
                    title = clean_field(parts[0])
                    author = ""
            else:
                title = clean_field(parts[0])
                author = clean_field(", ".join(parts[1:]))
                rating_raw = ""

            if title and author:
                return {"Title": title, "Author": author, "My Rating": normalize_rating(rating_raw)}

    # Strategy 5: Dash-separated
    if re.search(r"\s+-\s+", s):
        parts = [p.strip() for p in re.split(r"\s+-\s+", s) if p.strip()]
        if len(parts) >= 2:
            title = clean_field(parts[0])
            author = clean_field(parts[1])
            rating_raw = parts[2] if len(parts) >= 3 else ""
            if title and author:
                return {"Title": title, "Author": author, "My Rating": normalize_rating(rating_raw)}

    return None


def parse_book_text_input(text: str) -> (pd.DataFrame, list):
    """Parse raw text lines into a DataFrame and return any skipped lines."""
    if not text or not isinstance(text, str):
        return pd.DataFrame(columns=["Title", "Author", "My Rating"]), []

    lines = text.strip().split("\n")
    parsed = []
    seen_keys = set()
    skipped = []

    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(title|book)[,|\t]", line, re.IGNORECASE):
            continue

        result = parse_book_line(line)
        if result is None:
            skipped.append((i, line))
            continue

        dedup_key = normalize_for_dedup(result["Title"], result["Author"])
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        parsed.append(result)

    df = pd.DataFrame(parsed, columns=["Title", "Author", "My Rating"])
    return df, skipped


def to_goodreads_df(parsed_df: pd.DataFrame) -> pd.DataFrame:
    """Expand parsed books to standard Goodreads catalog structure."""
    if parsed_df.empty:
        return pd.DataFrame(columns=GOODREADS_COLUMNS)

    out = pd.DataFrame(columns=GOODREADS_COLUMNS)
    out["Title"] = parsed_df["Title"]
    out["Author"] = parsed_df["Author"]
    out["My Rating"] = pd.to_numeric(parsed_df["My Rating"], errors="coerce").astype(float)
    out["Exclusive Shelf"] = "read"
    out["Read Count"] = 1

    def to_last_first(name: str) -> str:
        if not name or not isinstance(name, str):
            return ""
        parts = name.strip().split()
        if len(parts) == 1:
            return parts[0]
        return f"{parts[-1]}, {' '.join(parts[:-1])}"

    out["Author l-f"] = parsed_df["Author"].apply(to_last_first)
    return out


# ── Open Library Enrichment Helpers ───────────────────────────────────────────
PRIORITY_GENRES = [
    "historical fiction", "science fiction", "romance", "fantasy",
    "thriller", "mystery", "suspense", "crime", "horror", "adventure",
    "history", "biography", "autobiography", "memoir", "war", "family",
    "philosophy", "psychology", "politics", "drama", "poetry", "science", "art",
    "nonfiction",
]


def clean_and_prioritize_subjects(
    raw_subjects: list,
    places: list = None,
    times: list = None,
    limit: int = 5,
) -> list:
    """Filter, clean, and prioritize subjects matching priority genres.
    
    Deprioritizes generic 'fiction' so that meaningful, specific genres
    (e.g., historical fiction, fantasy, thriller) are prioritized first.
    'fiction' is only placed first if it is the sole subject available.
    """
    if not raw_subjects:
        return []
    places_set = {str(p).lower().strip() for p in (places or []) if p}
    times_set = {str(t).lower().strip() for t in (times or []) if t}
    time_regex = re.compile(
        r"(\b(1\d{3}|20\d{2})s?\b|\b\d{1,2}(st|nd|rd|th)\s+century\b)",
        re.IGNORECASE,
    )
    seen = set()
    cleaned = []
    has_generic_fiction = False

    for item in raw_subjects:
        if not item or not isinstance(item, str):
            continue
        s = item.lower().strip()
        # Clean prefix like 'fiction / ' or 'fiction - '
        s = re.sub(r"^fiction\s*[/–—\-]\s*", "", s).strip()
        if s in ("fiction", "general", "general fiction"):
            has_generic_fiction = True
            continue
        if any(bad in s for bad in [":", "http", "fast (", "(ocolc)", "new york times", "bestseller"]):
            continue
        if s in seen:
            continue
        seen.add(s)
        if s in places_set or s in times_set or "(imaginary place)" in s or "(place)" in s:
            continue
        if any(p in s for p in places_set if len(p) > 2) and (", fiction" in s or "description and travel" in s):
            continue
        if time_regex.search(s) and not any(kw in s for kw in ["war", "history"]):
            continue
        cleaned.append(s)

    tier1, tier2, tier3 = [], [], []
    for s in cleaned:
        if s in PRIORITY_GENRES:
            tier1.append(s)
        elif any(re.search(r"\b" + re.escape(g) + r"\b", s) for g in PRIORITY_GENRES):
            tier2.append(s)
        else:
            tier3.append(s)

    selected = []
    selected_words = set()
    for s in (tier1 + tier2 + tier3):
        words = set(re.findall(r"\b\w+\b", s))
        if len(words) > 1 and words.issubset(selected_words):
            continue
        selected.append(s)
        selected_words.update(words)
        if len(selected) >= limit:
            break

    # If no specific genres were found, fallback to 'fiction' if present
    if not selected and has_generic_fiction:
        selected.append("fiction")
    elif has_generic_fiction and len(selected) < limit:
        # Append generic fiction to the end so it never overrides specific genres as primary
        selected.append("fiction")

    return selected


def fetch_best_book_match(title: str, author: str) -> Optional[dict]:
    """Query Open Library API by title and author, falling back to query search."""
    url = "https://openlibrary.org/search.json"
    fields = (
        "key,title,author_name,first_publish_year,edition_count,"
        "number_of_pages_median,ratings_average,ratings_count,subject,place,time,cover_i"
    )

    # 1. Exact title & author search
    params = {"title": title, "author": author, "limit": 5, "fields": fields}
    try:
        response = requests.get(url, params=params, timeout=10)
        docs = response.json().get("docs", [])
    except Exception:
        docs = []

    # 2. General query fallback
    if not docs:
        params_q = {"q": f"{title} {author}", "limit": 10, "fields": fields}
        try:
            response = requests.get(url, params=params_q, timeout=10)
            docs = response.json().get("docs", [])
        except Exception:
            docs = []

    if not docs:
        return None

    # Filter by author match if possible
    author_tokens = set(author.lower().split())
    author_matched_docs = [
        d for d in docs
        if any(
            any(t in a.lower() for t in author_tokens if len(t) > 2)
            for a in d.get("author_name", [])
        )
    ]
    candidate_pool = author_matched_docs if author_matched_docs else docs

    # Exclude summaries / study guides
    non_summary = [
        d for d in candidate_pool
        if not any(
            kw in d.get("title", "").lower() or kw in " ".join(d.get("author_name", [])).lower()
            for kw in ["study guide", "supersummary", "instaread"]
        )
    ]
    if non_summary:
        candidate_pool = non_summary

    best = max(candidate_pool, key=lambda d: d.get("edition_count", 0))
    return best


def format_openlibrary_result(doc: dict, fallback_pages: int = None) -> dict:
    """Format and clean fields from Open Library response."""
    subjects = clean_and_prioritize_subjects(
        raw_subjects=doc.get("subject"),
        places=doc.get("place"),
        times=doc.get("time"),
        limit=5,
    )

    raw_pages = doc.get("number_of_pages_median")
    pages_median = None
    if raw_pages is not None:
        try:
            val = int(raw_pages)
            if val < 25 and fallback_pages and fallback_pages >= 25:
                pages_median = int(fallback_pages)
            else:
                pages_median = val
        except (ValueError, TypeError):
            pages_median = int(fallback_pages) if fallback_pages else None
    elif fallback_pages:
        try:
            pages_median = int(fallback_pages)
        except (ValueError, TypeError):
            pages_median = None

    ratings_avg = None
    if doc.get("ratings_average") is not None:
        try:
            ratings_avg = round(float(doc["ratings_average"]), 2)
        except (ValueError, TypeError):
            ratings_avg = None

    return {
        "ol_key": doc.get("key"),
        "Average Rating": ratings_avg,
        "Number of Pages": pages_median,
        "Original Publication Year": doc.get("first_publish_year"),
        "cover_url": f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-M.jpg" if doc.get("cover_i") else None,
        "genres": ", ".join(subjects) if subjects else None,
    }


def enrich_catalog_dataframe(df: pd.DataFrame, progress_bar=None, status_text=None) -> (pd.DataFrame, int):
    """Enrich books with Open Library data (genres, pages, pub year, cover, rating, ol_key).
    
    Preserves any pre-existing user data: only fills columns that are empty / missing.
    """
    enriched_df = df.copy()

    # Ensure required enrichment columns exist
    for col in ["ol_key", "Average Rating", "Number of Pages", "Original Publication Year", "Year Published", "cover_url", "genres"]:
        if col not in enriched_df.columns:
            enriched_df[col] = None

    def is_blank(val) -> bool:
        """Check if a value is None, NaN, or whitespace-only."""
        if pd.isna(val) or val is None:
            return True
        if isinstance(val, str) and not val.strip():
            return True
        return False

    total = len(enriched_df)
    matched_count = 0

    for idx, row in enriched_df.iterrows():
        title = str(row.get("Title") or "").strip()
        author = str(row.get("Author") or "").strip()

        if status_text:
            status_text.text(f"Matching ({idx + 1}/{total}): {title} by {author}...")
        if progress_bar:
            progress_bar.progress((idx + 1) / total)

        if not title:
            continue

        fallback_pg = row.get("Number of Pages")
        try:
            fallback_pg = int(fallback_pg) if pd.notnull(fallback_pg) else None
        except Exception:
            fallback_pg = None

        doc = fetch_best_book_match(title, author)
        if doc:
            data = format_openlibrary_result(doc, fallback_pages=fallback_pg)
            matched_count += 1

            # Always save canonical Open Library work key
            if data.get("ol_key"):
                enriched_df.at[idx, "ol_key"] = data["ol_key"]

            # Save cover URL and genres
            if data.get("cover_url"):
                enriched_df.at[idx, "cover_url"] = data["cover_url"]
            if data.get("genres"):
                enriched_df.at[idx, "genres"] = data["genres"]

            # PRESERVE USER DATA: Only fill if currently missing / blank
            if is_blank(row.get("Number of Pages")) and data.get("Number of Pages") is not None:
                enriched_df.at[idx, "Number of Pages"] = data["Number of Pages"]

            if data.get("Original Publication Year") is not None:
                if is_blank(row.get("Original Publication Year")):
                    enriched_df.at[idx, "Original Publication Year"] = data["Original Publication Year"]
                if is_blank(row.get("Year Published")):
                    enriched_df.at[idx, "Year Published"] = data["Original Publication Year"]

            if is_blank(row.get("Average Rating")) and data.get("Average Rating") is not None:
                enriched_df.at[idx, "Average Rating"] = data["Average Rating"]

    return enriched_df, matched_count


# ── Database Helpers ──────────────────────────────────────────────────────────
def df_to_sqlite(df: pd.DataFrame, db_name=DB_NAME, table_name=TABLE_NAME):
    """Store a DataFrame in SQLite table, replacing existing records."""
    if "My Rating" in df.columns:
        df["My Rating"] = pd.to_numeric(df["My Rating"], errors="coerce").astype(float)
    conn = sqlite3.connect(db_name)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()
    return df


def csv_to_sqlite(csv_path, db_name=DB_NAME, table_name=TABLE_NAME):
    """Read a CSV file and store its contents in SQLite."""
    df = pd.read_csv(csv_path)
    return df_to_sqlite(df, db_name=db_name, table_name=table_name)


def load_table(db_name=DB_NAME, table_name=TABLE_NAME):
    """Load the full SQLite table into a DataFrame."""
    conn = sqlite3.connect(db_name)
    df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
    conn.close()
    if "My Rating" in df.columns:
        df["My Rating"] = pd.to_numeric(df["My Rating"], errors="coerce").astype(float)
    return df


def table_has_data(db_name=DB_NAME, table_name=TABLE_NAME) -> bool:
    """Check if the SQLite database exists, contains the table, and has rows."""
    if not os.path.exists(db_name):
        return False
    try:
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        table_exists = cursor.fetchone() is not None
        if not table_exists:
            conn.close()
            return False
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def save_table(df, db_name=DB_NAME, table_name=TABLE_NAME):
    """Write an edited DataFrame back to SQLite."""
    if "My Rating" in df.columns:
        df["My Rating"] = pd.to_numeric(df["My Rating"], errors="coerce").astype(float)
    conn = sqlite3.connect(db_name)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()


def table_is_enriched(db_name=DB_NAME, table_name=TABLE_NAME) -> bool:
    """Check if table has Open Library enrichment data (ol_key or genres populated)."""
    if not table_has_data(db_name, table_name):
        return False
    try:
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        cols = [c[1] for c in cursor.fetchall()]
        if "ol_key" not in cols and "genres" not in cols:
            conn.close()
            return False
        cursor.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE (ol_key IS NOT NULL AND TRIM(ol_key) != '') OR (genres IS NOT NULL AND TRIM(genres) != '')"
        )
        cnt = cursor.fetchone()[0]
        conn.close()
        return cnt > 0
    except Exception:
        return False


# ── Wrapped Stats Helpers ─────────────────────────────────────────────────────
def fetch_wrapped_stats(db_name=DB_NAME, table_name=TABLE_NAME) -> Dict[str, object]:
    """Execute the 5 Wrapped stats SQL queries from NextChapterDev.ipynb."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    if not cursor.fetchone():
        conn.close()
        return {
            "total_books": 0,
            "top_rated": pd.DataFrame(),
            "top_genres": pd.DataFrame(),
            "longest_book": pd.DataFrame(),
            "enjoyed_genres": pd.DataFrame(),
        }

    cursor.execute(f"PRAGMA table_info({table_name})")
    cols = [col[1] for col in cursor.fetchall()]
    cover_col = (
        "CASE WHEN cover_url IS NOT NULL AND cover_url != '' THEN cover_url ELSE NULL END AS cover_url"
        if "cover_url" in cols
        else "NULL AS cover_url"
    )

    # 1. Number of books read this year
    q1 = f"SELECT COUNT(DISTINCT Title) as total_books FROM {table_name} WHERE Title IS NOT NULL AND TRIM(Title) != ''"
    df1 = pd.read_sql_query(q1, conn)
    total_books = int(df1.iloc[0]["total_books"]) if not df1.empty and pd.notnull(df1.iloc[0]["total_books"]) else 0

    # 2. Highest rated books this year (returns all tied for top rating)
    if "My Rating" in cols:
        q2 = f"""
        SELECT Title, Author, [My Rating], {cover_col}
        FROM {table_name}
        WHERE [My Rating] = (SELECT MAX([My Rating]) FROM {table_name} WHERE [My Rating] IS NOT NULL)
          AND [My Rating] IS NOT NULL
        """
        df_top_rated = pd.read_sql_query(q2, conn)
    else:
        df_top_rated = pd.DataFrame()

    # 3. Most read genres this year (Top 3 by book count, ignoring generic 'fiction' if specific genres exist)
    q3 = f"""
    WITH SplitGenres AS (
        SELECT 
            Title,
            genres,
            TRIM(SUBSTR(genres, 1, INSTR(genres || ',', ',') - 1)) AS genre_1,
            CASE 
                WHEN INSTR(genres, ',') > 0 THEN 
                    TRIM(SUBSTR(
                        SUBSTR(genres, INSTR(genres, ',') + 1), 
                        1, 
                        INSTR(SUBSTR(genres, INSTR(genres, ',') + 1) || ',', ',') - 1
                    ))
                ELSE ''
            END AS genre_2,
            CASE 
                WHEN INSTR(SUBSTR(genres, INSTR(genres, ',') + 1), ',') > 0 THEN 
                    TRIM(SUBSTR(
                        SUBSTR(SUBSTR(genres, INSTR(genres, ',') + 1), INSTR(SUBSTR(genres, INSTR(genres, ',') + 1), ',') + 1),
                        1,
                        INSTR(SUBSTR(SUBSTR(genres, INSTR(genres, ',') + 1), INSTR(SUBSTR(genres, INSTR(genres, ',') + 1), ',') + 1) || ',', ',') - 1
                    ))
                ELSE ''
            END AS genre_3
        FROM {table_name}
        WHERE genres IS NOT NULL AND TRIM(genres) != ''
    ),
    CandidateGenres AS (
        SELECT 
            Title,
            genres,
            CASE 
                WHEN LOWER(genre_1) IN ('fiction', 'general', 'fiction / general') THEN
                    CASE 
                        WHEN genre_2 != '' AND LOWER(genre_2) NOT IN ('fiction', 'general', 'fiction / general') THEN genre_2
                        WHEN genre_3 != '' AND LOWER(genre_3) NOT IN ('fiction', 'general', 'fiction / general') THEN genre_3
                        WHEN genre_2 != '' THEN genre_2
                        ELSE genre_1
                    END
                ELSE genre_1
            END AS candidate_genre
        FROM SplitGenres
    ),
    PrimaryGenres AS (
        SELECT 
            Title,
            CASE 
                WHEN LOWER(candidate_genre) LIKE 'fiction / %' THEN TRIM(SUBSTR(candidate_genre, 11))
                ELSE candidate_genre
            END AS primary_genre
        FROM CandidateGenres
    )
    SELECT 
        primary_genre AS Genre,
        COUNT(*) AS [Books Read]
    FROM PrimaryGenres
    WHERE primary_genre != ''
    GROUP BY primary_genre
    ORDER BY [Books Read] DESC
    LIMIT 3;
    """
    df_top_genres = pd.read_sql_query(q3, conn) if "genres" in cols else pd.DataFrame()

    # 4. Longest book read this year (handles ties, integer cast)
    if "Number of Pages" in cols:
        q4 = f"""
        SELECT 
            Title,
            Author,
            CAST([Number of Pages] AS INTEGER) AS Pages,
            {cover_col}
        FROM {table_name}
        WHERE [Number of Pages] = (
            SELECT MAX([Number of Pages]) 
            FROM {table_name} 
            WHERE [Number of Pages] IS NOT NULL AND [Number of Pages] > 0
        )
        """
        df_longest = pd.read_sql_query(q4, conn)
    else:
        df_longest = pd.DataFrame()

    # 5. Most enjoyed genre this year (Top ranked by average rating, ignoring generic 'fiction' if specific genres exist)
    if "genres" in cols and "My Rating" in cols:
        q5 = f"""
        WITH SplitGenres AS (
            SELECT 
                Title,
                Author,
                [My Rating],
                genres,
                {cover_col},
                TRIM(SUBSTR(genres, 1, INSTR(genres || ',', ',') - 1)) AS genre_1,
                CASE 
                    WHEN INSTR(genres, ',') > 0 THEN 
                        TRIM(SUBSTR(
                            SUBSTR(genres, INSTR(genres, ',') + 1), 
                            1, 
                            INSTR(SUBSTR(genres, INSTR(genres, ',') + 1) || ',', ',') - 1
                        ))
                    ELSE ''
                END AS genre_2,
                CASE 
                    WHEN INSTR(SUBSTR(genres, INSTR(genres, ',') + 1), ',') > 0 THEN 
                        TRIM(SUBSTR(
                            SUBSTR(SUBSTR(genres, INSTR(genres, ',') + 1), INSTR(SUBSTR(genres, INSTR(genres, ',') + 1), ',') + 1),
                            1,
                            INSTR(SUBSTR(SUBSTR(genres, INSTR(genres, ',') + 1), INSTR(SUBSTR(genres, INSTR(genres, ',') + 1), ',') + 1) || ',', ',') - 1
                        ))
                    ELSE ''
                END AS genre_3
            FROM {table_name}
            WHERE genres IS NOT NULL AND TRIM(genres) != '' AND [My Rating] IS NOT NULL
        ),
        CandidateGenres AS (
            SELECT 
                Title,
                Author,
                [My Rating],
                cover_url,
                genres,
                CASE 
                    WHEN LOWER(genre_1) IN ('fiction', 'general', 'fiction / general') THEN
                        CASE 
                            WHEN genre_2 != '' AND LOWER(genre_2) NOT IN ('fiction', 'general', 'fiction / general') THEN genre_2
                            WHEN genre_3 != '' AND LOWER(genre_3) NOT IN ('fiction', 'general', 'fiction / general') THEN genre_3
                            WHEN genre_2 != '' THEN genre_2
                            ELSE genre_1
                        END
                    ELSE genre_1
                END AS candidate_genre
            FROM SplitGenres
        ),
        PrimaryGenres AS (
            SELECT 
                Title,
                Author,
                [My Rating],
                cover_url,
                CASE 
                    WHEN LOWER(candidate_genre) LIKE 'fiction / %' THEN TRIM(SUBSTR(candidate_genre, 11))
                    ELSE candidate_genre
                END AS primary_genre
            FROM CandidateGenres
        ),
        GenreAgg AS (
            SELECT 
                primary_genre AS Genre,
                ROUND(AVG([My Rating]), 2) AS [Average Rating],
                COUNT(*) AS [Books Read],
                MAX(cover_url) AS cover_url
            FROM PrimaryGenres
            WHERE primary_genre != ''
            GROUP BY primary_genre
        ),
        DistinctGenreBooks AS (
            SELECT DISTINCT
                primary_genre,
                Title || CASE WHEN Author IS NOT NULL AND TRIM(Author) != '' THEN ' by ' || Author ELSE '' END AS book_title
            FROM PrimaryGenres
            WHERE primary_genre != ''
        )
        SELECT 
            g.Genre,
            g.[Average Rating],
            g.[Books Read],
            (SELECT GROUP_CONCAT(b.book_title, ' • ') FROM DistinctGenreBooks b WHERE b.primary_genre = g.Genre) AS [Book Titles],
            g.cover_url,
            DENSE_RANK() OVER (ORDER BY g.[Average Rating] DESC, g.[Books Read] DESC) AS [Overall Rating Rank]
        FROM GenreAgg g
        ORDER BY [Overall Rating Rank]
        LIMIT 3;
        """
        df_enjoyed_genres = pd.read_sql_query(q5, conn)
    else:
        df_enjoyed_genres = pd.DataFrame()

    conn.close()
    return {
        "total_books": total_books,
        "top_rated": df_top_rated,
        "top_genres": df_top_genres,
        "longest_book": df_longest,
        "enjoyed_genres": df_enjoyed_genres,
    }


def render_safe_html(html_str: str):
    """Strip leading/trailing indentation from each line so CommonMark markdown parser
    does not convert indented tags into code blocks (<pre><code>)."""
    cleaned = "\n".join(line.strip() for line in html_str.strip().splitlines() if line.strip())
    st.markdown(cleaned, unsafe_allow_html=True)


def generate_wrapped_poster_image(stats: Dict[str, object], archetype: str, archetype_desc: str) -> bytes:
    """Generate a sleek high-res (920x840) reading recap poster image without emojis or truncated text."""
    w, h = 920, 840
    img = Image.new("RGB", (w, h), color=(15, 15, 20))
    draw = ImageDraw.Draw(img)

    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    if not os.path.exists(font_path):
        font_path = "/System/Library/Fonts/Helvetica.ttc"

    try:
        f_badge = ImageFont.truetype(font_path, 15)
        f_title = ImageFont.truetype(font_path, 34)
        f_sub = ImageFont.truetype(font_path, 16)
        f_card_title = ImageFont.truetype(font_path, 13)
        f_stat = ImageFont.truetype(font_path, 28)
        f_desc = ImageFont.truetype(font_path, 14)
    except Exception:
        f_badge = f_title = f_sub = f_card_title = f_stat = f_desc = ImageFont.load_default()

    def clean_text_no_emojis(txt: str) -> str:
        """Strip all emojis and non-ASCII characters to guarantee clean font rendering without missing-glyph boxes."""
        if not txt:
            return ""
        cleaned = re.sub(r"[^\x20-\x7E]+", " ", str(txt))
        return re.sub(r"\s+", " ", cleaned).strip()

    def fit_text_lines(text: str, font, max_width: int, max_lines: int = 2) -> List[str]:
        """Wrap text by words to fit within max_width across up to max_lines. If still too long, add ellipsis."""
        words = text.split()
        lines = []
        curr = ""
        for word in words:
            test = f"{curr} {word}".strip()
            if draw.textlength(test, font=font) <= max_width:
                curr = test
            else:
                if curr:
                    lines.append(curr)
                curr = word
        if curr:
            lines.append(curr)

        if len(lines) > max_lines:
            lines = lines[:max_lines]
            while lines[-1] and draw.textlength(lines[-1] + "...", font=font) > max_width:
                lines[-1] = lines[-1][:-1].strip()
            lines[-1] += "..."
        return lines

    # Header Card
    draw.rounded_rectangle([(40, 28), (w - 40, 138)], radius=16, fill=(24, 24, 32), outline=(45, 45, 55), width=1)
    draw.text((60, 46), "ANNUAL READING RECAP", font=f_badge, fill=(59, 130, 246))
    draw.text((60, 68), "Your Reading Wrapped", font=f_title, fill=(255, 255, 255))
    draw.text((60, 108), "Your Year in Books - Personalized Reading Stats", font=f_sub, fill=(180, 180, 190))

    def draw_panel(x1, y1, x2, y2, bg_color, badge, stat, subtitle, note):
        draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=18, fill=bg_color)
        px = x1 + 24
        max_w = (x2 - x1) - 48

        # 1. Badge
        draw.text((px, y1 + 16), clean_text_no_emojis(badge).upper(), font=f_card_title, fill=(255, 255, 255))

        # 2. Big Stat
        stat_clean = clean_text_no_emojis(stat)
        draw.text((px, y1 + 38), stat_clean, font=f_stat, fill=(255, 255, 255))

        # 3. Subtitle (wrapped to max 2 lines, no arbitrary slicing)
        sub_clean = clean_text_no_emojis(subtitle)
        sub_lines = fit_text_lines(sub_clean, f_desc, max_w, max_lines=2)
        y_sub = y1 + 76
        for i, line in enumerate(sub_lines):
            draw.text((px, y_sub + (i * 18)), line, font=f_desc, fill=(255, 255, 255))

        # 4. Note (wrapped cleanly, positioned below subtitle)
        y_note = y_sub + (len(sub_lines) * 18) + 6
        note_clean = clean_text_no_emojis(note)
        note_lines = fit_text_lines(note_clean, f_card_title, max_w, max_lines=1 if len(sub_lines) > 1 else 2)
        for i, line in enumerate(note_lines):
            draw.text((px, y_note + (i * 16)), line, font=f_card_title, fill=(225, 225, 235))

    # Panel 1: Total Books (Left)
    draw_panel(40, 155, 445, 315, (235, 51, 73), "Reading Volume", f"{stats['total_books']} Books Read", archetype, archetype_desc)

    # Panel 2: Highest Rated (Right)
    top_r = stats["top_rated"]
    r_stat = f"{top_r.iloc[0]['My Rating']} / 5 Stars" if not top_r.empty else "Unrated"
    r_sub = f"{top_r.iloc[0]['Title']} by {top_r.iloc[0]['Author']}" if not top_r.empty else "No rated books"
    draw_panel(475, 155, w - 40, 315, (248, 87, 166), "Top Rated Read", r_stat, r_sub, "Benchmark reads of the year")

    # Panel 3: Most Read Genre (Left)
    top_g = stats["top_genres"]
    g_stat = str(top_g.iloc[0]["Genre"]).title() if not top_g.empty else "None"
    g_sub = f"{int(top_g.iloc[0]['Books Read'])} books in this genre" if not top_g.empty else "Enrich to reveal"
    draw_panel(40, 332, 445, 492, (17, 153, 142), "Most Read Genre", g_stat, g_sub, "Your most visited literary world")

    # Panel 4: Longest Book (Right)
    long_b = stats["longest_book"]
    l_stat = f"{int(long_b.iloc[0]['Pages'])} Pages" if not long_b.empty and pd.notnull(long_b.iloc[0]["Pages"]) else "N/A"
    l_sub = f"{long_b.iloc[0]['Title']} by {long_b.iloc[0]['Author']}" if not long_b.empty else "Page count missing"
    draw_panel(475, 332, w - 40, 492, (101, 78, 163), "Longest Book Read", l_stat, l_sub, "An epic journey of endurance")

    # Panel 5: Highest Rated Genre (Full Width)
    enj_g = stats["enjoyed_genres"]
    e_stat = str(enj_g.iloc[0]["Genre"]).title() if not enj_g.empty else "N/A"
    if not enj_g.empty:
        r_avg = float(enj_g.iloc[0]["Average Rating"])
        b_titles = str(enj_g.iloc[0].get("Book Titles") or "").strip()
        first_title = b_titles.split(" • ")[0].strip() if " • " in b_titles else b_titles.split(",")[0].strip() if b_titles else ""
        e_sub = f"{r_avg:.2f} Avg Rating - {first_title}" if first_title else f"{r_avg:.2f} Avg Rating"
    else:
        e_sub = "Rate more to reveal"
    draw_panel(40, 509, w - 40, 669, (120, 60, 200), "Highest Rated Genre", e_stat, e_sub, "The genre that earned your highest praise")

    # Footer (clean ASCII, no emojis)
    draw.text((w // 2 - 110, h - 45), "Generated with NextChapter", font=f_sub, fill=(130, 130, 140))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_wrapped_card(
    badge: str,
    title: str,
    big_stat: str,
    subtitle: str,
    gradient: str,
    items_html: str = "",
    cover_url: str = None,
    extra_note: str = "",
) -> str:
    """Generate clean HTML for a Spotify Wrapped style card without leading line whitespace."""
    cover_img_html = ""
    if cover_url:
        cover_img_html = f'<div style="margin: 14px 0 10px 0;"><img src="{cover_url}" style="height: 140px; border-radius: 8px; box-shadow: 0 10px 20px rgba(0,0,0,0.35); object-fit: cover;" /></div>'

    raw_html = f"""<div style="background: {gradient}; border-radius: 22px; padding: 28px 22px; color: #ffffff; box-shadow: 0 14px 32px rgba(0,0,0,0.22); min-height: 380px; display: flex; flex-direction: column; justify-content: space-between; text-align: center; align-items: center; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; box-sizing: border-box;">
<div style="width: 100%;">
<div style="display: inline-block; padding: 4px 14px; background: rgba(255, 255, 255, 0.22); backdrop-filter: blur(6px); border-radius: 999px; font-size: 12px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; margin-bottom: 12px;">{html.escape(badge)}</div>
<h4 style="margin: 0 0 6px 0; font-size: 18px; font-weight: 600; opacity: 0.92; color: #ffffff;">{html.escape(title)}</h4>
<div style="font-size: 38px; font-weight: 900; line-height: 1.1; margin: 10px 0; color: #ffffff; text-shadow: 0 2px 10px rgba(0,0,0,0.25);">{big_stat}</div>
{cover_img_html}
<p style="margin: 4px 0 12px 0; font-size: 15px; font-weight: 500; opacity: 0.95; color: #ffffff;">{html.escape(subtitle)}</p>
{items_html}
</div>
<div style="font-size: 13px; opacity: 0.78; font-style: italic; margin-top: 10px; color: #ffffff;">{html.escape(extra_note)}</div>
</div>"""
    return "\n".join(line.strip() for line in raw_html.strip().splitlines() if line.strip())


def render_wrapped_section(stats: Dict[str, object]):
    """Render the Wrapped interactive stories, summary poster, and social sharing."""
    render_safe_html(
        """
        <div style="text-align: center; margin: 35px 0 25px 0;">
            <div style="display: inline-block; padding: 6px 18px; background: #2563EB; color: #ffffff; font-size: 13px; font-weight: 800; border-radius: 999px; letter-spacing: 1.5px; margin-bottom: 8px;">
                ANNUAL RECAP
            </div>
            <h2 style="font-size: 2.3rem; font-weight: 800; margin: 0; color: inherit;">
                Your Reading Wrapped!
            </h2>
            <p style="font-size: 1.05rem; opacity: 0.8; margin-top: 6px;">
                Your literary journey, parsed into your signature reading stats.
            </p>
        </div>
        """
    )

    tab_story, tab_grid = st.tabs([
        "Wrapped Stories (Interactive)",
        "All Stats Cards (Grid View)",
    ])

    total_b = stats["total_books"]
    df_top_r = stats["top_rated"]
    df_top_g = stats["top_genres"]
    df_long = stats["longest_book"]
    df_enjoy_g = stats["enjoyed_genres"]

    # ── Reader Persona Archetype ──────────────────────────────────────────────
    if total_b >= 30:
        archetype = "👑 The Literary Maven"
        archetype_desc = "Devoured books like oxygen this year. True literary royalty."
    elif total_b >= 15:
        archetype = "📖 The Voracious Bookworm"
        archetype_desc = "Always turning pages, always seeking the next chapter."
    elif total_b >= 6:
        archetype = "☕ The Intentional Reader"
        archetype_desc = "Every read was chosen with care, building a rich personal library."
    elif total_b >= 1:
        archetype = "🌱 The Curious Explorer"
        archetype_desc = "Quality over quantity. Every journey made a lasting mark."
    else:
        archetype = "📚 The New Reader"
        archetype_desc = "Your reading journey is just beginning."

    # ── Format Data for Card 1 (Total Books) ──────────────────────────────────
    c1_html = render_wrapped_card(
        badge="Slide 1 of 5 • Reading Odyssey",
        title="YOUR READING VOLUME",
        big_stat=f"{total_b} Books",
        subtitle=archetype,
        gradient="linear-gradient(135deg, #FF0844 0%, #FFB199 100%)",
        extra_note=archetype_desc,
    )

    # ── Format Data for Card 2 (Top Rated) ────────────────────────────────────
    c2_cover = None
    if not df_top_r.empty:
        max_rating = df_top_r.iloc[0]["My Rating"]
        top_titles = [f"★ {r['Title']} by {r['Author']}" for _, r in df_top_r.iterrows()]
        c2_big = f"★ {max_rating} / 5"
        c2_sub = f"{len(df_top_r)} Top-Rated Read{'s' if len(df_top_r) > 1 else ''}"
        c2_items = "<div style='font-size: 14px; text-align: left; background: rgba(0,0,0,0.2); padding: 10px 14px; border-radius: 12px; margin-top: 10px; max-height: 120px; overflow-y: auto;'>"
        for t in top_titles:
            c2_items += f"<div style='margin-bottom: 4px;'>{html.escape(t)}</div>"
        c2_items += "</div>"
        if "cover_url" in df_top_r.columns and pd.notnull(df_top_r.iloc[0]["cover_url"]):
            c2_cover = str(df_top_r.iloc[0]["cover_url"])
    else:
        c2_big = "Unrated"
        c2_sub = "No ratings entered yet"
        c2_items = ""

    c2_html = render_wrapped_card(
        badge="Slide 2 of 5 • The Crown Jewels",
        title="YOUR HIGHEST RATED",
        big_stat=c2_big,
        subtitle=c2_sub,
        gradient="linear-gradient(135deg, #F857A6 0%, #FF5858 100%)",
        items_html=c2_items,
        cover_url=c2_cover,
        extra_note="The reads that set the benchmark this year.",
    )

    # ── Format Data for Card 3 (Most Read Genres) ─────────────────────────────
    if not df_top_g.empty:
        top_genre_name = str(df_top_g.iloc[0]["Genre"]).title()
        c3_big = top_genre_name
        c3_sub = f"{int(df_top_g.iloc[0]['Books Read'])} books in this genre"
        c3_items = "<div style='font-size: 14px; text-align: left; background: rgba(0,0,0,0.2); padding: 10px 14px; border-radius: 12px; margin-top: 10px;'>"
        for idx, r in df_top_g.iterrows():
            medal = ["🥇", "🥈", "🥉"][idx] if idx < 3 else "•"
            c3_items += f"<div style='margin-bottom: 4px;'><b>{medal} #{idx+1} {html.escape(str(r['Genre']).title())}</b> — {int(r['Books Read'])} book(s)</div>"
        c3_items += "</div>"
    else:
        c3_big = "No Genres"
        c3_sub = "Enrich with Open Library to uncover genres"
        c3_items = ""

    c3_html = render_wrapped_card(
        badge="Slide 3 of 5 • Genre Obsession",
        title="MOST READ GENRE",
        big_stat=c3_big,
        subtitle=c3_sub,
        gradient="linear-gradient(135deg, #11998E 0%, #38EF7D 100%)",
        items_html=c3_items,
        extra_note="The literary world you couldn't stay away from.",
    )

    # ── Format Data for Card 4 (Longest Book) ─────────────────────────────────
    c4_cover = None
    if not df_long.empty and pd.notnull(df_long.iloc[0]["Pages"]):
        max_pages = int(df_long.iloc[0]["Pages"])
        book_title = str(df_long.iloc[0]["Title"])
        book_author = str(df_long.iloc[0]["Author"])
        c4_big = f"{max_pages} Pages"
        c4_sub = f"{book_title} by {book_author}"
        if "cover_url" in df_long.columns and pd.notnull(df_long.iloc[0]["cover_url"]):
            c4_cover = str(df_long.iloc[0]["cover_url"])
    else:
        c4_big = "—"
        c4_sub = "Page numbers missing"

    c4_html = render_wrapped_card(
        badge="Slide 4 of 5 • The Marathon Read",
        title="YOUR LONGEST BOOK",
        big_stat=c4_big,
        subtitle=c4_sub,
        gradient="linear-gradient(135deg, #654EA3 0%, #EAAFC8 100%)",
        cover_url=c4_cover,
        extra_note="A journey of endurance and imagination.",
    )

    # ── Format Data for Card 5 (Most Enjoyed Genre) ───────────────────────────
    c5_cover = None
    c5_items = ""
    if not df_enjoy_g.empty:
        top_enjoyed = str(df_enjoy_g.iloc[0]["Genre"]).title()
        avg_score = float(df_enjoy_g.iloc[0]["Average Rating"])
        b_count = int(df_enjoy_g.iloc[0]["Books Read"])
        raw_titles = str(df_enjoy_g.iloc[0].get("Book Titles") or "").strip()
        c5_big = top_enjoyed

        c5_sub = f"★ {avg_score:.2f} Avg Rating"
        if raw_titles:
            delimiter = " • " if " • " in raw_titles else ","
            titles_list = [t.strip() for t in raw_titles.split(delimiter) if t.strip()]
            c5_items = "<div style='font-size: 14px; text-align: left; background: rgba(0,0,0,0.2); padding: 10px 14px; border-radius: 12px; margin-top: 10px; max-height: 120px; overflow-y: auto;'>"
            for t in titles_list:
                c5_items += f"<div style='margin-bottom: 4px;'>{html.escape(t)}</div>"
            c5_items += "</div>"

        if "cover_url" in df_enjoy_g.columns and pd.notnull(df_enjoy_g.iloc[0]["cover_url"]):
            c5_cover = str(df_enjoy_g.iloc[0]["cover_url"])
    else:
        c5_big = "—"
        c5_sub = "Rate more books to reveal your top genre"

    c5_html = render_wrapped_card(
        badge="Slide 5 of 5 • Genre Soulmate",
        title="HIGHEST RATED GENRE",
        big_stat=c5_big,
        subtitle=c5_sub,
        gradient="linear-gradient(135deg, #2E0854 0%, #9B51E0 100%)",
        items_html=c5_items,
        cover_url=c5_cover,
        extra_note="The genre that consistently earned your highest praise.",
    )

    cards = [c1_html, c2_html, c3_html, c4_html, c5_html]

    # Generate downloadable poster bytes
    poster_png_bytes = generate_wrapped_poster_image(stats, archetype, archetype_desc)

    # ── TAB 1: Story Mode ─────────────────────────────────────────────────────
    with tab_story:
        current_idx = st.session_state.get("wrapped_slide", 0)
        current_idx = max(0, min(current_idx, len(cards) - 1))

        # Progress bar indicators
        progress_cols_html = "<div style='display: flex; gap: 8px; margin-bottom: 20px;'>"
        for i in range(len(cards)):
            opacity = "1.0" if i == current_idx else "0.3"
            color = "#3B82F6" if i == current_idx else "#ffffff"
            progress_cols_html += f"<div style='flex: 1; height: 5px; border-radius: 4px; background: {color}; opacity: {opacity}; transition: all 0.3s;'></div>"
        progress_cols_html += "</div>"
        render_safe_html(progress_cols_html)

        # Center the active story card
        c_left, c_center, c_right = st.columns([1, 2.2, 1])
        with c_center:
            render_safe_html(cards[current_idx])

            # Story navigation buttons
            st.markdown("<div style='margin-top: 18px;'></div>", unsafe_allow_html=True)
            nav1, nav2, nav3 = st.columns([1, 1.4, 1])

            with nav1:
                if st.button("◀ Previous", key="btn_prev_slide", disabled=(current_idx == 0)):
                    st.session_state["wrapped_slide"] = current_idx - 1
                    st.rerun()

            with nav2:
                st.markdown(
                    f"<p style='text-align: center; margin-top: 6px; font-weight: 600; font-size: 14px;'>Story {current_idx + 1} of {len(cards)}</p>",
                    unsafe_allow_html=True,
                )

            with nav3:
                if current_idx < len(cards) - 1:
                    if st.button("Next ▶", key="btn_next_slide", type="primary"):
                        st.session_state["wrapped_slide"] = current_idx + 1
                        st.rerun()
                else:
                    if st.button("↺ Replay", key="btn_replay_slide", type="primary"):
                        st.session_state["wrapped_slide"] = 0
                        st.rerun()

            st.markdown("<div style='margin-top: 24px;'></div>", unsafe_allow_html=True)
            st.download_button(
                label="📥 Download Wrapped Poster (PNG)",
                data=poster_png_bytes,
                file_name="NextChapter_Reading_Wrapped.png",
                mime="image/png",
                use_container_width=True,
                key="btn_download_poster_story",
            )

    # ── TAB 2: Grid View (All Cards Poster) ───────────────────────────────────
    with tab_grid:
        st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
        row1_col1, row1_col2 = st.columns(2)
        with row1_col1:
            render_safe_html(c1_html)
        with row1_col2:
            render_safe_html(c2_html)

        st.markdown("<div style='margin-top: 20px;'></div>", unsafe_allow_html=True)

        row2_col1, row2_col2, row2_col3 = st.columns(3)
        with row2_col1:
            render_safe_html(c3_html)
        with row2_col2:
            render_safe_html(c4_html)
        with row2_col3:
            render_safe_html(c5_html)

        st.markdown("<div style='margin-top: 25px;'></div>", unsafe_allow_html=True)
        st.download_button(
            label="📥 Download Wrapped Poster (PNG)",
            data=poster_png_bytes,
            file_name="NextChapter_Reading_Wrapped.png",
            mime="image/png",
            type="primary",
            key="btn_download_poster_grid",
        )


def is_children_book(subjects: list) -> bool:
    if not subjects:
        return False
    juvenile_terms = (
        "juvenile", "children's", "child and youth",
        "middle grade", "picture book", "elementary school", "early reader"
    )
    return any(any(term in str(s).lower() for term in juvenile_terms) for s in subjects)


def clean_book_title(title: str) -> str:
    if not title or not isinstance(title, str):
        return ""
    base = re.split(r" - | \(|: | by ", title, flags=re.IGNORECASE)[0].strip()
    nfkd = unicodedata.normalize("NFKD", base)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    clean = re.sub(r"[^\w\s]", "", no_accents).lower()
    return re.sub(r"\s+", " ", clean).strip()


def calculate_length_fit(pages: Optional[float], p25: float = 300.0, p75: float = 500.0) -> float:
    if not pages or pd.isna(pages) or pages <= 0:
        return 0.5
    if p25 <= pages <= p75:
        return 1.0
    if pages < p25:
        return max(0.0, 1.0 - (p25 - pages) / 200.0)
    return max(0.0, 1.0 - (pages - p75) / 400.0)


def calculate_era_fit(pub_year: Optional[float], target_era: float = 2020.0, era_spread: float = 15.0) -> float:
    """
    Computes an era affinity fit score between 0.0 and 1.0 using smooth Gaussian decay.
    Books published near the reader's preferred era receive scores close to 1.0,
    smoothly decaying for books from eras the reader rarely explores.
    """
    if not pub_year or pd.isna(pub_year) or pub_year <= 0:
        return 0.5  # Neutral fallback when publication year is unlisted
    diff = abs(float(pub_year) - float(target_era))
    spread = max(10.0, float(era_spread))
    return round(max(0.0, min(1.0, math.exp(- (diff ** 2) / (2.0 * (spread ** 2))))), 3)


SPANISH_WORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "en", "con",
    "por", "para", "sin", "sobre", "entre", "como", "pero", "mas", "más", "este", "esta",
    "estos", "estas", "su", "sus", "vida", "muerte", "amor", "tiempo", "años", "cuento",
    "historia", "crónica", "novela", "libro", "peseta", "pesetas", "euro", "euros", "sol",
    "sombra", "cien", "soledad", "ficciones",
}
ITALIAN_WORDS = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "del", "dello", "della",
    "dei", "degli", "delle", "nel", "nella", "nei", "negli", "nelle", "sul", "sulla", "da",
    "per", "tra", "fra", "con", "ed", "non", "che", "sono", "tutti", "tutto", "amore", "morte",
    "storia", "libro", "vita", "lira", "lire", "euro", "mondo",
}
FRENCH_WORDS = {
    "le", "la", "les", "des", "du", "de", "dans", "avec", "pour", "sur", "un", "une", "qui",
    "que", "est", "mort", "histoire", "livre", "vie", "franc", "francs", "centime", "centimes",
    "france", "paris", "chambre", "madame", "monsieur", "homme", "femme", "soleil", "jour",
    "nuit", "temps", "petit", "petite", "grand", "grande", "belle", "beau", "chateau", "coeur",
    "ville", "rue", "monde", "yeux", "etranger", "prince", "euro", "euros",
}
GERMAN_WORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines", "und", "mit",
    "für", "von", "aus", "auf", "nicht", "buch", "leben", "mark", "pfennig", "zeit", "welt",
}
ENG_WORDS = {
    "the", "a", "an", "and", "of", "in", "to", "for", "with", "on", "at", "from", "by",
    "about", "as", "into", "like", "through", "after", "over", "between", "out", "against",
    "during", "without", "before", "under", "around", "among", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "but", "if", "or",
    "because", "until", "while", "my", "your", "his", "her", "its", "our", "their", "this",
    "that", "these", "those", "one", "two", "three", "first", "second", "last", "good",
    "great", "little", "big", "world", "man", "woman", "life", "time", "day", "night",
    "house", "girl", "boy", "eyes", "eye", "secret", "death", "story", "book", "love",
    "heart", "shadow", "winter", "summer", "fall", "spring", "road", "sea", "sky",
    "wind", "sun", "moon", "star", "stars", "garden", "city", "water", "fire", "dark", "light",
}


FORBIDDEN_CHARS = set(
    "¿¡ßñÑ"
    "ōūāīēŌŪĀĪĒ"  # Macrons (Japanese Hepburn romanization, etc.)
    "łŁżźćńśąę"   # Polish
    "čšžřňďť"     # Czech/Slovak
    "őűů"         # Hungarian/Czech
    "øØæÆåÅ"      # Nordic
    "äöüÄÖÜ"      # German umlauts
    "ãõÃÕ"        # Portuguese
    "ğĞıİşŞ"      # Turkish
    "ășțĂȘȚ"      # Romanian
    "àâîôûÀÂÎÔÛ"  # French circumflex/grave on vowels
    "áíúÁÍÚ"      # Spanish/Portuguese acutes
)

NON_LATIN_RANGES = [
    (0x0400, 0x04FF),  # Cyrillic
    (0x0500, 0x052F),  # Cyrillic Supplementary
    (0x0600, 0x06FF),  # Arabic
    (0x0590, 0x05FF),  # Hebrew
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x0900, 0x097F),  # Devanagari
    (0x0E00, 0x0E7F),  # Thai
    (0x0370, 0x03FF),  # Greek
]

JAPANESE_ROMANIZED_TOKENS = {
    "kun", "chan", "san", "sama", "sensei", "senpai", "joshikai", "monogatari",
    "shoujo", "shonen", "manga", "otaku", "isekai", "akkochan"
}


def detect_catalog_languages(catalog_df: pd.DataFrame) -> (Set[str], bool):
    """
    Detects user's reading languages from catalog titles and metadata.
    Returns:
        (detected_languages, is_all_english)
        e.g. ({'eng'}, True) or ({'eng', 'spa'}, False)
    """
    if catalog_df is None or catalog_df.empty:
        return {"eng"}, True

    detected_langs = set()
    
    # 1. Check explicit language column if present
    if "language" in catalog_df.columns:
        for val in catalog_df["language"].dropna():
            val_str = str(val).lower()
            for code in ["eng", "spa", "fre", "fra", "ger", "deu", "ita", "por", "pol", "rus", "jpn"]:
                if code in val_str:
                    canonical = "fre" if code == "fra" else ("ger" if code == "deu" else code)
                    detected_langs.add(canonical)

    # 2. Linguistic analysis of catalog titles
    titles = catalog_df["Title"].dropna().tolist() if "Title" in catalog_df.columns else []
    
    has_spanish, has_french, has_german, has_italian, has_english = False, False, False, False, False

    for t in titles:
        raw = str(t).strip()
        words = [re.sub(r"[^\w]", "", w).lower() for w in raw.split()]
        words = [w for w in words if w]
        if not words:
            continue

        sp_hits = sum(1 for w in words if w in SPANISH_WORDS) or any(c in raw for c in ["¿", "¡", "ñ", "Ñ"])
        fr_hits = sum(1 for w in words if w in FRENCH_WORDS) or any(w in words for w in ["yeux", "etranger", "prince"])
        de_hits = sum(1 for w in words if w in GERMAN_WORDS) or any(c in raw for c in ["ß", "ä", "ö", "ü"])
        it_hits = sum(1 for w in words if w in ITALIAN_WORDS)
        eng_hits = sum(1 for w in words if w in ENG_WORDS)

        if sp_hits >= 2 or (len(words) >= 2 and words[0] in {"el", "los", "del"} and eng_hits == 0) or any(c in raw for c in ["¿", "¡", "ñ", "Ñ"]):
            has_spanish = True
        elif fr_hits >= 2 or (len(words) >= 2 and words[0] in {"les", "des"} and eng_hits == 0):
            has_french = True
        elif de_hits >= 2 or (len(words) >= 2 and words[0] in {"der", "die", "das"} and eng_hits == 0):
            has_german = True
        elif it_hits >= 2 or (len(words) >= 2 and words[0] in {"il", "lo", "gli"} and eng_hits == 0):
            has_italian = True
        else:
            has_english = True

    if has_spanish:
        detected_langs.add("spa")
    if has_french:
        detected_langs.add("fre")
    if has_german:
        detected_langs.add("ger")
    if has_italian:
        detected_langs.add("ita")
    if has_english or not detected_langs:
        detected_langs.add("eng")

    is_all_english = (detected_langs == {"eng"})
    return detected_langs, is_all_english


def check_title_and_translation(
    title: str,
    doc_languages: Optional[List] = None,
    allowed_languages: Optional[Set[str]] = None
) -> Tuple[bool, bool]:
    """
    Validates candidate language and detects English translation availability.
    Returns:
        (is_allowed: bool, is_translation: bool)
    If a book was originally written in another language (e.g. French, German, Japanese)
    but has an English translation logged in Open Library, it is welcomed and flagged
    with is_translation=True so the reader is clearly informed.
    """
    if not title or not isinstance(title, str):
        return False, False
    raw_title = title.strip()
    if not raw_title:
        return False, False

    langs = [str(l).lower() for l in doc_languages] if isinstance(doc_languages, list) else ([str(doc_languages).lower()] if doc_languages else [])
    has_eng = any(l in {"eng", "en", "english"} for l in langs)
    has_foreign_meta = any(l in {
        "spa", "ita", "fre", "fra", "ger", "deu", "rus", "chi", "zho",
        "jpn", "kor", "por", "pol", "cze", "ces", "hun", "dan", "swe",
        "nor", "fin", "tur", "dut", "nld", "gre", "ell", "heb", "ara", "hin"
    } for l in langs)

    # 1. If Open Library confirms an English translation exists for a foreign work:
    if has_eng and has_foreign_meta:
        return True, True
    if has_foreign_meta and not has_eng:
        return False, False
    if langs and not has_eng and all(len(l) == 3 for l in langs):
        return False, False
    if has_eng and not has_foreign_meta:
        tokens = [re.sub(r"[^\w]", "", w).lower() for w in re.split(r"[\s\-_/]+", raw_title)]
        tokens = [t for t in tokens if t]
        is_trans = any(c in FORBIDDEN_CHARS for c in raw_title) or any(t in JAPANESE_ROMANIZED_TOKENS for t in tokens)
        return True, is_trans

    # 2. If no language metadata is provided by Open Library, inspect the title text:
    for ch in raw_title:
        cp = ord(ch)
        for r_start, r_end in NON_LATIN_RANGES:
            if r_start <= cp <= r_end:
                return False, False
        if ch in FORBIDDEN_CHARS:
            return False, False

    tokens = [re.sub(r"[^\w]", "", w).lower() for w in re.split(r"[\s\-_/]+", raw_title)]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False, False

    for t in tokens:
        if t in JAPANESE_ROMANIZED_TOKENS:
            return False, False
    if any(t.endswith("kun") and len(t) > 3 for t in tokens):
        return False, False
    if any(t.endswith("chan") and len(t) > 4 for t in tokens):
        return False, False

    sp_count = sum(1 for w in tokens if w in SPANISH_WORDS)
    it_count = sum(1 for w in tokens if w in ITALIAN_WORDS)
    fr_count = sum(1 for w in tokens if w in FRENCH_WORDS)
    de_count = sum(1 for w in tokens if w in GERMAN_WORDS)
    eng_count = sum(1 for w in tokens if w in ENG_WORDS)

    first = tokens[0]
    if first in {"el", "los", "del", "las"} and eng_count == 0:
        return False, False
    if first in {"il", "lo", "gli"} and eng_count == 0:
        return False, False
    if first in {"le", "les", "des", "du"} and eng_count == 0:
        return False, False
    if first in {"der", "die", "das", "dem", "den"} and eng_count == 0:
        return False, False
    if first == "la" and (sp_count >= 1 or it_count >= 1 or fr_count >= 1) and eng_count == 0:
        return False, False

    if eng_count == 0 and (sp_count >= 1 or it_count >= 1 or fr_count >= 1 or de_count >= 1):
        return False, False

    if (sp_count >= 2 or it_count >= 2 or fr_count >= 2 or de_count >= 2) and eng_count <= 1:
        return False, False

    return True, False


def is_allowed_language_title(title: str, doc_languages: Optional[List] = None, allowed_languages: Optional[Set[str]] = None) -> bool:
    allowed, _ = check_title_and_translation(title, doc_languages, allowed_languages)
    return allowed


def is_likely_english_title(title: str, doc_languages: Optional[List] = None) -> bool:
    """Backward compatibility alias for English verification."""
    return is_allowed_language_title(title, doc_languages, allowed_languages={"eng"})
    return is_allowed_language_title(title, doc_languages, allowed_languages={"eng"})


def fetch_scored_candidates(catalog_df: pd.DataFrame) -> (List[Dict[str, object]], bool):
    if catalog_df.empty:
        return [], False

    anchor_books = catalog_df[catalog_df["My Rating"] >= 4.0].copy()
    if anchor_books.empty:
        anchor_books = catalog_df[catalog_df["My Rating"] > 0].copy()
    if anchor_books.empty:
        anchor_books = catalog_df.copy()

    affinity_authors = set(anchor_books["Author"].dropna().str.strip().str.lower())

    subject_scores = {}
    for _, row in catalog_df.iterrows():
        rating = row.get("My Rating") or 0
        weight = 2.5 if rating >= 4.5 else (1.5 if rating >= 3.5 else (0.2 if rating >= 2.5 else -1.5))
        genres_text = str(row.get("genres") or "")
        tags = [t.strip().lower() for t in genres_text.split(",") if t.strip()]
        for tag in tags:
            subject_scores[tag] = subject_scores.get(tag, 0.0) + weight

    top_subjects = sorted(subject_scores.items(), key=lambda x: x[1], reverse=True)

    nonfiction_keywords = {
        "nonfiction", "history", "biography", "memoir", "philosophy",
        "psychology", "science", "politics", "religion", "christianity"
    }
    has_nonfiction_anchor = any(
        any(k in str(r.get("genres") or "").lower() for k in nonfiction_keywords)
        for _, r in anchor_books.iterrows()
    )

    children_keywords = (
        "juvenile", "children's", "child and youth",
        "middle grade", "picture book", "elementary", "fairy tale"
    )
    reads_children = any(
        any(k in str(r.get("genres") or "").lower() for k in children_keywords)
        for _, r in anchor_books.iterrows()
    )

    if "Number of Pages" in catalog_df.columns:
        valid_pages = catalog_df[catalog_df["Number of Pages"] > 0]["Number of Pages"].dropna()
        p25 = float(valid_pages.quantile(0.25)) if len(valid_pages) >= 4 else 300.0
        p75 = float(valid_pages.quantile(0.75)) if len(valid_pages) >= 4 else 500.0
    else:
        p25, p75 = 300.0, 500.0

    # Era profile & sweet spot
    year_col = None
    if "Original Publication Year" in catalog_df.columns:
        year_col = "Original Publication Year"
    elif "Year Published" in catalog_df.columns:
        year_col = "Year Published"

    valid_years = pd.Series(dtype=float)
    if year_col:
        valid_years = pd.to_numeric(catalog_df[year_col], errors="coerce").dropna()
        valid_years = valid_years[(valid_years >= 1500) & (valid_years <= 2035)]

    if len(valid_years) >= 3:
        target_era = float(valid_years.median())
        p25_y = float(valid_years.quantile(0.25))
        p75_y = float(valid_years.quantile(0.75))
        era_spread = max(12.0, (p75_y - p25_y) * 1.2)
        min_user_year = float(valid_years.min())
        is_contemporary_reader = (target_era >= 1995 and min_user_year >= 1960)
    else:
        target_era = 2020.0
        era_spread = 15.0
        is_contemporary_reader = True

    # Dynamic language detection: strict English hard filter if all books English, else dynamic
    allowed_langs, is_all_english = detect_catalog_languages(catalog_df)
    if is_all_english:
        lang_query = "language:eng"
    else:
        lang_query = "(" + " OR ".join(f"language:{l}" for l in sorted(allowed_langs)) + ")"

    read_titles = set(clean_book_title(t) for t in catalog_df["Title"].dropna())
    read_keys = set(str(k).strip() for k in catalog_df["ol_key"].dropna().unique()) if "ol_key" in catalog_df.columns else set()
    
    # Map read authors to clean titles for cross-edition matching
    read_author_to_titles = {}
    for _, r in catalog_df.iterrows():
        a = str(r.get("Author") or "").strip().lower()
        t_clean = clean_book_title(str(r.get("Title") or ""))
        if a and t_clean:
            read_author_to_titles.setdefault(a, set()).add(t_clean)

    candidates = []
    seen_titles = set(read_titles)
    seen_keys = set(read_keys)
    headers = {"User-Agent": "NextChapterBookApp/1.0 (educational research)"}
    era_query = "publish_year:[1970 TO *]" if is_contemporary_reader else ""

    def add_valid_candidate(doc: dict, is_nonfiction: bool = False) -> bool:
        t = doc.get("title")
        c_title = clean_book_title(t)
        doc_key = str(doc.get("key") or "").strip()
        if not c_title or c_title in seen_titles or (doc_key and doc_key in seen_keys):
            return False
        allowed, is_trans = check_title_and_translation(t, doc.get("language"), allowed_languages=allowed_langs)
        if not allowed:
            return False
        if not reads_children and is_children_book(doc.get("subject", [])):
            return False
        cand_year = doc.get("first_publish_year")
        if is_contemporary_reader and cand_year and isinstance(cand_year, (int, float)) and cand_year < 1960:
            return False
        auth_list = doc.get("author_name") or []
        cand_auth = auth_list[0].strip().lower() if auth_list else ""
        if cand_auth in read_author_to_titles:
            if any(c_title in r_t or r_t in c_title for r_t in read_author_to_titles[cand_auth]):
                return False
        seen_titles.add(c_title)
        if doc_key:
            seen_keys.add(doc_key)
        doc["is_nonfiction"] = is_nonfiction
        doc["is_translation"] = is_trans
        candidates.append(doc)
        return True

    generic_tags = {"fiction", "general", "fiction / general", "nonfiction"}
    active_subjects = [s for s, _ in top_subjects if s.lower() not in generic_tags][:3]
    if not active_subjects:
        active_subjects = [s for s, _ in top_subjects][:2]

    # Primary Pass 1: Active top subjects
    for subj in active_subjects:
        url = "https://openlibrary.org/search.json"
        query_str = f'subject:"{subj}" {lang_query} {era_query}'.strip()
        params = {
            "q": query_str,
            "sort": "readinglog",
            "limit": 20,
            "fields": "key,title,author_name,first_publish_year,number_of_pages_median,ratings_average,ratings_count,subject,cover_i,language",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code == 200:
                for doc in resp.json().get("docs", []):
                    add_valid_candidate(doc, is_nonfiction=False)
        except Exception:
            pass

    # Primary Pass 2: Favorite anchor authors (up to 3)
    anchor_author_list = list(affinity_authors)
    for auth in anchor_author_list[:3]:
        url = "https://openlibrary.org/search.json"
        query_str = f'author:"{auth}" {lang_query} {era_query}'.strip()
        params = {
            "q": query_str,
            "sort": "readinglog",
            "limit": 12,
            "fields": "key,title,author_name,first_publish_year,number_of_pages_median,ratings_average,ratings_count,subject,cover_i,language",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code == 200:
                for doc in resp.json().get("docs", []):
                    add_valid_candidate(doc, is_nonfiction=False)
        except Exception:
            pass

    # Primary Pass 3: Nonfiction if anchor present
    if has_nonfiction_anchor:
        url = "https://openlibrary.org/search.json"
        query_str = f"subject:memoir OR subject:history {lang_query} {era_query}".strip()
        params = {
            "q": query_str,
            "sort": "readinglog",
            "limit": 12,
            "fields": "key,title,author_name,first_publish_year,number_of_pages_median,ratings_average,ratings_count,subject,cover_i,language",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code == 200:
                for doc in resp.json().get("docs", []):
                    add_valid_candidate(doc, is_nonfiction=True)
        except Exception:
            pass

    # ── Auto-Repopulation / Dynamic Backfill ──────────────────────────────────
    # If edge case filters (language, era, juvenile, duplicates) eliminated too many candidates,
    # automatically backfill with additional favorite authors, secondary subjects, and curated fallbacks.
    TARGET_MIN_CANDIDATES = 25
    if len(candidates) < TARGET_MIN_CANDIDATES:
        # Repopulation Step A: Query remaining anchor authors
        for auth in anchor_author_list[3:]:
            if len(candidates) >= TARGET_MIN_CANDIDATES:
                break
            url = "https://openlibrary.org/search.json"
            query_str = f'author:"{auth}" {lang_query} {era_query}'.strip()
            params = {
                "q": query_str,
                "sort": "readinglog",
                "limit": 10,
                "fields": "key,title,author_name,first_publish_year,number_of_pages_median,ratings_average,ratings_count,subject,cover_i,language",
            }
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=8)
                if resp.status_code == 200:
                    for doc in resp.json().get("docs", []):
                        add_valid_candidate(doc, is_nonfiction=False)
            except Exception:
                pass

        # Repopulation Step B: Query secondary subjects if available
        secondary_subjects = [s for s, _ in top_subjects if s.lower() not in generic_tags and s not in active_subjects][:4]
        for subj in secondary_subjects:
            if len(candidates) >= TARGET_MIN_CANDIDATES:
                break
            url = "https://openlibrary.org/search.json"
            query_str = f'subject:"{subj}" {lang_query} {era_query}'.strip()
            params = {
                "q": query_str,
                "sort": "readinglog",
                "limit": 15,
                "fields": "key,title,author_name,first_publish_year,number_of_pages_median,ratings_average,ratings_count,subject,cover_i,language",
            }
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=8)
                if resp.status_code == 200:
                    for doc in resp.json().get("docs", []):
                        add_valid_candidate(doc, is_nonfiction=False)
            except Exception:
                pass

        # Repopulation Step C: Fallback to Curated High-Affinity Contemporary Reads if still sparse
        if len(candidates) < TARGET_MIN_CANDIDATES:
            CURATED_CONTEMPORARY_FALLBACKS = [
                {"title": "The Four Winds", "author_name": ["Kristin Hannah"], "first_publish_year": 2021, "subject": ["historical fiction", "drama", "family"], "number_of_pages_median": 464, "ratings_average": 4.35, "ratings_count": 350000, "language": ["eng"]},
                {"title": "The Great Alone", "author_name": ["Kristin Hannah"], "first_publish_year": 2018, "subject": ["fiction", "drama", "adventure", "survival"], "number_of_pages_median": 440, "ratings_average": 4.40, "ratings_count": 400000, "language": ["eng"]},
                {"title": "The Maid", "author_name": ["Nita Prose"], "first_publish_year": 2022, "subject": ["mystery", "cozy mystery", "thriller", "crime fiction"], "number_of_pages_median": 304, "ratings_average": 3.85, "ratings_count": 280000, "language": ["eng"]},
                {"title": "The Bullet That Missed", "author_name": ["Richard Osman"], "first_publish_year": 2022, "subject": ["mystery", "humor", "crime", "cozy mystery"], "number_of_pages_median": 432, "ratings_average": 4.28, "ratings_count": 180000, "language": ["eng"]},
                {"title": "Lessons in Chemistry", "author_name": ["Bonnie Garmus"], "first_publish_year": 2022, "subject": ["historical fiction", "humor", "science", "feminism"], "number_of_pages_median": 400, "ratings_average": 4.30, "ratings_count": 600000, "language": ["eng"]},
                {"title": "Project Hail Mary", "author_name": ["Andy Weir"], "first_publish_year": 2021, "subject": ["science fiction", "space exploration", "hard sci-fi"], "number_of_pages_median": 496, "ratings_average": 4.52, "ratings_count": 450000, "language": ["eng"]},
                {"title": "Yellowface", "author_name": ["R.F. Kuang"], "first_publish_year": 2023, "subject": ["satire", "contemporary fiction", "publishing", "drama"], "number_of_pages_median": 336, "ratings_average": 3.90, "ratings_count": 250000, "language": ["eng"]},
                {"title": "Daisy Jones & The Six", "author_name": ["Taylor Jenkins Reid"], "first_publish_year": 2019, "subject": ["historical fiction", "music", "drama"], "number_of_pages_median": 368, "ratings_average": 4.20, "ratings_count": 500000, "language": ["eng"]},
                {"title": "A Man Called Ove", "author_name": ["Fredrik Backman"], "first_publish_year": 2012, "subject": ["contemporary fiction", "humor", "drama"], "number_of_pages_median": 337, "ratings_average": 4.38, "ratings_count": 700000, "language": ["eng"]},
                {"title": "Tomorrow, and Tomorrow, and Tomorrow", "author_name": ["Gabrielle Zevin"], "first_publish_year": 2022, "subject": ["fiction", "video games", "friendship", "drama"], "number_of_pages_median": 416, "ratings_average": 4.22, "ratings_count": 380000, "language": ["eng"]},
            ]
            for fb in CURATED_CONTEMPORARY_FALLBACKS:
                if len(candidates) >= TARGET_MIN_CANDIDATES:
                    break
                add_valid_candidate(fb, is_nonfiction=False)

    if not candidates:
        return [], has_nonfiction_anchor

    raw_subject_scores = []
    for doc in candidates:
        tags = [str(s).lower() for s in doc.get("subject", [])]
        raw = sum(subject_scores.get(t, 0.0) for t in tags)
        raw_subject_scores.append(raw)

    min_raw = min(raw_subject_scores) if raw_subject_scores else 0.0
    max_raw = max(raw_subject_scores) if raw_subject_scores else 1.0
    raw_spread = (max_raw - min_raw) if (max_raw - min_raw) > 0 else 1.0

    scored_candidates = []
    for i, doc in enumerate(candidates):
        title = doc.get("title", "Unknown Title")
        authors = doc.get("author_name", ["Unknown Author"])
        author = authors[0] if authors else "Unknown Author"
        pages = doc.get("number_of_pages_median")
        cand_year = doc.get("first_publish_year")
        ratings_avg = doc.get("ratings_average") or 0.0
        ratings_cnt = doc.get("ratings_count") or 0
        subjects = doc.get("subject", [])
        cover_i = doc.get("cover_i")
        cover_url = f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg" if cover_i else None
        is_nonfiction = doc.get("is_nonfiction", False)

        subject_fit = max(0.0, min(1.0, (raw_subject_scores[i] - min_raw) / raw_spread))
        author_bonus = 1.0 if any(a.lower() in affinity_authors for a in authors) else 0.0
        era_fit = calculate_era_fit(cand_year, target_era, era_spread)
        community = min(1.0, float(ratings_avg) / 5.0) if ratings_cnt >= 100 else 0.0
        length_fit = calculate_length_fit(pages, p25, p75)

        score = (
            (0.45 * subject_fit) +
            (0.20 * author_bonus) +
            (0.15 * era_fit) +
            (0.10 * community) +
            (0.10 * length_fit)
        )

        scored_candidates.append({
            "key": doc.get("key"),
            "title": title,
            "author": author,
            "year": cand_year,
            "pages": pages,
            "ratings_avg": round(ratings_avg, 2),
            "ratings_cnt": ratings_cnt,
            "subjects": subjects,
            "cover_url": cover_url,
            "is_nonfiction": is_nonfiction,
            "is_translation": doc.get("is_translation", False),
            "subject_fit": subject_fit,
            "author_bonus": author_bonus,
            "era_fit": era_fit,
            "community": community,
            "length_fit": length_fit,
            "score": score,
        })

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)

    top_pool = scored_candidates[:15]
    if top_pool:
        s_max = top_pool[0]["score"]
        s_min = top_pool[-1]["score"]
        spread = (s_max - s_min) if (s_max - s_min) > 0 else 1.0
        for c in scored_candidates:
            ratio = max(0.0, min(1.0, (c["score"] - s_min) / spread))
            c["taste_match"] = round(75.0 + (ratio * (98.0 - 75.0)), 1)
    else:
        for c in scored_candidates:
            c["taste_match"] = 85.0

    return scored_candidates, has_nonfiction_anchor


def select_diverse_recommendations(
    scored_pool: List[Dict[str, object]],
    exclude_titles: Optional[set] = None,
    max_picks: int = 5,
    reserve_nonfiction: bool = False,
) -> List[Dict[str, object]]:
    if not scored_pool:
        return []
    if exclude_titles is None:
        exclude_titles = set()

    GENERIC_SUBJECTS = {
        "fiction", "general", "fiction / general", "nonfiction",
        "accessible book", "protected daisy", "in library",
        "lending library", "large type books", "open library staff picks",
        "reading level", "classic literature", "paperback"
    }

    def get_primary_subject(cand):
        tags = [str(s).lower() for s in cand.get("subjects", [])]
        specific = [t for t in tags if t not in GENERIC_SUBJECTS]
        return specific[0] if specific else (tags[0] if tags else "general")

    def assign_pick_reason(cand, top_s):
        top_s_title = top_s.title()
        if cand.get("author_bonus", 0.0) == 1.0:
            return f"Because you loved {cand['author']}"
        elif cand.get("is_nonfiction", False):
            return f"Because you enjoy real-world stories & memoirs in {top_s_title}"
        elif cand.get("subject_fit", 0.0) >= 0.55:
            return f"Because you were hooked on {top_s_title}"
        elif cand.get("length_fit", 0.0) >= 0.9 and cand.get("pages"):
            return f"Since you typically read books of this length ({int(cand['pages'])}p)"
        else:
            return f"Because you liked top-rated {top_s_title} reads"

    picks = []
    subject_counts = {}
    author_counts = {}

    if reserve_nonfiction:
        for cand in scored_pool:
            if cand.get("is_nonfiction", False):
                c_title = clean_book_title(cand["title"])
                if c_title in exclude_titles:
                    continue
                cand_copy = dict(cand)
                top_s = get_primary_subject(cand)
                cand_copy["pick_reason"] = assign_pick_reason(cand_copy, top_s)
                picks.append(cand_copy)
                auth = cand["author"].lower()
                subject_counts[top_s] = 1
                author_counts[auth] = 1
                break

    for cand in scored_pool:
        if len(picks) >= max_picks:
            break

        c_title = clean_book_title(cand["title"])
        if c_title in exclude_titles or any(clean_book_title(p["title"]) == c_title for p in picks):
            continue

        top_s = get_primary_subject(cand)
        auth = cand["author"].lower()

        if subject_counts.get(top_s, 0) >= 2 or author_counts.get(auth, 0) >= 2:
            continue

        cand_copy = dict(cand)
        cand_copy["pick_reason"] = assign_pick_reason(cand_copy, top_s)
        picks.append(cand_copy)

        subject_counts[top_s] = subject_counts.get(top_s, 0) + 1
        author_counts[auth] = author_counts.get(auth, 0) + 1

    return picks


def clear_recommendations_state():
    for k in ("recommendations", "seen_recommendation_titles", "refresh_count", "scored_candidates", "has_nonfiction_anchor", "interested_titles", "read_titles"):
        st.session_state.pop(k, None)


def render_recommendations_section(catalog_df: pd.DataFrame):
    st.markdown(
        """
        <div style="text-align: center; margin: 40px 0 20px 0;">
            <div style="display: inline-block; padding: 6px 18px; background: #2563EB; color: #ffffff; font-size: 13px; font-weight: 800; border-radius: 999px; letter-spacing: 1.5px; margin-bottom: 8px;">
                RECOMMENDATIONS
            </div>
            <h2 style="font-size: 2.1rem; font-weight: 800; margin: 0; color: inherit;">
                So, what's your NextChapter? 🎯
            </h2>
            <p style="font-size: 1rem; opacity: 0.8; margin-top: 6px;">
                Hand-picked book recommendations matching your taste profile.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if "recommendations" not in st.session_state:
        col_c, _ = st.columns([1, 1])
        with col_c:
            if st.button("Discover Your NextChapter 🎯", type="primary", key="btn_fetch_recs"):
                with st.spinner("Analyzing your reading profile and sourcing recommendations..."):
                    scored_pool, has_nonfic = fetch_scored_candidates(catalog_df)
                    st.session_state["scored_candidates"] = scored_pool
                    st.session_state["has_nonfiction_anchor"] = has_nonfic
                    picks = select_diverse_recommendations(
                        scored_pool,
                        exclude_titles=set(),
                        max_picks=5,
                        reserve_nonfiction=has_nonfic,
                    )
                    st.session_state["recommendations"] = picks
                    st.session_state["seen_recommendation_titles"] = {clean_book_title(p["title"]) for p in picks}
                    st.session_state["refresh_count"] = 0
                    sess_id = st.session_state.get("session_id", "default")
                    log_feedback_batch([
                        {
                            "session_id": sess_id,
                            "title": p["title"],
                            "author": p["author"],
                            "taste_match_pct": p.get("taste_match", 0),
                            "action": "shown",
                            "ol_key": p.get("key"),
                        }
                        for p in picks
                    ])
                    st.rerun()
        return

    picks = st.session_state.get("recommendations", [])
    if not picks:
        st.info("No recommendations found yet. Click below to fetch.")
        if st.button("🔄 Try Again", type="secondary", key="btn_retry_recs"):
            clear_recommendations_state()
            st.rerun()
        return

    if "interested_titles" not in st.session_state:
        st.session_state["interested_titles"] = set()
    interested_titles = st.session_state["interested_titles"]
    if "read_titles" not in st.session_state:
        st.session_state["read_titles"] = set()
    read_titles = st.session_state["read_titles"]
    session_id = st.session_state.get("session_id", "default")

    refresh_count = st.session_state.get("refresh_count", 0)
    col_btn, _ = st.columns([1, 2])
    with col_btn:
        if refresh_count < 1:
            if st.button("🔄 Refresh Recommendations", type="secondary", key="btn_refresh_recs"):
                scored_pool = st.session_state.get("scored_candidates", [])
                if not scored_pool:
                    scored_pool, has_nonfic = fetch_scored_candidates(catalog_df)
                    st.session_state["scored_candidates"] = scored_pool
                    st.session_state["has_nonfiction_anchor"] = has_nonfic
                seen = st.session_state.get("seen_recommendation_titles", set())
                next_picks = select_diverse_recommendations(
                    scored_pool,
                    exclude_titles=seen,
                    max_picks=5,
                    reserve_nonfiction=False,
                )
                if next_picks:
                    st.session_state["recommendations"] = next_picks
                    st.session_state["seen_recommendation_titles"] = seen.union({clean_book_title(p["title"]) for p in next_picks})
                    st.session_state["refresh_count"] = 1
                    log_feedback_batch([
                        {
                            "session_id": session_id,
                            "title": p["title"],
                            "author": p["author"],
                            "taste_match_pct": p.get("taste_match", 0),
                            "action": "shown",
                            "ol_key": p.get("key"),
                        }
                        for p in next_picks
                    ])
                    st.rerun()
        else:
            st.button("🔄 Refresh Limit Reached (1/1 used)", type="secondary", disabled=True, key="btn_refresh_recs_disabled")

    cols = st.columns(len(picks))
    for idx, b in enumerate(picks):
        c_title = clean_book_title(b["title"])
        is_interested = c_title in interested_titles
        is_read = c_title in read_titles
        ol_url = f"https://openlibrary.org{b['key']}" if b.get("key") else f"https://openlibrary.org/search?q={urllib.parse.quote(b['title'])}"

        with cols[idx]:
            with st.container(border=True):
                if b.get("cover_url"):
                    st.markdown(
                        f"""<div style="text-align: center; margin-bottom: 8px;">
                            <img src="{b['cover_url']}" style="width: 100%; height: 165px; object-fit: cover; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.3);" />
                        </div>""",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        """<div style="height: 165px; background: #23232c; border-radius: 8px; display: flex; align-items: center; justify-content: center; color: #888; font-size: 11px; margin-bottom: 8px;">No Cover</div>""",
                        unsafe_allow_html=True,
                    )

                trans_badge = (
                    """<div style="background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.35); font-size: 9px; font-weight: 700; padding: 2px 6px; border-radius: 999px; display: inline-block; margin-left: 4px; margin-bottom: 6px;">
                        🌐 English Translation
                    </div>"""
                    if b.get("is_translation")
                    else ""
                )

                st.markdown(
                    f"""<div style="display: flex; align-items: center; flex-wrap: wrap; gap: 4px; margin-bottom: 6px;">
                        <div style="background: #2563EB; color: #ffffff; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 999px; display: inline-block;">
                            {b['taste_match']}% MATCH
                        </div>
                        {trans_badge}
                    </div>""",
                    unsafe_allow_html=True,
                )

                st.markdown(f"**{b['title']}**\n\n<small style='color: #cbd5e1;'>by {b['author']}</small>", unsafe_allow_html=True)

                year_t = f"🗓️ {int(b['year'])}" if b.get("year") else ""
                pages_t = f"📖 {int(b['pages'])}p" if b.get("pages") else "📖 N/A"
                comm_t = f"★ {b['ratings_avg']}" if b.get("ratings_avg") else "★ N/A"
                meta_parts = [p for p in [year_t, pages_t, comm_t] if p]
                st.caption(" • ".join(meta_parts))

                st.markdown(f"<div style='font-size: 11px; color: #93c5fd; font-style: italic; min-height: 40px; margin-bottom: 4px;'>💡 {b.get('pick_reason', '')}</div>", unsafe_allow_html=True)

                st.markdown(
                    f"""<div style="margin: 4px 0 10px 0;">
                        <a href="{ol_url}" target="_blank" style="display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-weight: 600; color: #60A5FA; text-decoration: none; padding: 3px 8px; background: rgba(59, 130, 246, 0.12); border: 1px solid rgba(59, 130, 246, 0.3); border-radius: 6px;">
                            <span>📖</span> Read Synopsis ↗
                        </a>
                    </div>""",
                    unsafe_allow_html=True,
                )

                if is_interested:
                    st.button("⭐ Interested", key=f"btn_int_{idx}_{c_title[:10]}", disabled=True, use_container_width=True)
                else:
                    if st.button("⭐ Interested!", key=f"btn_int_{idx}_{c_title[:10]}", type="secondary", use_container_width=True):
                        log_recommendation_feedback(
                            session_id=session_id,
                            title=b["title"],
                            author=b["author"],
                            taste_match_pct=b.get("taste_match", 0),
                            action="interested",
                            ol_key=b.get("key"),
                        )
                        st.session_state["interested_titles"].add(c_title)
                        st.rerun()

                if is_read:
                    st.button("✓ I've Read This", key=f"btn_read_{idx}_{c_title[:10]}", disabled=True, use_container_width=True)
                else:
                    if st.button("I've Read This!", key=f"btn_read_{idx}_{c_title[:10]}", type="secondary", use_container_width=True):
                        log_recommendation_feedback(
                            session_id=session_id,
                            title=b["title"],
                            author=b["author"],
                            taste_match_pct=b.get("taste_match", 0),
                            action="already_read",
                            ol_key=b.get("key"),
                        )
                        st.session_state["read_titles"].add(c_title)
                        st.rerun()


# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="NextChapter",
    page_icon="📖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom Royal Blue Theme Styles ───────────────────────────────────────────
st.markdown(
    """
    <style>
    /* Primary Action Buttons (Proceed, Generate Wrapped, Enrich, Import, etc.) */
    button[kind="primary"]:not(:disabled),
    button[data-testid="baseButton-primary"]:not(:disabled),
    div[data-testid="stButton"] > button[kind="primary"]:not(:disabled),
    div[data-testid="stDownloadButton"] > button[kind="primary"]:not(:disabled) {
        background: linear-gradient(135deg, #1D4ED8 0%, #3B82F6 100%) !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 12px !important;
        font-weight: 600 !important;
        letter-spacing: 0.3px !important;
        box-shadow: 0 4px 14px rgba(29, 78, 216, 0.38) !important;
        transition: all 0.25s ease-in-out !important;
    }

    button[kind="primary"]:not(:disabled):hover,
    button[data-testid="baseButton-primary"]:not(:disabled):hover,
    div[data-testid="stButton"] > button[kind="primary"]:not(:disabled):hover,
    div[data-testid="stDownloadButton"] > button[kind="primary"]:not(:disabled):hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 6px 20px rgba(37, 99, 235, 0.55) !important;
        filter: brightness(1.08) !important;
    }

    button[kind="primary"]:not(:disabled):active,
    button[data-testid="baseButton-primary"]:not(:disabled):active {
        transform: translateY(0) !important;
        box-shadow: 0 2px 8px rgba(29, 78, 216, 0.3) !important;
    }

    /* Secondary Action Buttons */
    button[kind="secondary"]:not(:disabled),
    button[data-testid="baseButton-secondary"]:not(:disabled),
    div[data-testid="stButton"] > button[kind="secondary"]:not(:disabled),
    div[data-testid="stDownloadButton"] > button[kind="secondary"]:not(:disabled) {
        border-radius: 12px !important;
        border: 1px solid rgba(255, 255, 255, 0.18) !important;
        font-weight: 500 !important;
        transition: all 0.2s ease-in-out !important;
    }

    button[kind="secondary"]:not(:disabled):hover,
    button[data-testid="baseButton-secondary"]:not(:disabled):hover {
        border-color: #3B82F6 !important;
        color: #60A5FA !important;
        background: rgba(59, 130, 246, 0.08) !important;
    }

    /* Disabled Buttons (Greyed out) */
    button:disabled,
    button[disabled],
    div[data-testid="stButton"] > button:disabled,
    div[data-testid="stButton"] > button[disabled],
    div[data-testid="stDownloadButton"] > button:disabled,
    div[data-testid="stDownloadButton"] > button[disabled] {
        background: #2D3748 !important;
        color: #94A3B8 !important;
        border: 1px solid #4B5563 !important;
        border-radius: 12px !important;
        box-shadow: none !important;
        cursor: not-allowed !important;
        opacity: 0.65 !important;
        transform: none !important;
        filter: grayscale(1) !important;
    }

    /* Active Tabs Accent */
    button[data-baseweb="tab"][aria-selected="true"] {
        border-bottom-color: #3B82F6 !important;
        color: #60A5FA !important;
    }

    /* Text Area & Input Focus Ring */
    textarea:focus, input:focus {
        border-color: #3B82F6 !important;
        box-shadow: 0 0 0 1px #3B82F6 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Clear DB on fresh session start ──────────────────────────────────────────
if "initialized" not in st.session_state:
    if os.path.exists(DB_NAME):
        os.remove(DB_NAME)
    st.session_state["initialized"] = True
if "session_id" not in st.session_state:
    st.session_state["session_id"] = str(uuid.uuid4())[:8]
if "interested_titles" not in st.session_state:
    st.session_state["interested_titles"] = set()
if "read_titles" not in st.session_state:
    st.session_state["read_titles"] = set()

st.markdown(
    """
    <div style="text-align: center; padding-top: 5px; padding-bottom: 20px;">
        <h1 style="
            font-family: 'Playfair Display', Georgia, 'Times New Roman', serif;
            font-size: 3.4rem;
            font-weight: 800;
            letter-spacing: -0.5px;
            margin: 0;
            display: inline-block;
        ">
            <span style="
                background: linear-gradient(135deg, #93C5FD 0%, #3B82F6 45%, #1D4ED8 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                color: #2563EB;
            ">NextChapter</span>
            <span style="
                font-size: 2.8rem;
                vertical-align: middle;
                margin-left: 6px;
            ">📖</span>
        </h1>
    </div>
    """,
    unsafe_allow_html=True,
)

has_data = table_has_data()

# ── Step 1: Import section ────────────────────────────────────────────────────────────
st.markdown("### Step 1: Paste Your Reading List / Upload CSV")
st.markdown(
    "<p style='font-size: 15px; color: gray; margin-top: -10px;'>"
    "Ratings should be on a scale of 1: Did Not Enjoy to 5: Loved It! (ratings given out of 10 will be divided by 2)."
    "</p>",
    unsafe_allow_html=True,
)

tab_paste, tab_upload = st.tabs(["Paste Text", "Upload CSV"])

with tab_paste:
    st.markdown("Paste your reading list below. Each book should be on its own line:")
    user_text = st.text_area(
        "Paste book list here:",
        height=200,
        placeholder="Example: Harry Potter and the Order of Phoenix, J.K. Rowling, 4.5",
        label_visibility="collapsed",
        disabled=has_data,
    )

    if not has_data:
        if st.button("Proceed", type="primary", key="btn_parse_text"):
            if not user_text.strip():
                st.warning("Please paste some text first.")
            else:
                try:
                    parsed_df, skipped = parse_book_text_input(user_text)
                    if parsed_df.empty:
                        st.error("Could not parse any books from the provided text. Please check the format.")
                    else:
                        df_goodreads = to_goodreads_df(parsed_df)
                        df_to_sqlite(df_goodreads)
                        st.session_state["show_wrapped"] = False
                        st.session_state["is_enriched"] = False
                        clear_recommendations_state()
                        st.rerun()
                except Exception as e:
                    st.error(f"Text parsing failed: {e}")
    else:
        col_proc, col_reset = st.columns([1, 4])
        with col_proc:
            st.button("Proceed", type="primary", key="btn_parse_text_disabled", disabled=True)
        with col_reset:
            if st.button("🔄 Start Over / Paste New List", type="secondary", key="btn_reset_paste"):
                if os.path.exists(DB_NAME):
                    os.remove(DB_NAME)
                st.session_state["show_wrapped"] = False
                st.session_state["is_enriched"] = False
                clear_recommendations_state()
                st.rerun()

with tab_upload:
    uploaded_file = st.file_uploader(
        "Upload a CSV file",
        type=["csv"],
        label_visibility="collapsed",
        disabled=has_data,
    )

    with open(TEMPLATE_PATH, "rb") as template_file:
        template_bytes = template_file.read()

    st.caption("Download CSV template here — leave columns empty if data is not known")
    st.download_button(
        label="⬇️ goodreads-template.csv",
        data=template_bytes,
        file_name="goodreads-template.csv",
        mime="text/csv",
    )

    if not has_data:
        if uploaded_file:
            tmp_path = f"/tmp/{uploaded_file.name}"
            with open(tmp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())

            if st.button("Proceed", type="primary", key="btn_import_csv"):
                try:
                    df_imported = csv_to_sqlite(tmp_path)
                    st.session_state["show_wrapped"] = False
                    st.session_state["is_enriched"] = False
                    clear_recommendations_state()
                    st.rerun()
                except Exception as e:
                    st.error(f"Import failed: {e}")
        else:
            st.button("Proceed", type="primary", key="btn_import_csv_placeholder", disabled=True)
    else:
        col_proc, col_reset = st.columns([1, 4])
        with col_proc:
            st.button("Proceed", type="primary", key="btn_import_csv_disabled", disabled=True)
        with col_reset:
            if st.button("🔄 Start Over / Upload New List", type="secondary", key="btn_reset_upload"):
                if os.path.exists(DB_NAME):
                    os.remove(DB_NAME)
                st.session_state["show_wrapped"] = False
                st.session_state["is_enriched"] = False
                clear_recommendations_state()
                st.rerun()

# ── Check Data Presence ───────────────────────────────────────────────────────
if not has_data:
    st.session_state["show_wrapped"] = False
    st.session_state["is_enriched"] = False
    st.stop()

# ── Step 2: Mandatory Enrichment ──────────────────────────────────────────────
is_enriched = st.session_state.get("is_enriched", False) or table_is_enriched()

st.divider()
st.subheader("Step 2: Enrich with Open Library")

if not is_enriched:
    st.info(
        "Click the button below to match your books against Open Library and fetch genres, page counts, community ratings, and cover images needed for your Wrapped stats."
    )
    if st.button("Proceed", type="primary", key="btn_enrich_mandatory"):
        progress_bar = st.progress(0)
        status_text = st.empty()
        with st.spinner("Enriching books via Open Library API..."):
            raw_df = load_table()
            enriched_df, matches = enrich_catalog_dataframe(
                raw_df, progress_bar=progress_bar, status_text=status_text
            )
            save_table(enriched_df)
            status_text.empty()
            progress_bar.empty()
            st.session_state["is_enriched"] = True
            st.session_state["show_wrapped"] = False
            st.rerun()
    st.stop()
else:
    st.button("Proceed", type="primary", key="btn_enrich_mandatory_done", disabled=True)
    st.success("✅ Catalog enriched with Open Library data.")

# ── Step 3: Editable Table & Wrapped Recap ────────────────────────────────────
st.divider()

st.subheader("Step 3: Review your table and edit if required")

try:
    df = load_table()

    display_cols = [
        "cover_url",
        "Title",
        "Author",
        "My Rating",
        "Number of Pages",
        "Year Published",
        "genres",
        "Average Rating",
    ]
    for c in display_cols:
        if c not in df.columns:
            df[c] = None

    if "Original Publication Year" in df.columns:
        df["Year Published"] = df["Year Published"].fillna(df["Original Publication Year"])

    df_display = df[display_cols].copy()
    if "My Rating" in df_display.columns:
        df_display["My Rating"] = pd.to_numeric(df_display["My Rating"], errors="coerce").astype(float)
    if "Number of Pages" in df_display.columns:
        df_display["Number of Pages"] = pd.to_numeric(df_display["Number of Pages"], errors="coerce")
    if "Year Published" in df_display.columns:
        df_display["Year Published"] = pd.to_numeric(df_display["Year Published"], errors="coerce")
    if "Average Rating" in df_display.columns:
        df_display["Average Rating"] = pd.to_numeric(df_display["Average Rating"], errors="coerce")

    col1, col2, col3 = st.columns([1, 1, 2])
    col1.metric("Rows", len(df))
    col2.metric("Columns", len(display_cols))

    with col3:
        if st.button("🔄 Re-enrich with Open Library", type="secondary", help="Re-fetch genres, page count, publication year, cover image, and average ratings from Open Library"):
            progress_bar = st.progress(0)
            status_text = st.empty()
            with st.spinner("Enriching books via Open Library API..."):
                enriched_df, matches = enrich_catalog_dataframe(
                    df, progress_bar=progress_bar, status_text=status_text
                )
                save_table(enriched_df)
                status_text.empty()
                progress_bar.empty()
                st.session_state["is_enriched"] = True
                st.success(f"✅ Successfully matched and enriched {matches}/{len(df)} books with Open Library data!")
                st.rerun()

    column_config = {
        "cover_url": st.column_config.ImageColumn("Cover", help="Book cover from Open Library"),
        "Title": st.column_config.TextColumn("Title", required=True),
        "Author": st.column_config.TextColumn("Author"),
        "My Rating": st.column_config.NumberColumn(
            "My Rating",
            help="Your rating from 0.0 to 5.0 (decimals allowed, e.g. 4.5)",
            min_value=0.0,
            max_value=5.0,
            step=0.1,
            format="%.1f",
        ),
        "Number of Pages": st.column_config.NumberColumn(
            "Pages",
            help="Total page count",
            min_value=1,
            max_value=10000,
            step=1,
            format="%d",
        ),
        "Year Published": st.column_config.NumberColumn(
            "Year",
            help="Year published",
            min_value=1000,
            max_value=2050,
            step=1,
            format="%d",
        ),
        "genres": st.column_config.TextColumn("Genres / Subjects"),
        "Average Rating": st.column_config.NumberColumn(
            "Community Rating",
            help="Open Library community rating (out of 5)",
            min_value=0.0,
            max_value=5.0,
            step=0.1,
            format="%.2f",
        ),
    }

    edited_df = st.data_editor(
        df_display,
        width="stretch",
        num_rows="dynamic",
        column_config=column_config,
    )

    def persist_edits(original_df, edited_subset):
        if len(original_df) == len(edited_subset):
            for col in display_cols:
                original_df[col] = edited_subset[col]
            if "Original Publication Year" in original_df.columns:
                original_df["Original Publication Year"] = original_df["Year Published"]
            save_table(original_df)
            return original_df
        else:
            save_table(edited_subset)
            return edited_subset

    btn_col1, btn_col2 = st.columns([1, 2])
    with btn_col1:
        if st.button("💾 Save changes", type="secondary"):
            df = persist_edits(df, edited_df)
            st.session_state["show_wrapped"] = False
            clear_recommendations_state()
            st.success("Changes saved to database.")

    with btn_col2:
        if st.button(
            "Save & Generate Wrapped 🎉",
            type="primary",
            help="Save your table and generate your Reading Wrapped!",
        ):
            df = persist_edits(df, edited_df)
            st.session_state["show_wrapped"] = True
            st.session_state["wrapped_slide"] = 0
            st.balloons()
            st.rerun()

    if st.session_state.get("show_wrapped", False):
        st.divider()
        try:
            stats = fetch_wrapped_stats()
            render_wrapped_section(stats)
            st.divider()
            render_recommendations_section(df)
        except Exception as err:
            st.error(f"Could not generate Wrapped stats: {err}")

except Exception as e:
    st.error(f"Could not load table: {e}")
