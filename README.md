# NextChapter

> Tell it the books you read and loved. It generates a "Wrapped" recap, tells you what to read next, and why it is worth your time!

A personalized book recommendation and reading telemetry app built with Python and hosted on Streamlit.

---

## Recommender Evaluation & The Holdout Paradox

To evaluate recommendation quality, I initially implemented a textbook **Leave-One-Out holdout test** across 37 highly rated (4.0+) books by sequentially hiding one favorite book, rebuilding the user taste profile without it, and checking if the engine recovered the hidden title in its top 5 picks (**Recall@5**).

The test yielded **0% Recall@5**, which highlighted three key dynamics in open-catalog discovery:

1. **Sample Size & Generalizability ($n=37, N=1$):** A single user's reading history of 37 books offers a **directional case study**, not statistical proof. At this sample size, individual quirks in one person's catalog disproportionately skew aggregate percentages.
2. **The Incomplete Ground Truth Problem:** In an open catalog with millions of titles (Open Library), hiding one book does not make it the *only* valid recommendation. In fact, **3 of the 5 books recommended during the test were titles the reader had already read and loved in prior years** (a 60% real-world hit rate)—yet the offline test penalized them as false positives.
3. **The Outlier Erasure Trap:** Hiding a standalone, high-rated book in a genre (e.g., the reader's single historical novel) erased the only evidence of that latent interest. The engine correctly recommended books matching the remaining profile, which mathematically ensured the hidden book would miss.

> **Takeaway:** While offline Recall@K works well on small, closed-world benchmark datasets (like MovieLens), open-web discovery engines require **online evaluation**.

NextChapter tracks this through persistent user events in SQLite (`feedback.db`), measuring true **Precision** via active reader interactions:
- **"⭐ Interested!"** (positive intent / latent interest)
- **"I've Read This!"** (prior affinity verification)

This captures genuine taste alignment in a massive candidate pool rather than penalizing serendipitous or previously validated discoveries.
