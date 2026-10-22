"""
NextChapter Recommendation Engine — Holdout Evaluation (Recall@5)

How it works:
  For each book you rated highly (≥ 4.0), we "hide" it from the catalog,
  rebuild the taste profile from scratch, fetch new recommendations, and
  check whether the hidden book appears in the top-5 picks.

  Recall@5 = (number of hidden books recovered) / (total hidden books tested)

Usage:
  python evaluate.py                       # uses catalog.db in current dir
  python evaluate.py --db path/to/catalog.db
  python evaluate.py --min-rating 4.5      # only test 4.5+ rated books
  python evaluate.py --runs 3              # repeat each case 3 times (majority vote)

Results are saved to evaluation_results.json for reproducibility.
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import sqlite3


# ── Helper functions (mirrored from app.py) ──────────────────────────────────

def clean_book_title(title: str) -> str:
    if not title or not isinstance(title, str):
        return ""
    base = re.split(r" - | \(|: | by ", title, flags=re.IGNORECASE)[0].strip()
    nfkd = unicodedata.normalize("NFKD", base)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    clean = re.sub(r"[^\w\s]", "", no_accents).lower()
    return re.sub(r"\s+", " ", clean).strip()


def is_children_book(subjects: list) -> bool:
    if not subjects:
        return False
    juvenile_terms = (
        "juvenile", "children's", "child and youth",
        "middle grade", "picture book", "elementary school", "early reader",
    )
    return any(any(term in str(s).lower() for term in juvenile_terms) for s in subjects)


def calculate_length_fit(pages: Optional[float], p25: float = 300.0, p75: float = 500.0) -> float:
    if not pages or pd.isna(pages) or pages <= 0:
        return 0.5
    if p25 <= pages <= p75:
        return 1.0
    if pages < p25:
        return max(0.0, 1.0 - (p25 - pages) / 200.0)
    return max(0.0, 1.0 - (pages - p75) / 400.0)


# ── Core recommender logic (extracted from app.py for offline use) ───────────

def fetch_scored_candidates(catalog_df: pd.DataFrame) -> Tuple[List[Dict], bool]:
    """Build taste profile and fetch scored candidates from Open Library."""
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
        "psychology", "science", "politics", "religion", "christianity",
    }
    has_nonfiction_anchor = any(
        any(k in str(r.get("genres") or "").lower() for k in nonfiction_keywords)
        for _, r in anchor_books.iterrows()
    )

    children_keywords = (
        "juvenile", "children's", "child and youth",
        "middle grade", "picture book", "elementary", "fairy tale",
    )
    reads_children = any(
        any(k in str(r.get("genres") or "").lower() for k in children_keywords)
        for _, r in anchor_books.iterrows()
    )

    anchor_pages = anchor_books["Number of Pages"].dropna()
    valid_pages = anchor_pages[(anchor_pages >= 50) & (anchor_pages <= 2000)]
    if len(valid_pages) >= 4:
        p25 = float(valid_pages.quantile(0.25))
        p75 = float(valid_pages.quantile(0.75))
    else:
        p25, p75 = 300.0, 500.0

    read_titles = set(clean_book_title(t) for t in catalog_df["Title"].dropna())
    candidates = []
    seen_titles = set(read_titles)
    headers = {"User-Agent": "NextChapterBookApp/1.0 (educational research)"}

    generic_tags = {"fiction", "general", "fiction / general", "nonfiction"}
    active_subjects = [s for s, _ in top_subjects if s.lower() not in generic_tags][:3]
    if not active_subjects:
        active_subjects = [s for s, _ in top_subjects][:2]

    for subj in active_subjects:
        url = "https://openlibrary.org/search.json"
        params = {
            "q": f'subject:"{subj}" language:eng',
            "sort": "readinglog",
            "limit": 15,
            "fields": "key,title,author_name,number_of_pages_median,ratings_average,ratings_count,subject,cover_i",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code == 200:
                for doc in resp.json().get("docs", []):
                    t = doc.get("title")
                    c_title = clean_book_title(t)
                    if not c_title or c_title in seen_titles:
                        continue
                    if not reads_children and is_children_book(doc.get("subject", [])):
                        continue
                    seen_titles.add(c_title)
                    doc["is_nonfiction"] = False
                    candidates.append(doc)
        except Exception:
            pass
        time.sleep(1)

    for auth in list(affinity_authors)[:2]:
        url = "https://openlibrary.org/search.json"
        params = {
            "author": auth,
            "sort": "readinglog",
            "limit": 6,
            "fields": "key,title,author_name,number_of_pages_median,ratings_average,ratings_count,subject,cover_i",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code == 200:
                for doc in resp.json().get("docs", []):
                    t = doc.get("title")
                    c_title = clean_book_title(t)
                    if not c_title or c_title in seen_titles:
                        continue
                    if not reads_children and is_children_book(doc.get("subject", [])):
                        continue
                    seen_titles.add(c_title)
                    doc["is_nonfiction"] = False
                    candidates.append(doc)
        except Exception:
            pass
        time.sleep(1)

    if has_nonfiction_anchor:
        url = "https://openlibrary.org/search.json"
        params = {
            "q": "subject:memoir OR subject:history language:eng",
            "sort": "readinglog",
            "limit": 10,
            "fields": "key,title,author_name,number_of_pages_median,ratings_average,ratings_count,subject,cover_i",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code == 200:
                for doc in resp.json().get("docs", []):
                    t = doc.get("title")
                    c_title = clean_book_title(t)
                    if not c_title or c_title in seen_titles:
                        continue
                    if not reads_children and is_children_book(doc.get("subject", [])):
                        continue
                    seen_titles.add(c_title)
                    doc["is_nonfiction"] = True
                    candidates.append(doc)
        except Exception:
            pass

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
        ratings_avg = doc.get("ratings_average") or 0.0
        ratings_cnt = doc.get("ratings_count") or 0
        subjects = doc.get("subject", [])
        is_nonfiction = doc.get("is_nonfiction", False)

        subject_fit = max(0.0, min(1.0, (raw_subject_scores[i] - min_raw) / raw_spread))
        author_bonus = 1.0 if any(a.lower() in affinity_authors for a in authors) else 0.0
        community = min(1.0, float(ratings_avg) / 5.0) if ratings_cnt >= 100 else 0.0
        length_fit = calculate_length_fit(pages, p25, p75)

        score = (0.55 * subject_fit) + (0.20 * author_bonus) + (0.15 * community) + (0.10 * length_fit)

        scored_candidates.append({
            "key": doc.get("key"),
            "title": title,
            "author": author,
            "pages": pages,
            "ratings_avg": round(ratings_avg, 2),
            "ratings_cnt": ratings_cnt,
            "subjects": subjects,
            "is_nonfiction": is_nonfiction,
            "subject_fit": subject_fit,
            "author_bonus": author_bonus,
            "community": community,
            "length_fit": length_fit,
            "score": score,
        })

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    return scored_candidates, has_nonfiction_anchor


def select_top_5(scored_pool: List[Dict], exclude_titles: set = None, reserve_nonfiction: bool = False) -> List[Dict]:
    """Pick top 5 diverse recommendations (mirrors select_diverse_recommendations from app.py)."""
    if not scored_pool:
        return []
    if exclude_titles is None:
        exclude_titles = set()

    def get_primary_subject(cand):
        tags = [str(s).lower() for s in cand.get("subjects", [])]
        specific = [t for t in tags if t not in ("fiction", "general", "fiction / general", "nonfiction")]
        return specific[0] if specific else (tags[0] if tags else "general")

    picks = []
    subject_counts = {}
    author_counts = {}

    if reserve_nonfiction:
        for cand in scored_pool:
            if cand.get("is_nonfiction", False):
                c_title = clean_book_title(cand["title"])
                if c_title in exclude_titles:
                    continue
                picks.append(cand)
                subject_counts[get_primary_subject(cand)] = 1
                author_counts[cand["author"].lower()] = 1
                break

    for cand in scored_pool:
        if len(picks) >= 5:
            break
        c_title = clean_book_title(cand["title"])
        if c_title in exclude_titles or any(clean_book_title(p["title"]) == c_title for p in picks):
            continue
        top_s = get_primary_subject(cand)
        auth = cand["author"].lower()
        if subject_counts.get(top_s, 0) >= 2 or author_counts.get(auth, 0) >= 2:
            continue
        picks.append(cand)
        subject_counts[top_s] = subject_counts.get(top_s, 0) + 1
        author_counts[auth] = author_counts.get(auth, 0) + 1

    return picks


# ── Holdout evaluation loop ──────────────────────────────────────────────────

def run_holdout_evaluation(
    db_path: str = "catalog.db",
    min_rating: float = 4.0,
    runs_per_case: int = 1,
) -> dict:
    """
    Leave-one-out holdout evaluation.

    For each book rated >= min_rating:
      1. Remove it entirely from the catalog (taste profile + exclusion list)
      2. Run the full recommendation pipeline on the reduced catalog
      3. Check if the hidden book appears in the top 5 picks
      4. Record hit/miss and what the top 5 were

    Returns a results dict with per-case details and aggregate Recall@5.
    """
    conn = sqlite3.connect(db_path)
    full_catalog = pd.read_sql_query("SELECT * FROM user_catalog", conn)
    conn.close()

    if "My Rating" not in full_catalog.columns:
        print("❌ 'My Rating' column not found in catalog. Cannot run evaluation.")
        return {}

    full_catalog["My Rating"] = pd.to_numeric(full_catalog["My Rating"], errors="coerce").fillna(0)
    holdout_books = full_catalog[full_catalog["My Rating"] >= min_rating].copy()

    if holdout_books.empty:
        print(f"⚠️  No books rated >= {min_rating} found. Try a lower --min-rating.")
        return {}

    print("=" * 70)
    print("🔬 NEXTCHAPTER HOLDOUT EVALUATION — Recall@5")
    print("=" * 70)
    print(f"   Catalog size:        {len(full_catalog)} books")
    print(f"   Books rated ≥ {min_rating}:  {len(holdout_books)} (test cases)")
    print(f"   Runs per case:       {runs_per_case}")
    print(f"   Started at:          {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    results = []
    hits = 0
    total = len(holdout_books)

    for idx, (row_idx, hidden_row) in enumerate(holdout_books.iterrows()):
        hidden_title = hidden_row["Title"]
        hidden_author = hidden_row.get("Author", "Unknown")
        hidden_rating = hidden_row["My Rating"]
        hidden_clean = clean_book_title(hidden_title)

        print(f"\n── Case {idx + 1}/{total}: Hiding \"{hidden_title}\" by {hidden_author} "
              f"(rated {hidden_rating}) ──")

        reduced_catalog = full_catalog.drop(index=row_idx).reset_index(drop=True)

        run_hits = 0
        all_top5_titles = []

        for run in range(runs_per_case):
            if runs_per_case > 1:
                print(f"   Run {run + 1}/{runs_per_case}...", end=" ")

            try:
                scored, has_nonfic = fetch_scored_candidates(reduced_catalog)
                top5 = select_top_5(scored, reserve_nonfiction=has_nonfic)
                top5_titles = [c["title"] for c in top5]
                top5_clean = [clean_book_title(t) for t in top5_titles]

                found = hidden_clean in top5_clean
                if found:
                    run_hits += 1

                all_top5_titles = top5_titles
                status = "✅ HIT" if found else "❌ MISS"
                if runs_per_case > 1:
                    print(status)
                else:
                    print(f"   Result: {status}")

            except Exception as e:
                print(f"   ⚠️  Error: {e}")
                all_top5_titles = []

            if run < runs_per_case - 1:
                time.sleep(2)

        is_hit = run_hits > (runs_per_case / 2)
        if is_hit:
            hits += 1

        if runs_per_case > 1:
            print(f"   Majority vote: {'✅ HIT' if is_hit else '❌ MISS'} "
                  f"({run_hits}/{runs_per_case} runs)")

        print(f"   Top 5 shown: {all_top5_titles}")

        results.append({
            "hidden_title": hidden_title,
            "hidden_author": hidden_author,
            "hidden_rating": float(hidden_rating),
            "hit": is_hit,
            "run_hits": run_hits,
            "runs": runs_per_case,
            "top5_shown": all_top5_titles,
        })

        if idx < total - 1:
            time.sleep(2)

    recall_at_5 = hits / total if total > 0 else 0.0

    print("\n" + "=" * 70)
    print("📊 FINAL RESULTS")
    print("=" * 70)
    print(f"   Test cases (n):   {total}")
    print(f"   Hits:             {hits}")
    print(f"   Recall@5:         {recall_at_5:.2%}")
    print("=" * 70)

    if total < 20:
        print(f"\n   ⚠️  Small sample (n={total}). This is a directional signal, not")
        print(f"      statistically significant. Collect more reading lists to strengthen it.")

    misses = [r for r in results if not r["hit"]]
    if misses:
        print(f"\n📝 Failure Analysis ({len(misses)} misses):")
        for m in misses:
            print(f"   • \"{m['hidden_title']}\" by {m['hidden_author']} "
                  f"(rated {m['hidden_rating']})")
            print(f"     Instead showed: {m['top5_shown'][:3]}...")

    output = {
        "timestamp": datetime.now().isoformat(),
        "catalog_size": len(full_catalog),
        "test_cases": total,
        "hits": hits,
        "recall_at_5": round(recall_at_5, 4),
        "min_rating_threshold": min_rating,
        "runs_per_case": runs_per_case,
        "cases": results,
    }

    output_path = os.path.join(os.path.dirname(db_path) or ".", "evaluation_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n💾 Results saved to {output_path}")

    return output


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Holdout evaluation for the NextChapter recommendation engine"
    )
    parser.add_argument("--db", default="catalog.db",
                        help="Path to catalog.db (default: catalog.db)")
    parser.add_argument("--min-rating", type=float, default=4.0,
                        help="Minimum rating for holdout books (default: 4.0)")
    parser.add_argument("--runs", type=int, default=1,
                        help="Runs per test case for majority voting (default: 1)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"❌ Database not found: {args.db}")
        print("   Run the NextChapter app first to build your catalog, then re-run this script.")
        sys.exit(1)

    run_holdout_evaluation(
        db_path=args.db,
        min_rating=args.min_rating,
        runs_per_case=args.runs,
    )
