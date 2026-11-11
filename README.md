# NextChapter

> Tell it the books you read and loved. It generates a "Wrapped" recap, tells you what to read next, and why it is worth your time!

A personalized book recommendation and reading telemetry app built with Python and hosted on Streamlit.

---

## Recommender Evaluation & The Holdout Paradox

To evaluate recommendation quality, I initially ran a standard **Leave-One-Out holdout test** across 37 highly rated (4.0+) books. This involved hiding one favorite book at a time, rebuilding the taste profile without it, and checking if the app recommended that book back in the top 5 picks (**Recall@5**).

The test returned **0% Recall@5**, which surfaced three interesting dynamics in open-catalog discovery:

1. **Small sample size:** A single person's reading history of 37 books is a useful case study, but individual quirks (like having read mostly one genre) can skew the results significantly.
2. **The incomplete ground truth problem:** In an open catalog with millions of titles, hiding one book does not make it the *only* valid recommendation. In fact, **3 of the 5 books recommended during the test were titles the reader had already read and loved** (a 60% real-world hit rate), but the test still penalized these as failures.
3. **The outlier erasure trap:** Hiding a one-off, high-rated book in a niche genre (e.g., the reader's only historical novel) erased the only signal the app had for that interest. Naturally, it then recommended books based on everything else the reader liked, which mathematically guaranteed the hidden book would not appear.

> **Takeaway:** Offline holdout tests work well on clean, closed benchmark datasets, but open-catalog discovery apps need live evaluation with real users.

NextChapter tracks this through user interactions saved to a database (`feedback.db`), measuring how readers respond to each recommendation:
- **"⭐ Interested!"** — the book looks new and exciting
- **"I've Read This!"** — the book is already known to the reader

---

## Online A/B Experiment: Rule-Based vs. Similarity-Based Recommendations

To test whether a smarter model could improve recommendations, I ran an A/B experiment with 7 readers (182 total recommendation impressions) comparing two approaches:

- **Model A (Rule-Based Heuristic):** Scores books using weighted rules: genre match (55%), author affinity (20%), community ratings (15%), and book length (10%).
- **Model B (k-NN Similarity):** Builds a profile of the reader's tastes from their ratings, then finds books that are most similar to that profile across genre, topic, era, and author.

The hypothesis and success criteria were written down in [`experiments.md`](experiments.md) *before* any Model B data was collected, to avoid drawing conclusions that fit the data after the fact.

### Results

| Metric | Model A (Rule-Based) | Model B (k-NN) | Change |
|---|---|---|---|
| Recommendations Shown | 61 | 60 | — |
| "Interested!" Clicks | 19 | 20 | +1 |
| "Already Read" Clicks | 15 | 7 | -8 |
| **Interest Rate (Primary)** | **31.1%** (19/61) | **33.3%** (20/60) | **+2.2%** |
| **Already-Read Rate** | **24.6%** (15/61) | **11.7%** (7/60) | **-12.9%** (down 52.5%)* |
| Share of clicks that were discoveries | 55.9% | **74.1%** | **+18.2%** |

*\*Significant at the 10% level (χ² = 3.40, p = 0.065).*

![A/B Experiment Results](ab_experiment_results.png)

### Key Takeaways

1. **The recommendation engine was recommending books readers had already read.** Nearly 1 in 4 recommendations shown by the rule-based model (24.6%) were books the reader had already finished. The similarity-based model more than halved this rate, down to 11.7%.
2. **More of the engagement became meaningful.** In Model A, 44% of all reader clicks were "I've already read this." In Model B, **74.1% of clicks were "Interested!"** — meaning readers were genuinely discovering new books rather than just recognizing old ones.
3. **Model B (k-NN) is the active engine in NextChapter.** Full experiment details are in [`experiments.md`](experiments.md).
