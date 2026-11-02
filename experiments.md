# NextChapter — A/B Experiment Log

> This file was created **before** k-NN (Model B) results were collected, to pre-register
> the hypothesis and evaluation criteria in accordance with A/B testing best practices.
> Hypotheses must not be modified after data collection begins.

---

## Experiment 1: Heuristic vs. k-NN Recommender

**Pre-registration timestamp:** 2026-11-02T11:10:00-05:00 (Eastern)
**Model B went live:** 2026-11-02 ~11:07 EST (commit `ee7ac86`)

---

### Models

| | Model A (Control) | Model B (Variant) |
|---|---|---|
| **Label** | `heuristic` | `knn` |
| **Algorithm** | 4-pillar weighted formula | TF-IDF cosine similarity + k-NN |
| **Scoring pillars** | subject fit (55%), author bonus (20%), community (15%), length fit (10%) | Rating-weighted user taste vector ranked by cosine distance |
| **Era awareness** | ❌ None | ✅ Era bucket tokens in feature soup |
| **Language filtering** | ✅ Strict English / dynamic multi-language | ✅ Same (identical candidate pipeline) |
| **Candidate source** | Open Library Search API | Open Library Search API (identical) |
| **Live from** | Project start → 2026-11-02 | 2026-11-02 → ongoing |

---

### Hypothesis

> **H1 (Primary):** The k-NN model will achieve a higher user engagement rate (interested actions / shown recommendations) than the heuristic model, because cosine similarity in TF-IDF space captures latent genre and era preferences that the heuristic's hand-tuned weights miss.

> **H0 (Null):** There is no meaningful difference in engagement rate between the two models.

---

### Primary Success Metric

**Interested Rate** = `COUNT(action = 'interested') / COUNT(action = 'shown')` per model

A higher interested rate on k-NN than heuristic supports H1.

---

### Secondary Metrics

| Metric | Formula | Interpretation |
|---|---|---|
| Already-read rate | `COUNT(already_read) / COUNT(shown)` | High = model is surfacing books people already know → bad candidate novelty |
| Total engagement rate | `COUNT(interested + already_read) / COUNT(shown)` | Broader signal; already_read still means the book was relevant |

---

### Target Sample Size

- **Model A (heuristic):** 95 rows collected ✅ (7 friends, collected before 2026-11-02)
- **Model B (k-NN):** Target ≥ 95 rows from the same 7 friends

---

### Study Design & Known Limitations

**Design:** Within-subject sequential (same users, Model A first, Model B second)

This is **not** a randomised between-subjects design. Known confounds:

1. **Order effect** — heuristic always came first. Users may be more engaged on first use (novelty) or more discerning on repeat use (familiarity). This can bias results in either direction.
2. **Familiarity bias** — returning users know what the app does and may interact more deliberately the second time.
3. **Catalog drift** — if any participant updated their Goodreads between sessions, their taste profile changes, making the two runs non-comparable for that user.
4. **Social learning** — participants may have discussed recommendations between sessions.

**Mitigation:** Ask participants to use the same Goodreads export as their first session. Do not prompt them about what they saw previously.

**Interpretation:** Results should be treated as **directional evidence**, not a statistically rigorous causal claim. A formal between-subjects design (7 new users for k-NN) would be needed for stronger inference.

---

### What a Win Looks Like

| Outcome | Interpretation |
|---|---|
| k-NN interested rate > heuristic | H1 supported — cosine similarity captures taste better than hand-tuned weights |
| k-NN interested rate ≈ heuristic | Models are similarly effective; heuristic may be preferred for simplicity |
| k-NN interested rate < heuristic | k-NN underperforms; investigate feature soup quality (genre enrichment coverage) |
| k-NN already-read rate < heuristic | k-NN surfaces more novel books → positive signal even if interested rate is similar |

---

### Results (fill in after collection)

*To be completed after Model B data collection.*

| Metric | Model A (heuristic) | Model B (k-NN) |
|---|---|---|
| Rows collected | 95 | — |
| Books shown | — | — |
| Interested | — | — |
| Already read | — | — |
| **Interested rate** | — | — |
| Already-read rate | — | — |

---

### Conclusion (fill in after analysis)

*To be completed after evaluation.*
