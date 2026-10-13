import streamlit as st
import pandas as pd
import sqlite3
import os
import re
from typing import Dict, Optional

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
    s = re.sub(r"^['\"'\"\u201c\u201d\u2018\u2019]+|['\"'\"\u201c\u201d\u2018\u2019]+$", "", s)
    s = re.sub(r"^\s*(?:\d+[.)\-]\s*|[\-•*]\s+)", "", s)
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

    s = re.sub(r"^\s*(?:\d+[.)\-]\s*|[•*]\s+)", "", s).strip()

    title = ""
    author = ""
    rating_raw = ""

    # Strategy 1: "Title by Author"
    by_match = re.match(r"^(.+?)\s+by\s+(.+)$", s, re.IGNORECASE)
    if by_match:
        title_part = by_match.group(1).strip()
        rest = by_match.group(2).strip()

        trailing_rating = re.search(
            r"[,;]\s*(\d+\.?\d*(?:\s*/\s*\d+)?(?:\s*stars?)?)\s*$",
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

    col1, col2 = st.columns(2)
    col1.metric("Rows", len(df))
    col2.metric("Columns", len(df.columns))

    edited_df = st.data_editor(df, width="stretch", num_rows="dynamic")

    if st.button("💾 Save changes", type="primary"):
        save_table(edited_df)
        st.success("Changes saved to database.")

except Exception as e:
    st.error(f"Could not load table: {e}")
