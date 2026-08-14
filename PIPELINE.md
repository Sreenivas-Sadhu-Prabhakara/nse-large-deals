# India Large Deals Downloader

This package downloads and merges **NSE and BSE bulk and block deal disclosures** from **1 January 2020 through today**.

## Output columns

- `deal_date`
- `exchange`
- `deal_type`
- `side`
- `is_purchase`
- `symbol`
- `security_code`
- `security_name`
- `client_name`
- `quantity`
- `price_inr`
- `trade_value_inr`
- `trade_value_crore`
- `remarks`
- source date window, URL and download timestamp

`trade_value_inr = quantity × price_inr`.

## Run on macOS, Linux or Windows

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python india_large_deals_downloader.py
```

On Windows PowerShell, activate with:

```powershell
.venv\Scripts\Activate.ps1
```

The default command downloads:

- NSE bulk deals
- NSE block deals
- BSE bulk deals
- BSE block deals
- 2020-01-01 through the current date
- one calendar month at a time

It creates:

```text
india_large_deals_output/
├── india_bulk_block_deals_2020_to_today.csv
├── india_large_deals.sqlite
├── failed_windows.txt                 # only if a window failed
└── raw/
    ├── nse/bulk/
    ├── nse/block/
    ├── bse/bulk/
    └── bse/block/
```

The CSV uses UTF-8 with a BOM, so it opens cleanly in Excel.

## Useful commands

Only purchase rows:

```bash
python india_large_deals_downloader.py --buys-only
```

Only deals worth at least ₹5 crore:

```bash
python india_large_deals_downloader.py --min-value-crore 5
```

Exact date range:

```bash
python india_large_deals_downloader.py \
  --start 2020-01-01 \
  --end 2026-07-15
```

NSE only:

```bash
python india_large_deals_downloader.py --exchanges NSE
```

Force a complete refresh:

```bash
python india_large_deals_downloader.py --force
```

Offline parser test:

```bash
python india_large_deals_downloader.py --self-test
```

## Resume behaviour

The script records every successfully completed exchange/type/month in SQLite. If it is interrupted or an endpoint temporarily fails, run the same command again. Completed windows are skipped and failed windows are retried.

## NSE anti-bot fallback

The script first uses a normal cookie-aware `requests` session. If NSE returns an anti-bot response, it can automatically use a persistent Playwright browser.

Install the optional fallback:

```bash
pip install -r requirements-browser.txt
playwright install chromium
```

Then run the normal command again. No command-line change is required.

To explicitly disable the browser fallback:

```bash
python india_large_deals_downloader.py --no-browser-fallback
```

## Important interpretation note

A disclosure is not necessarily a single exchange execution. Bulk-deal records can represent a participant’s disclosed aggregate activity in a security during the day. Block deals are separately reported large transactions. The dataset is therefore a dataset of **exchange-disclosed large deals**, not every tick-level trade.
