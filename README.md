# NSE / BSE Large Deals — two small ML studies

Two machine-learning studies built on India's public **bulk & block deal disclosures** (NSE + BSE, 2020–2026), and the data pipeline that feeds them. This is the code and data behind the passion project *"The Orderly Chaos of Money."*

**Live dashboard (Study 2):** https://sreenivas-sadhu-prabhakara.github.io/nse-large-deals/

---

## Study 1 — *Can we predict **who** will trade?*
Rank 584 institutional houses by their chance of **buying** on a specific future day, frozen five days early and graded blind.

- Frozen Fri **15 Jul 2026**, graded Mon **20 Jul 2026** — a genuine 5-day-ahead, out-of-sample test.
- **HistGradientBoosting** (primary) + **KNN** (k=300) cross-check on a leakage-safe walk-forward panel.
- Result: **84%** of the shortlist bought (16/19), top-5 = **5/5**, live **AUC 0.949**, ×21 lift, turnover called to 93%.
- Honest limit: it predicts habitual *market-makers* (net position ≈ 0), and is blind to ~⅓ first-time buyers.
- Notebooks: [`notebooks/Predict_House_Investments_Jul20.ipynb`](notebooks/Predict_House_Investments_Jul20.ipynb), [`notebooks/KNN_House_Prediction.ipynb`](notebooks/KNN_House_Prediction.ipynb)
- Interactive telling: **https://scrooge-crystal-ball.pages.dev**

## Study 2 — *Did the AI era make trading more profitable?*
Measure same-day round-trip market-maker profit, mark every major LLM launch, and check for a break.

- ~**₹619 cr** of round-trip proxy profit over **38,452** round-trips.
- Monthly profit **×2.7**, but volume **×3.4** → value-weighted edge drifted **9.8 → 7.7 bps**.
- No structural break at any AI date; the slide predates ChatGPT. **Bigger, not smarter** — correlation, not cause.
- Notebook: [`notebooks/AI_Era_Trading_Profits.ipynb`](notebooks/AI_Era_Trading_Profits.ipynb) · Dashboard: [`index.html`](index.html)

---

## The data pipeline
[`india_large_deals_downloader.py`](india_large_deals_downloader.py) downloads and merges NSE + BSE bulk & block deal disclosures from 2020-01-01 through today. See [`PIPELINE.md`](PIPELINE.md) for full usage.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python india_large_deals_downloader.py
```

The notebooks run end-to-end on **Google Colab** — upload the generated CSV/SQLite and Run all. The models and their plain-English explanations come from the free [**Learn ML Models**](https://sreenivas-sadhu-prabhakara.github.io/learn-ml-models/) course.

---

*Built from public NSE/BSE disclosures. **Educational use only — not investment advice**; past model performance never guarantees future results. "Duck house" characters on the Study-1 site are a storytelling wrapper for real institutional trading houses.*
