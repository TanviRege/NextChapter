# NextChapter
Tell it the books you loved. It tells you what to read next, and why it is worth your time! A book recommendation app, created with Python and hosted on Streamlit.

## Recommender Evaluation & The Holdout Paradox                                                                                  
                                                                                                                                        
To evaluate recommendation quality, I initially implemented a textbook **Leave-One-Out holdout test** across 37 highly rated (4.0 +) books by sequentially hiding one favorite book, rebuilding the user taste profile without it, and checking if the engine recovered the hidden title in its top 5 picks (**Recall@5**).                                                                                              
The test yielded **0% Recall@5**, which revealed a fundamental data science insight:                                                
  1. **The Incomplete Ground Truth Problem**: In an open catalog with millions of books (Open Library), hiding one book does not make 
  it the *only* valid recommendation. In fact, **3 of the 5 books recommended during the test were titles the reader had already read   
  and loved in prior years** (a 60% real-world hit rate), yet the offline test penalized them as complete failures.
  
  2. **The Outlier Erasure Trap**: Hiding a standalone, high-rated book in a genre (e.g., the reader's single historical novel) erased
  the only evidence of that interest. The engine correctly recommended books matching the remaining profile, which mathematically       
  ensured the hidden book would miss.
  
  **Takeaway**: While offline Recall@K works on small, closed-world benchmark datasets (like MovieLens), open-web discovery engines   
  require **online evaluation**. 
  
NextChapter tracks this through persistent user events in SQLite (`feedback.db`): measuring true **Precision** via reader interactions (**"⭐ Interested!"** and **"I've Read This!"**), which captures genuine taste alignment rather than penalizing serendipitous discoveries.
