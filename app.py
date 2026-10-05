import streamlit as st
import pandas as pd
import sqlite3
import os

DB_NAME = "catalog.db"
TABLE_NAME = "user_catalog"
TEMPLATE_PATH = "goodreads-template.csv"


def csv_to_sqlite(csv_path, db_name=DB_NAME, table_name=TABLE_NAME):
    """Read a CSV file and store its contents in a SQLite table, always replacing."""
    df = pd.read_csv(csv_path)
    conn = sqlite3.connect(db_name)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()
    return df


def load_table(db_name=DB_NAME, table_name=TABLE_NAME):
    """Load the full SQLite table into a DataFrame."""
    conn = sqlite3.connect(db_name)
    df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
    conn.close()
    return df


def save_table(df, db_name=DB_NAME, table_name=TABLE_NAME):
    """Write an edited DataFrame back to the SQLite table."""
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

# ── Upload section ────────────────────────────────────────────────────────────
st.header("Upload CSV")
uploaded_file = st.file_uploader("Upload a CSV file with books you read this year - include your ratings on a scale 1: Did Not Enjoy to 5: Loved It!", type=["csv"])

with open(TEMPLATE_PATH, "rb") as template_file:
    template_bytes = template_file.read()

st.caption(
    "Download CSV template here — leave columns empty if data is not known"
)
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

    if st.button("Import to SQLite", type="primary"):
        try:
            df_imported = csv_to_sqlite(tmp_path)
            st.success(
                f"✅ Imported **{len(df_imported)} rows × {len(df_imported.columns)} columns** "
                f"into `{TABLE_NAME}`"
            )
            st.rerun()
        except Exception as e:
            st.error(f"Import failed: {e}")

st.divider()

# ── Editable table ────────────────────────────────────────────────────────────
db_exists = os.path.exists(DB_NAME)

if not db_exists:
    st.info("Upload a CSV above to get started.")
    st.stop()

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
