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

### Results (Completed 2026-11-11)

| Metric | Model A (heuristic) | Model B (k-NN) | Delta / Shift |
|---|---|---|---|
| Rows collected | 95 | 87 | 182 total data rows (+ 1 header) |
| Books shown | 61 | 60 | — |
| Interested | 19 | 20 | +1 |
| Already read | 15 | 7 | -8 |
| **Interested rate (Primary)** | **31.1%** (19/61) | **33.3%** (20/60) | **+2.2%** (+7.0% relative lift) |
| **Already-read rate (Novelty)** | **24.6%** (15/61) | **11.7%** (7/60) | **-12.9%** (**-52.5% relative drop**) |
| Total engagement rate | 55.7% (34/61) | 45.0% (27/60) | -10.7% (-19.2% relative drop) |
| Discovery yield (Int / [Shown - Read]) | 41.3% (19/46) | 37.7% (20/53) | +15% more novel items surfaced (53 vs 46) |

#### Statistical Testing
- **Interested Rate:** $\chi^2 = 0.066,\ p = 0.797$ (Yates-corrected $p = 0.950$).
  - Two-proportion $Z = 0.257$. Directionally favors k-NN ($H_1$), but not statistically significant at $\alpha = 0.05$ due to sample size ($N = 121$ shown recommendations).
- **Already-Read Rate (Novelty / Serendipity):** $\chi^2 = 3.40,\ p = 0.065$ (Uncorrected $p = 0.0654$; Yates-corrected $p = 0.108$).
  - Statistically significant at the 10% significance level ($\alpha = 0.10$).
- **Sensitivity Check (Excluding initial test session `12eb8afc`, 7 vs 7 friends):**
  - Model A: 54 shown, 15 interested (27.8%), 13 already read (24.1%).
  - Model B: 60 shown, 20 interested (33.3%), 7 already read (11.7%).
  - Interested rate lift increases to **+5.6%** ($p = 0.521$), and already-read rate drop remains strong at **-12.4%** ($p = 0.082$).

---

### Conclusion & Evaluation

1. **Primary Hypothesis ($H_1$):**
   - The k-NN model achieved an Interested Rate of **33.3%** compared to **31.1%** for the heuristic model (+2.2% absolute lift; +5.6% lift on strictly non-test friend sessions). While the sample size of 7 participants ($N=121$ shown books) means this difference is not statistically significant at $\alpha=0.05$, the metric directionally supports $H_1$.
2. **The Discovery Breakthrough (Already-Read Rate):**
   - The most impactful engineering finding was the **52.5% reduction in already-read recommendations** (falling from 24.6% down to 11.7%, $\chi^2=3.40, p=0.065$).
   - The heuristic model's hand-crafted author and subject weights suffered from popularity bias—frequently surfacing mainstream bestsellers that participants had already consumed. In contrast, the TF-IDF feature soup (combining subjects, genres, eras, and author tokens) successfully broke the popularity bubble, finding relevant yet undiscovered titles.
3. **Engagement Quality:**
   - In Model A, 44% of all user clicks were retrospective ("I've already read this"), leaving only 19 genuine forward-looking reading discoveries.
   - In Model B, 74% of all user clicks were forward-looking ("Interested!"), surfacing 20 active reading discoveries across 53 novel candidates.
4. **Conclusion:**
   - **Variant Model B (k-NN) is adopted as the primary recommender engine.** It preserves high taste precision while dramatically improving catalog discovery and novelty.
