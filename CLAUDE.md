# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Fragrant Souq's **Competitor Table** service: a Flask app (deployed on Render, started by gunicorn) that downloads competitor perfume shops (UAE, prices in AED) into Postgres, matches them to our products, works out the least competitor price and a suggested price, and pushes prices and stock to Shopify. The UI is server-rendered HTML built inside Python (mainly `competitors_page.py`). There is no build step, no test suite and no linter.

**This repo is the TEST copy.** It runs the same code as production with `TEST_SITE=on`, its own database copy, and `SHOPIFY_SHOP`/`SHOPIFY_API_TOKEN` pointing at the **dev store (devfragrantsouq)**. Don't add code paths that touch the live store or the production database (`french_fragrance_db`, see `DB_IS_PRODUCTION` in `app.py`) when `TEST_SITE` is on.

## Commands

```bash
pip install -r requirements.txt              # flask, requests, gunicorn, psycopg2-binary, curl_cffi
python app.py                                # local run on $PORT (default 5000); needs FF_DATABASE_URL etc.
python suggested_price.py                    # self-test of the suggested-price rule (the only built-in test)
python sql.py "SELECT ... FROM competitor_table WHERE sku = 'X'"   # one-off SQL; reads FF_DATABASE_URL from daily_update.bat
python daily_update.py --test                # RUN 1 dry run (PC only, prints, saves nothing)
python daily_update.py --shop es | --brands "Dior,Chanel" | --full-lists --force
```

Shop jobs are started from the browser: `/<shop>/test?secret=RUN_SECRET` (reads page 1 only, saves nothing), then `/<shop>/run?secret=...`, then `/competitor/match-<prefix>?secret=...`. Progress is at `/<shop>/status`. The full ordered list is in the docstring at the top of `app.py`. Parts of that docstring are out of date (it still talks about Airtable), so go by the code.

## Architecture

**`app.py` is the hub** (~3,700 lines). It's split into numbered `SECTION n` comments (the file map is at the top of the file). It holds the matching logic (`match_one`), the `SHOPS` registry (prefix, loader and `trust_barcode` for each competitor), loading our products (`ct_load_fi`), the price SQL (`LEAST_SQL`, `SUGGEST_SQL`), background workers with a single `run_lock`, and most routes. Every other page or job is its own module exposing a Flask `Blueprint`, imported and registered in SECTION 1. A new page means a new file plus registering its blueprint there.

**Competitor shop modules** (`samawa_catalog.py`, `amberiya.py`, `ajmal.py`, …) each download one shop into `<shop>_catalog` and expose `/test`, `/run` and `/status`. Each shop has a 2-letter prefix (`vp`, `aj`, `on`, `am`, `ng`, `jm`, `ah`, …), used as the column prefix in `competitor_table` (`<prefix>_link`, `<prefix>_price`, stock). Adding a shop touches: the shop module, the `SHOPS` entry plus its `*_entries_from_db` loader in `app.py`, `SHOP_LISTS`/`COMPETITORS` in `db_triggers.py`, and the tabs in `competitors_page.py`. "Brands Store" shops and the shops listed as "NOT LINKED" (near SECTION 2) are downloaded and shown, but they are left out of matching and least price.

**`*_scraper.py` files run on the PC, not on Render.** Some shops block Render or need a real Chrome (`bp_scraper.py`, `es_scraper.py`, `on_scraper.py`, …). `daily_update.py` has two runs:
- RUN 1: Windows Task Scheduler runs `daily_update.bat` (gitignored; holds the DB URL and secret) on the PC.
- RUN 2: `run2_scheduler()` runs as a thread inside the website. `app.py` starts it at import, and `RUN2_SCHEDULER=off` disables it.

**Pricing pipeline.** The single rule is in `suggested_price.py`: cheapest in-stock competitor price minus X% (`discount_rate.py`), rounded half-up, then clamped to LOP/MOP (`price_limits.py`). **The same rule exists in three places**: `suggested_price.py`, `SUGGEST_SQL`/`price_limits.clamp_sql` in `app.py`, and the Postgres triggers in `db_triggers.py`. Change all three together. The triggers recalculate automatically:
- Trigger A: a competitor list's price changes, and the new price is copied into `competitor_table`.
- Trigger B: a row in `competitor_table` changes, and its least and suggested price are recalculated.

`price_triggers.recalc_product()` does the same for one product in Python. `skip_auto.py` excludes a product from automatic price updates.

**Airtable has been removed** (`AIRTABLE_ON`, default off). Our product list is now the `item_master` table (`item_master.py`, `item_create.py`, `item_edit.py`). Prices, barcodes and stock go straight to Shopify through the Admin GraphQL helpers in `shopify_create.py`/`item_edit.py` (`shopify_set_prices`, `shopify_set_uae_price`). Every Airtable write is guarded by `in_airtable(fid)`, which returns False when Airtable is off or on the test site. Product ids are `rec…` (from Airtable) or `new…` (created here).

**Database v2 (in progress, design of 25 Nov 2026).** New tables are built *next to* the old ones in 8 Postgres schemas (`ops`, `core`, `competitor`, `pricing`, `sales`, `crm`, `purchasing`, `extractor`). `db_v2/NNN_*.sql` are numbered migrations, each run once (tracked in `public.newdb_migrations`); never edit one that has run, add a new file. `newdb.py` applies them and copies reference data (`/newdb/test` = dry run that rolls back, `/newdb/run`, `/newdb/status`), and refuses to run on `french_fragrance_db`. Pages still read the old tables. Steps 3–8 of the plan (shop lists → `competitor.listings`, links → `competitor.matches`, page switch to `pricing.v_suggested_price`) are not done yet.

**Other cross-cutting pieces:**
- `auth.py`: logins stored in the `app_users` table (admin/user roles). Everything except `/`, `/login` and the secret-protected run/status/test URLs needs a login.
- `safe_output.py`: must be imported first in `app.py`. It stops stuck stdout from causing 500s under gunicorn.
- Edit modules (`link_edit.py`, `new_link.py`, `barcode_edit.py`, `stock_edit.py`, `bundle.py`): admin-only POST endpoints behind the buttons on the product cards.
- `app_settings` table: stores run progress and settings.

## Environment variables

`FF_DATABASE_URL` (Postgres), `RUN_SECRET`, `SECRET_KEY`, `TEST_SITE`, `SHOPIFY_SHOP`, `SHOPIFY_API_TOKEN` (or `SHOPIFY_CLIENT_ID`/`SHOPIFY_CLIENT_SECRET`), `SHOPIFY_API_VERSION`, `AIRTABLE`, `RUN2_SCHEDULER`, `BR_QUICK_CHECK`, `MATCH_THRESHOLD` (name-match score, default 0.85), `SUGGESTIONS`, `RESET_ADMIN` (emergency admin reset), `OUR_SITE`.

## Code conventions in this repo

- Comments and docstrings use plain, non-technical English for a non-developer owner. When behaviour changes because the user asked, add a dated comment quoting the request, e.g. `# TEST SITE (2026-11-24, user: "..."): ...`. Follow that style.
- Long jobs run in background threads that update a status dict polled by `/status` routes. Only one long job runs at a time (`run_lock`).
- Postgres access is raw `psycopg2` with `execute_values` for bulk upserts. Tables are created on demand with `CREATE TABLE IF NOT EXISTS` / `to_regclass(...)` checks. There are no migrations.
- `database doc.docx` documents the database tables.
