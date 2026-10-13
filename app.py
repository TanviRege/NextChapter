import streamlit as st
import pandas as pd
import sqlite3
import os
import re
import requests
from typing import Dict, Optional, Union, List

DB_NAME = "catalog.db"
TABLE_NAME = "user_catalog"
TEMPLATE_PATH = "goodreads-template.csv"

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


def normalize_rating(raw: str) -> int:
    """Parse and normalize rating (handles /5, /10, clamps to 0-5)."""
    if not raw or not isinstance(raw, str):
        return 0
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
            value = (numerator / denominator) * 5.0 if denominator > 0 else 0
        return max(0, min(5, round(value)))

    num_match = re.search(r"(\d+\.?\d*)", s)
    if not num_match:
        return 0

    value = float(num_match.group(1))
    if value > 5:
        value = value / 2.0
    return max(0, min(5, round(value)))


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
    out["My Rating"] = parsed_df["My Rating"]
    out["Exclusive Shelf"] = "read"
    out["Read Count"] = 1
    out["Owned Copies"] = 0

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
    "historical fiction", "science fiction", "fiction", "nonfiction", "romance",
    "thriller", "mystery", "suspense", "war", "family", "art", "history",
    "biography", "autobiography", "memoir", "science", "fantasy", "crime",
    "horror", "adventure", "poetry", "drama", "philosophy", "psychology", "politics",
]


def clean_and_prioritize_subjects(
    raw_subjects: list,
    places: list = None,
    times: list = None,
    limit: int = 5,
) -> list:
    """Filter, clean, and prioritize subjects matching priority genres."""
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
    for item in raw_subjects:
        if not item or not isinstance(item, str):
            continue
        s = item.lower().strip()
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
    """Enrich books with Open Library data: genres, pages, pub year, cover, and rating."""
    enriched_df = df.copy()

    # Ensure required columns exist
    for col in ["Average Rating", "Number of Pages", "Original Publication Year", "cover_url", "genres"]:
        if col not in enriched_df.columns:
            enriched_df[col] = None

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
            if data.get("Average Rating") is not None:
                enriched_df.at[idx, "Average Rating"] = data["Average Rating"]
            if data.get("Number of Pages") is not None:
                enriched_df.at[idx, "Number of Pages"] = data["Number of Pages"]
            if data.get("Original Publication Year") is not None:
                enriched_df.at[idx, "Original Publication Year"] = data["Original Publication Year"]
            if data.get("cover_url"):
                enriched_df.at[idx, "cover_url"] = data["cover_url"]
            if data.get("genres"):
                enriched_df.at[idx, "genres"] = data["genres"]

    return enriched_df, matched_count


# ── Database Helpers ──────────────────────────────────────────────────────────
def df_to_sqlite(df: pd.DataFrame, db_name=DB_NAME, table_name=TABLE_NAME):
    """Store a DataFrame in SQLite table, replacing existing records."""
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
    conn = sqlite3.connect(db_name)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()


# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="NextChapter", page_icon="📚", layout="wide")

# ── Clear DB on fresh session start ──────────────────────────────────────────
if "initialized" not in st.session_state:
    if os.path.exists(DB_NAME):
        os.remove(DB_NAME)
    st.session_state["initialized"] = True
st.title("NextChapter 📚")

# ── Import section ────────────────────────────────────────────────────────────
st.markdown("### Upload CSV/Paste Your Reading List")
st.markdown(
    "<p style='font-size: 15px; color: gray; margin-top: -10px;'>"
    "Ratings should be on a scale of 1: Did Not Enjoy to 5: Loved It! (ratings given out of 10 will be divided by 2)."
    "</p>",
    unsafe_allow_html=True,
)

tab_upload, tab_paste = st.tabs(["📁 Upload CSV", "✍️ Paste Text"])

with tab_upload:
    uploaded_file = st.file_uploader(
        "Upload a CSV file",
        type=["csv"],
        label_visibility="collapsed",
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

    if uploaded_file:
        tmp_path = f"/tmp/{uploaded_file.name}"
        with open(tmp_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        if st.button("Import CSV to SQLite", type="primary", key="btn_import_csv"):
            try:
                df_imported = csv_to_sqlite(tmp_path)
                st.success(
                    f"✅ Imported **{len(df_imported)} rows × {len(df_imported.columns)} columns** "
                    f"into `{TABLE_NAME}`"
                )
                st.rerun()
            except Exception as e:
                st.error(f"Import failed: {e}")

with tab_paste:
    st.markdown("Paste your reading list below. Each book should be on its own line:")
    user_text = st.text_area(
        "Paste book list here:",
        height=200,
        placeholder="Example: The Nightingale by Kristin Hannah, 5",
        label_visibility="collapsed",
    )

    if st.button("Parse & Import to SQLite", type="primary", key="btn_parse_text"):
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
                    st.success(
                        f"✅ Parsed and imported **{len(df_goodreads)} books** into `{TABLE_NAME}`!"
                    )
                    if skipped:
                        st.warning(
                            f"⚠️ Could not parse {len(skipped)} line(s): "
                            + ", ".join([f"line {num}" for num, _ in skipped])
                        )
                    st.rerun()
            except Exception as e:
                st.error(f"Text parsing failed: {e}")

# ── Editable table ────────────────────────────────────────────────────────────
has_data = table_has_data()

if not has_data:
    st.stop()

st.divider()

st.subheader("Review your table and edit if required:")

try:
    df = load_table()

    col1, col2, col3 = st.columns([1, 1, 2])
    col1.metric("Rows", len(df))
    col2.metric("Columns", len(df.columns))

    with col3:
        if st.button("✨ Enrich with Open Library", type="secondary", help="Fetch genres, page count, publication year, cover image, and average ratings from Open Library"):
            progress_bar = st.progress(0)
            status_text = st.empty()
            with st.spinner("Enriching books via Open Library API..."):
                enriched_df, matches = enrich_catalog_dataframe(
                    df, progress_bar=progress_bar, status_text=status_text
                )
                save_table(enriched_df)
                status_text.empty()
                progress_bar.empty()
                st.success(f"✅ Successfully matched and enriched {matches}/{len(df)} books with Open Library data!")
                st.rerun()

    # Column configuration for clean data editor presentation
    column_config = {}
    if "cover_url" in df.columns:
        column_config["cover_url"] = st.column_config.ImageColumn("Cover", help="Book cover image from Open Library")
    if "Average Rating" in df.columns:
        column_config["Average Rating"] = st.column_config.NumberColumn("Community Rating", format="%.2f")
    if "genres" in df.columns:
        column_config["genres"] = st.column_config.TextColumn("Genres / Subjects")

    edited_df = st.data_editor(
        df,
        width="stretch",
        num_rows="dynamic",
        column_config=column_config,
    )

    if st.button("💾 Save changes", type="primary"):
        save_table(edited_df)
        st.success("Changes saved to database.")

except Exception as e:
    st.error(f"Could not load table: {e}")
