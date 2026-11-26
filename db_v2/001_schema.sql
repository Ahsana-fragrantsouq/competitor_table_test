-- =====================================================================
-- FragrantSouq platform database — schema v2 (design, 25 Nov 2026)
-- PostgreSQL 14+
--
-- Replaces french_fragrance_db's 38 tables. Covers today's features
-- (competitor table, shop lists, Item Master, bundles, settings, users)
-- plus the new ones (CRM, photo extractor, cost-based price floor)
-- and likely next ones (purchasing, marketplaces, stock, price history,
-- WhatsApp campaigns, alerts, role permissions).
--
-- Rules used everywhere:
--   * every time is TIMESTAMPTZ (PC in Dubai and Render in UTC agree)
--   * money is NUMERIC(12,2); competitor prices are always stored WITH VAT
--   * one row per thing (no column group per shop, no table per shop)
--   * foreign keys and CHECKs enforce what the old code had to remember
--   * created_by / updated_by hold the username (TEXT) for readable logs
--
-- Applied by newdb.py (/newdb/test then /newdb/run). Runs ONCE: the number is
-- saved in table public.newdb_migrations. Never edit this file after it has
-- run on a database - put changes in a new file 002_....sql instead.
--
-- Changes to the 25 Nov design, made while adding it to the code (2026-11-26):
--   [A] core.products.legacy_id: the old id (rec... / new...) so step 4 can
--       turn competitor_table rows (fi_record_id) into matches rows.
--   [B] core.brands.sku_prefix may be empty: some brands in item_master /
--       brand_discount have no row in brand_sku yet. Such a brand cannot get
--       a new SKU until a prefix is typed (next_sku() says so).
--   [C] suggested price rounded to whole AED, 0.50 goes up - the SAME as
--       today's rule (suggested_price.py DECIMALS = 0), so old and new
--       suggested prices can be compared in step 5.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fast "contains" search on names
CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive brand / user names

CREATE SCHEMA IF NOT EXISTS ops;          -- users, roles, settings, jobs, audit, alerts
CREATE SCHEMA IF NOT EXISTS core;         -- brands, products, prices, costs, stock, channels
CREATE SCHEMA IF NOT EXISTS competitor;   -- shops, shop listings, matching, price history
CREATE SCHEMA IF NOT EXISTS pricing;      -- limits, suggested price, price decisions
CREATE SCHEMA IF NOT EXISTS sales;        -- bundles, orders, payments
CREATE SCHEMA IF NOT EXISTS crm;          -- customers, leads, WhatsApp messages, campaigns
CREATE SCHEMA IF NOT EXISTS purchasing;   -- suppliers, purchase invoices (cost source)
CREATE SCHEMA IF NOT EXISTS extractor;    -- photo extractor

-- Shared trigger: keep updated_at current
CREATE OR REPLACE FUNCTION ops.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;


-- =====================================================================
-- OPS — users, roles, settings, jobs, audit, alerts
-- =====================================================================

CREATE TABLE ops.roles (
  code        TEXT PRIMARY KEY,                       -- main_admin, admin, user, viewer ...
  name        TEXT NOT NULL,
  description TEXT
);

CREATE TABLE ops.permissions (
  code        TEXT PRIMARY KEY,                       -- e.g. pricing.update, crm.view, users.manage
  description TEXT NOT NULL
);

CREATE TABLE ops.role_permissions (
  role_code       TEXT NOT NULL REFERENCES ops.roles ON DELETE CASCADE,
  permission_code TEXT NOT NULL REFERENCES ops.permissions ON DELETE CASCADE,
  PRIMARY KEY (role_code, permission_code)
);

CREATE TABLE ops.users (
  username      CITEXT PRIMARY KEY,
  display_name  TEXT,
  password_hash TEXT NOT NULL,
  role_code     TEXT NOT NULL REFERENCES ops.roles,
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  failed_logins INTEGER NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
  locked_until  TIMESTAMPTZ,
  last_login    TIMESTAMPTZ,
  phone_e164    TEXT,                                 -- for WhatsApp assignment / alerts
  created_by    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Simple key/value settings (discount rate, margin %, brands per day ...)
CREATE TABLE ops.settings (
  key         TEXT PRIMARY KEY,
  value       JSONB NOT NULL,
  description TEXT,
  updated_by  TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Progress of long-running schedules (Run 1 on PC, Run 2 on Render): one row each,
-- so the PC and the website never overwrite each other's progress.
CREATE TABLE ops.run_state (
  run_code    TEXT PRIMARY KEY,                       -- run1_daily_brands, run2_full_lists ...
  state       JSONB NOT NULL DEFAULT '{}',
  updated_by  TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every job / button run / scrape, with step log (replaces button_runs,
-- price_check_runs and the results stored inside app_settings)
CREATE TABLE ops.job_runs (
  id           BIGSERIAL PRIMARY KEY,
  job          TEXT NOT NULL,                         -- scrape, create_in_website, create_description, shopify_sync ...
  trigger      TEXT NOT NULL DEFAULT 'manual'
               CHECK (trigger IN ('manual','schedule','webhook','system')),
  shop_code    TEXT,                                  -- for scrapes (FK added after competitor.shops)
  product_id   BIGINT,                                -- for product buttons (FK added after core.products)
  started_by   TEXT,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ,
  status       TEXT NOT NULL DEFAULT 'running'
               CHECK (status IN ('running','done','partial','error','cancelled')),
  stats        JSONB NOT NULL DEFAULT '{}',           -- {"checked":219,"changed":2,"new":0}
  steps        JSONB NOT NULL DEFAULT '[]',
  error        TEXT
);
CREATE INDEX ON ops.job_runs (job, started_at DESC);
CREATE INDEX ON ops.job_runs (product_id) WHERE product_id IS NOT NULL;
CREATE INDEX ON ops.job_runs (shop_code, started_at DESC) WHERE shop_code IS NOT NULL;

-- Who changed what (written by app or by audit triggers on key tables)
CREATE TABLE ops.audit_log (
  id          BIGSERIAL PRIMARY KEY,
  table_name  TEXT NOT NULL,
  record_id   TEXT NOT NULL,
  action      TEXT NOT NULL CHECK (action IN ('insert','update','delete')),
  changed_by  TEXT,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  old_values  JSONB,
  new_values  JSONB
);
CREATE INDEX ON ops.audit_log (table_name, record_id, changed_at DESC);

-- Alerts shown in the app / sent to WhatsApp (price drop, below cost, scrape failed ...)
CREATE TABLE ops.alerts (
  id          BIGSERIAL PRIMARY KEY,
  type        TEXT NOT NULL,                          -- competitor_price_drop, below_cost, scrape_failed, stockout, new_competitor_product
  severity    TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','critical')),
  product_id  BIGINT,
  shop_code   TEXT,
  message     TEXT NOT NULL,
  payload     JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_by TEXT,
  resolved_at TIMESTAMPTZ
);
CREATE INDEX ON ops.alerts (created_at DESC) WHERE resolved_at IS NULL;


-- =====================================================================
-- CORE — brands, options, products, images, markets, prices, costs, stock
-- =====================================================================

-- One brands table: replaces brand_sku, brand_automation, brand_discount
CREATE TABLE core.brands (
  id                 SERIAL PRIMARY KEY,
  name               CITEXT NOT NULL UNIQUE,           -- case-insensitive: "Armaf" = "ARMAF"
  sku_prefix         TEXT UNIQUE CHECK (sku_prefix ~ '^[A-Z]{3,4}$'),   -- [B] empty = no SKUs yet
  next_sku_number    INTEGER NOT NULL DEFAULT 1001 CHECK (next_sku_number >= 1001),
  auto_price         BOOLEAN NOT NULL DEFAULT FALSE,   -- UAE price follows suggested price
  discount_pct       NUMERIC(5,2) CHECK (discount_pct BETWEEN 0 AND 100),     -- NULL = general rate
  min_margin_pct     NUMERIC(5,2) CHECK (min_margin_pct BETWEEN 0 AND 500),   -- NULL = general margin (cost floor)
  country_of_origin  TEXT,
  established_year   SMALLINT,
  description        TEXT,
  logo_url           TEXT,
  airtable_id        TEXT UNIQUE,
  trendyol_brand_id  BIGINT,
  aliases            TEXT[] NOT NULL DEFAULT '{}',     -- spellings seen on shops / photos ("Maison Francis Kurkdjian Paris")
  created_by         TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by         TEXT,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER brands_touch BEFORE UPDATE ON core.brands
  FOR EACH ROW EXECUTE FUNCTION ops.touch_updated_at();

-- Dropdown values (size, type, category, product type, concentration, condition ...)
-- Replaces item_options; one place for the photo extractor and Item Master.
CREATE TABLE core.option_values (
  field       TEXT NOT NULL,                          -- size, type, category, product_type, condition
  value       TEXT NOT NULL,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  synonyms    TEXT[] NOT NULL DEFAULT '{}',           -- "Edp","Eau de Parfum" -> EDP
  added_by    TEXT,
  added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (field, value)
);
CREATE UNIQUE INDEX option_values_ci ON core.option_values (field, lower(value));

-- Our products (merges item_master + own-product part of competitor_table)
CREATE TABLE core.products (
  id                BIGSERIAL PRIMARY KEY,
  sku               TEXT NOT NULL UNIQUE,              -- AMF1085, bundles use sales.bundles
  brand_id          INTEGER NOT NULL REFERENCES core.brands,
  perfume_name      TEXT NOT NULL,
  product_name      TEXT NOT NULL,                     -- Brand + Perfume + Size + Type + Category + Product type
  product_type      TEXT,                              -- Perfume, Body Mist, Gift Set ...
  concentration     TEXT,                              -- EDP, EDT, Extrait ...
  gender            TEXT CHECK (gender IN ('Men','Women','Unisex')),
  size_ml           NUMERIC(7,2) CHECK (size_ml > 0),
  size_label        TEXT,                              -- "100 ml", "3 x 30 ml"
  barcode           TEXT CHECK (barcode ~ '^[0-9]{8,14}$'),
  is_tester         BOOLEAN NOT NULL DEFAULT FALSE,
  is_gift_set       BOOLEAN NOT NULL DEFAULT FALSE,
  gift_items        TEXT,
  origin            TEXT,
  top_notes         TEXT,
  middle_notes      TEXT,
  base_notes        TEXT,
  description_html  TEXT,
  description_at    TIMESTAMPTZ,
  status            TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('draft','active','discontinued','deleted')),
  airtable_id       TEXT UNIQUE,                       -- recXXX (old fi_record_id) while Airtable is still used
  legacy_id         TEXT UNIQUE,                       -- [A] old fi_record_id (rec... or new...), for the move
  source           TEXT NOT NULL DEFAULT 'manual',    -- manual, airtable, photo_extractor, import
  edited_fields     JSONB NOT NULL DEFAULT '{}',       -- field -> {by, at} for edits not yet pushed
  created_by        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by        TEXT,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_by        TEXT,
  deleted_at        TIMESTAMPTZ
);
CREATE INDEX ON core.products (brand_id);
CREATE INDEX ON core.products (barcode);
CREATE INDEX products_name_trgm ON core.products USING gin (product_name gin_trgm_ops);
CREATE TRIGGER products_touch BEFORE UPDATE ON core.products
  FOR EACH ROW EXECUTE FUNCTION ops.touch_updated_at();

CREATE TABLE core.product_images (
  id          BIGSERIAL PRIMARY KEY,
  product_id  BIGINT NOT NULL REFERENCES core.products ON DELETE CASCADE,
  url         TEXT NOT NULL,
  source      TEXT NOT NULL CHECK (source IN ('shopify','cloudinary','competitor','brand_site','extractor','upload')),
  position    SMALLINT NOT NULL DEFAULT 1,
  width       INTEGER,
  height      INTEGER,
  approved    BOOLEAN NOT NULL DEFAULT FALSE,
  added_by    TEXT,
  added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (product_id, url)
);

-- Sales channels: our website markets and marketplaces (one row each)
CREATE TABLE core.channels (
  code        TEXT PRIMARY KEY,                        -- web_uae, web_asia, web_kuwait, web_america, web_qatar,
                                                       -- trendyol, amazon_ae, noon, ebay, tiktok_us, whatsapp, store
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('website','marketplace','social','offline')),
  country     TEXT,
  currency    CHAR(3) NOT NULL DEFAULT 'AED',
  fee_pct     NUMERIC(5,2),                            -- platform commission, for profit / floor
  active      BOOLEAN NOT NULL DEFAULT TRUE
);

-- Selling price per product per channel (replaces uae_price, asia_price ... columns
-- and the Marketplace Management / Shopify Inventory tables)
CREATE TABLE core.product_listings (
  product_id          BIGINT NOT NULL REFERENCES core.products ON DELETE CASCADE,
  channel_code        TEXT   NOT NULL REFERENCES core.channels,
  price               NUMERIC(12,2) CHECK (price >= 0),
  compare_at_price    NUMERIC(12,2) CHECK (compare_at_price >= 0),
  currency            CHAR(3) NOT NULL DEFAULT 'AED',
  external_id         TEXT,                            -- Shopify product id, ASIN, Trendyol id ...
  external_variant_id TEXT,
  qty_listed          INTEGER,
  status              TEXT NOT NULL DEFAULT 'not_listed'
                      CHECK (status IN ('not_listed','draft','active','paused','rejected')),
  lowest_rival_price  NUMERIC(12,2),                   -- marketplace least price (ALP / TLP / NLP)
  price_source        TEXT,                            -- manual, auto, airtable, shopify
  last_synced_at      TIMESTAMPTZ,
  updated_by          TEXT,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (product_id, channel_code)
);
CREATE INDEX ON core.product_listings (channel_code, external_id);

-- Cost history: every cost change (purchase, manual, Airtable, EQL Plus).
-- Current cost = latest row (view core.v_current_cost).
CREATE TABLE core.cost_history (
  id               BIGSERIAL PRIMARY KEY,
  product_id       BIGINT NOT NULL REFERENCES core.products ON DELETE CASCADE,
  unit_cost        NUMERIC(12,2) NOT NULL CHECK (unit_cost >= 0),
  vat_included     BOOLEAN NOT NULL DEFAULT FALSE,
  currency         CHAR(3) NOT NULL DEFAULT 'AED',
  source           TEXT NOT NULL CHECK (source IN ('purchase','manual','airtable','eql','import')),
  purchase_line_id BIGINT,                             -- FK added after purchasing
  effective_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by       TEXT,
  note             TEXT
);
CREATE INDEX ON core.cost_history (product_id, effective_at DESC);

-- Stock per location (warehouse, Shopify, Amazon FBA ...) and every movement
CREATE TABLE core.locations (
  code   TEXT PRIMARY KEY,                             -- warehouse_dxb, shopify, amazon_fba, trendyol
  name   TEXT NOT NULL
);

CREATE TABLE core.stock_levels (
  product_id    BIGINT NOT NULL REFERENCES core.products ON DELETE CASCADE,
  location_code TEXT   NOT NULL REFERENCES core.locations,
  qty           INTEGER NOT NULL DEFAULT 0,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (product_id, location_code)
);

CREATE TABLE core.stock_movements (
  id            BIGSERIAL PRIMARY KEY,
  product_id    BIGINT NOT NULL REFERENCES core.products,
  location_code TEXT   NOT NULL REFERENCES core.locations,
  qty_change    INTEGER NOT NULL CHECK (qty_change <> 0),
  reason        TEXT NOT NULL CHECK (reason IN ('purchase','sale','return','adjustment','transfer','bundle','sync')),
  ref_type      TEXT,                                  -- order_line, purchase_line ...
  ref_id        BIGINT,
  created_by    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON core.stock_movements (product_id, created_at DESC);

-- Keep stock_levels in step with movements
CREATE OR REPLACE FUNCTION core.apply_stock_movement() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO core.stock_levels (product_id, location_code, qty, updated_at)
  VALUES (NEW.product_id, NEW.location_code, NEW.qty_change, now())
  ON CONFLICT (product_id, location_code)
  DO UPDATE SET qty = core.stock_levels.qty + NEW.qty_change, updated_at = now();
  RETURN NEW;
END $$;
CREATE TRIGGER stock_movement_apply AFTER INSERT ON core.stock_movements
  FOR EACH ROW EXECUTE FUNCTION core.apply_stock_movement();

-- Atomic next SKU for a brand: two users creating items at once never get the same SKU
CREATE OR REPLACE FUNCTION core.next_sku(p_brand_id INTEGER) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE v_prefix TEXT; v_num INTEGER;
BEGIN
  UPDATE core.brands SET next_sku_number = next_sku_number + 1
   WHERE id = p_brand_id
  RETURNING sku_prefix, next_sku_number - 1 INTO v_prefix, v_num;
  IF NOT FOUND THEN RAISE EXCEPTION 'brand % not found', p_brand_id; END IF;
  IF v_prefix IS NULL THEN RAISE EXCEPTION 'brand % has no SKU prefix yet', p_brand_id; END IF;   -- [B]
  RETURN v_prefix || v_num;
END $$;


-- =====================================================================
-- COMPETITOR — shops, listings, matching, price history
-- =====================================================================

-- One row per shop (replaces 22 tables and 22 column groups).
-- Adding a shop = one INSERT here.
CREATE TABLE competitor.shops (
  code              TEXT PRIMARY KEY,                  -- samawa, ff, bp, es, vp, aj, on, am, ng, pd, mh, br, ep ...
  name              TEXT NOT NULL,
  kind              TEXT NOT NULL CHECK (kind IN ('competitor','brand_store','marketplace','physical')),
  website           TEXT,
  platform          TEXT,                              -- shopify, woocommerce, custom
  refresh_method    TEXT CHECK (refresh_method IN ('render','pc_chrome','follows','manual')),
  refresh_days      SMALLINT CHECK (refresh_days > 0), -- 1 = daily, 5, 15 ...
  max_age_days      SMALLINT NOT NULL DEFAULT 7 CHECK (max_age_days > 0), -- older prices do not count
  follows_shop      TEXT REFERENCES competitor.shops,  -- bp and es follow ff
  has_gtin          BOOLEAN NOT NULL DEFAULT TRUE,     -- Ounass = FALSE -> name match only
  used_for_pricing  BOOLEAN NOT NULL DEFAULT TRUE,     -- br, pd = FALSE (list only)
  active            BOOLEAN NOT NULL DEFAULT TRUE,
  last_success_at   TIMESTAMPTZ,
  last_error        TEXT,
  sort_order        SMALLINT NOT NULL DEFAULT 0
);

ALTER TABLE ops.job_runs ADD FOREIGN KEY (shop_code) REFERENCES competitor.shops;
ALTER TABLE ops.alerts   ADD FOREIGN KEY (shop_code) REFERENCES competitor.shops;

-- Every product (and every size = variant) seen on every shop
CREATE TABLE competitor.listings (
  id                   BIGSERIAL PRIMARY KEY,
  shop_code            TEXT NOT NULL REFERENCES competitor.shops,
  url                  TEXT NOT NULL,
  external_product_id  TEXT,
  external_variant_id  TEXT,                            -- one row per size: fixes the wrong-size price risk
  gtin                 TEXT,
  shop_sku             TEXT,
  brand_raw            TEXT,
  brand_id             INTEGER REFERENCES core.brands,  -- resolved brand (via name / aliases)
  name                 TEXT NOT NULL,
  product_type         TEXT,
  size_ml              NUMERIC(7,2),
  size_label           TEXT,
  concentration        TEXT,
  is_tester            BOOLEAN NOT NULL DEFAULT FALSE,
  is_gift_set          BOOLEAN NOT NULL DEFAULT FALSE,
  image_url            TEXT,
  price_inc_vat        NUMERIC(12,2) CHECK (price_inc_vat >= 0),   -- ALWAYS with 5% VAT
  stock_status         TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (stock_status IN ('in_stock','out_of_stock','not_on_site','unknown')),
  check_status         TEXT NOT NULL DEFAULT 'ok'
                       CHECK (check_status IN ('ok','page_missing','failed')),
  first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_checked_at      TIMESTAMPTZ,                     -- last time we TRIED
  last_success_at      TIMESTAMPTZ,                     -- last time price/stock was read OK (freshness)
  price_changed_at     TIMESTAMPTZ,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- one row per shop + page + size (NULL variant treated as one)
CREATE UNIQUE INDEX listings_unique ON competitor.listings
  (shop_code, url, COALESCE(external_variant_id, ''));
CREATE INDEX ON competitor.listings (shop_code, gtin);
CREATE INDEX ON competitor.listings (gtin) WHERE gtin IS NOT NULL;
CREATE INDEX ON competitor.listings (brand_id);
CREATE INDEX ON competitor.listings (shop_code, first_seen_at DESC);   -- "new products" tab
CREATE INDEX listings_name_trgm ON competitor.listings USING gin (name gin_trgm_ops);
ALTER TABLE competitor.listings SET (fillfactor = 85);                 -- frequent price updates

-- Price / stock history, written only when something changes
CREATE TABLE competitor.listing_history (
  id             BIGSERIAL PRIMARY KEY,
  listing_id     BIGINT NOT NULL REFERENCES competitor.listings ON DELETE CASCADE,
  price_inc_vat  NUMERIC(12,2),
  stock_status   TEXT NOT NULL,
  observed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON competitor.listing_history (listing_id, observed_at DESC);

CREATE OR REPLACE FUNCTION competitor.track_listing_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT'
     OR NEW.price_inc_vat IS DISTINCT FROM OLD.price_inc_vat
     OR NEW.stock_status  IS DISTINCT FROM OLD.stock_status THEN
    INSERT INTO competitor.listing_history (listing_id, price_inc_vat, stock_status, observed_at)
    VALUES (NEW.id, NEW.price_inc_vat, NEW.stock_status, now());
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER listing_history_ins AFTER INSERT ON competitor.listings
  FOR EACH ROW EXECUTE FUNCTION competitor.track_listing_change();
CREATE TRIGGER listing_history_upd AFTER UPDATE OF price_inc_vat, stock_status ON competitor.listings
  FOR EACH ROW
  WHEN (OLD.price_inc_vat IS DISTINCT FROM NEW.price_inc_vat
        OR OLD.stock_status IS DISTINCT FROM NEW.stock_status)
  EXECUTE FUNCTION competitor.track_listing_change();

CREATE OR REPLACE FUNCTION competitor.listing_before_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  IF NEW.price_inc_vat IS DISTINCT FROM OLD.price_inc_vat THEN NEW.price_changed_at := now(); END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER listing_touch BEFORE UPDATE ON competitor.listings
  FOR EACH ROW EXECUTE FUNCTION competitor.listing_before_update();

-- The confirmed / automatic match: one row per our product per shop,
-- pointing at the exact listing row (exact size). Price is read through the
-- join, so nothing is copied and there are no per-shop copy triggers.
CREATE TABLE competitor.matches (
  product_id   BIGINT NOT NULL REFERENCES core.products ON DELETE CASCADE,
  shop_code    TEXT   NOT NULL REFERENCES competitor.shops,
  listing_id   BIGINT NOT NULL REFERENCES competitor.listings ON DELETE CASCADE,
  method       TEXT NOT NULL CHECK (method IN ('barcode','fuzzy','manual','follows')),
  score        NUMERIC(5,3) CHECK (score BETWEEN 0 AND 1),
  confirmed    BOOLEAN NOT NULL DEFAULT FALSE,          -- a person clicked OK / Edit link
  matched_by   TEXT,
  matched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (product_id, shop_code)
);
CREATE INDEX ON competitor.matches (listing_id);

-- Suggestions waiting for OK / Not OK, and every decision ever made
-- (all rejected links are remembered, not just the last one)
CREATE TABLE competitor.match_suggestions (
  id           BIGSERIAL PRIMARY KEY,
  product_id   BIGINT NOT NULL REFERENCES core.products ON DELETE CASCADE,
  listing_id   BIGINT NOT NULL REFERENCES competitor.listings ON DELETE CASCADE,
  shop_code    TEXT   NOT NULL REFERENCES competitor.shops,
  method       TEXT NOT NULL,
  score        NUMERIC(5,3),
  status       TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','accepted','rejected','removed')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_by   TEXT,
  decided_at   TIMESTAMPTZ,
  UNIQUE (product_id, listing_id)
);
CREATE INDEX ON competitor.match_suggestions (status, shop_code) WHERE status = 'pending';


-- =====================================================================
-- PRICING — limits, calculated price, decisions log / push queue
-- =====================================================================

-- Per product settings for automatic pricing (one row per product, web_uae)
CREATE TABLE pricing.product_rules (
  product_id     BIGINT PRIMARY KEY REFERENCES core.products ON DELETE CASCADE,
  lop            NUMERIC(12,2) CHECK (lop >= 0),        -- price floor
  lop_source     TEXT NOT NULL DEFAULT 'none'
                 CHECK (lop_source IN ('none','manual','cost')),  -- 'cost' = follows cost x (1 + margin)
  mop            NUMERIC(12,2) CHECK (mop >= 0),        -- price ceiling (manual)
  no_auto        BOOLEAN NOT NULL DEFAULT FALSE,        -- Skip automatic price
  limits_by      TEXT,
  limits_at      TIMESTAMPTZ,
  no_auto_by     TEXT,
  no_auto_at     TIMESTAMPTZ,
  CHECK (lop IS NULL OR mop IS NULL OR lop <= mop)
);

-- Every price decision: suggested, approved, sent, failed. Doubles as the push
-- queue to Shopify / Airtable and as the audit trail ("why did this price change?")
CREATE TABLE pricing.price_decisions (
  id                   BIGSERIAL PRIMARY KEY,
  product_id           BIGINT NOT NULL REFERENCES core.products,
  channel_code         TEXT   NOT NULL DEFAULT 'web_uae' REFERENCES core.channels,
  old_price            NUMERIC(12,2),
  new_price            NUMERIC(12,2) NOT NULL CHECK (new_price >= 0),
  cheapest_listing_id  BIGINT REFERENCES competitor.listings,
  cheapest_price       NUMERIC(12,2),
  discount_pct         NUMERIC(5,2),
  unit_cost            NUMERIC(12,2),
  lop                  NUMERIC(12,2),
  mop                  NUMERIC(12,2),
  reason               TEXT NOT NULL CHECK (reason IN ('auto','manual','bulk','cost_change','campaign')),
  status               TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','sent','failed','skipped','superseded')),
  skip_reason          TEXT,                            -- no_auto ticked, price unchanged, newer manual edit ...
  target               TEXT NOT NULL DEFAULT 'shopify' CHECK (target IN ('shopify','airtable','marketplace')),
  attempts             SMALLINT NOT NULL DEFAULT 0,
  error                TEXT,
  created_by           TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at              TIMESTAMPTZ
);
CREATE INDEX ON pricing.price_decisions (product_id, created_at DESC);
CREATE INDEX ON pricing.price_decisions (status) WHERE status IN ('pending','failed');


-- =====================================================================
-- PURCHASING — suppliers and purchase invoices (source of cost)
-- =====================================================================

CREATE TABLE purchasing.suppliers (
  id           SERIAL PRIMARY KEY,
  name         CITEXT NOT NULL UNIQUE,
  phone        TEXT,
  email        TEXT,
  website      TEXT,
  social_url   TEXT,
  address      TEXT,
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','blocked')),
  notes        TEXT,
  airtable_id  TEXT UNIQUE,
  eql_code     TEXT,
  created_by   TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE purchasing.supplier_contacts (
  id           SERIAL PRIMARY KEY,
  supplier_id  INTEGER NOT NULL REFERENCES purchasing.suppliers ON DELETE CASCADE,
  name         TEXT NOT NULL,
  designation  TEXT,
  mobile       TEXT,
  email        TEXT
);

-- Supplier price quotes (old "Price checking")
CREATE TABLE purchasing.supplier_prices (
  supplier_id  INTEGER NOT NULL REFERENCES purchasing.suppliers ON DELETE CASCADE,
  product_id   BIGINT  NOT NULL REFERENCES core.products ON DELETE CASCADE,
  unit_price   NUMERIC(12,2) NOT NULL CHECK (unit_price >= 0),
  checked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (supplier_id, product_id)
);

CREATE TABLE purchasing.invoices (
  id              BIGSERIAL PRIMARY KEY,
  supplier_id     INTEGER NOT NULL REFERENCES purchasing.suppliers,
  invoice_no      TEXT NOT NULL,
  invoice_date    DATE NOT NULL,
  subtotal        NUMERIC(12,2),
  tax_amount      NUMERIC(12,2),
  discount        NUMERIC(12,2) NOT NULL DEFAULT 0,
  total           NUMERIC(12,2),
  attachment_url  TEXT,                                 -- bill photo / PDF
  source          TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','photo','eql','airtable')),
  eql_ref         TEXT,                                 -- EQL Plus purchase id once entered there
  notes           TEXT,
  created_by      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (supplier_id, invoice_no)
);

CREATE TABLE purchasing.invoice_lines (
  id           BIGSERIAL PRIMARY KEY,
  invoice_id   BIGINT NOT NULL REFERENCES purchasing.invoices ON DELETE CASCADE,
  product_id   BIGINT NOT NULL REFERENCES core.products,
  qty          INTEGER NOT NULL CHECK (qty > 0),
  unit_cost    NUMERIC(12,2) NOT NULL CHECK (unit_cost >= 0),   -- without VAT
  vat_pct      NUMERIC(5,2) NOT NULL DEFAULT 5,
  line_total   NUMERIC(12,2) GENERATED ALWAYS AS (round(qty * unit_cost * (1 + vat_pct / 100), 2)) STORED
);
CREATE INDEX ON purchasing.invoice_lines (product_id);

ALTER TABLE core.cost_history
  ADD FOREIGN KEY (purchase_line_id) REFERENCES purchasing.invoice_lines ON DELETE SET NULL;

-- A purchase line records the new cost automatically
CREATE OR REPLACE FUNCTION purchasing.line_to_cost() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO core.cost_history (product_id, unit_cost, vat_included, source, purchase_line_id, effective_at, created_by)
  SELECT NEW.product_id, NEW.unit_cost, FALSE, 'purchase', NEW.id, i.invoice_date::timestamptz, i.created_by
    FROM purchasing.invoices i WHERE i.id = NEW.invoice_id;
  RETURN NEW;
END $$;
CREATE TRIGGER invoice_line_cost AFTER INSERT ON purchasing.invoice_lines
  FOR EACH ROW EXECUTE FUNCTION purchasing.line_to_cost();


-- =====================================================================
-- SALES — bundles, orders, order lines, charges, payments
-- =====================================================================

CREATE TABLE sales.bundles (
  id              BIGSERIAL PRIMARY KEY,
  sku             TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  description     TEXT,
  discount_type   TEXT NOT NULL CHECK (discount_type IN ('amount','percent')),
  discount_value  NUMERIC(12,2) NOT NULL CHECK (discount_value >= 0),
  active_from     DATE,
  active_until    DATE,                                  -- after this date stock -> 0
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft','active','ended')),
  shopify_id      TEXT,
  airtable_id     TEXT UNIQUE,
  stock_check     JSONB,                                 -- component stock when created
  created_by      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by      TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (active_until IS NULL OR active_from IS NULL OR active_until >= active_from)
);

CREATE TABLE sales.bundle_items (
  bundle_id   BIGINT NOT NULL REFERENCES sales.bundles ON DELETE CASCADE,
  product_id  BIGINT NOT NULL REFERENCES core.products,
  qty         SMALLINT NOT NULL DEFAULT 1 CHECK (qty > 0),
  position    SMALLINT NOT NULL DEFAULT 1,
  PRIMARY KEY (bundle_id, product_id)
);

-- Customers live in CRM; orders reference them
CREATE TABLE crm.customers (
  id                  BIGSERIAL PRIMARY KEY,
  name                TEXT,
  phone_e164          TEXT UNIQUE CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),  -- +971501234567 = WhatsApp id
  phone2_e164         TEXT,
  email               CITEXT,
  address_line        TEXT,
  city                TEXT,
  emirate_state       TEXT,
  country_code        CHAR(2),
  acquired_channel    TEXT REFERENCES core.channels,
  tags                TEXT[] NOT NULL DEFAULT '{}',      -- vip, wholesale, influencer ...
  loyalty_points      INTEGER NOT NULL DEFAULT 0,
  marketing_opt_in    BOOLEAN NOT NULL DEFAULT FALSE,    -- consent for WhatsApp campaigns
  blocked             BOOLEAN NOT NULL DEFAULT FALSE,    -- old "Blocked numbers"
  notes               TEXT,
  assigned_to         CITEXT REFERENCES ops.users,
  airtable_id         TEXT UNIQUE,
  first_order_at      TIMESTAMPTZ,
  last_order_at       TIMESTAMPTZ,
  orders_count        INTEGER NOT NULL DEFAULT 0,
  total_spent         NUMERIC(12,2) NOT NULL DEFAULT 0,
  created_by          TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON crm.customers (email);
CREATE INDEX customers_name_trgm ON crm.customers USING gin (name gin_trgm_ops);
CREATE TRIGGER customers_touch BEFORE UPDATE ON crm.customers
  FOR EACH ROW EXECUTE FUNCTION ops.touch_updated_at();

-- Same person on other platforms (Shopify customer id, Trendyol, Amazon, eBay ...)
CREATE TABLE crm.customer_identities (
  customer_id   BIGINT NOT NULL REFERENCES crm.customers ON DELETE CASCADE,
  channel_code  TEXT   NOT NULL REFERENCES core.channels,
  external_id   TEXT   NOT NULL,
  PRIMARY KEY (channel_code, external_id)
);
CREATE INDEX ON crm.customer_identities (customer_id);

CREATE TABLE sales.orders (
  id                  BIGSERIAL PRIMARY KEY,
  channel_code        TEXT NOT NULL REFERENCES core.channels,
  external_order_id   TEXT,                              -- Shopify order id, Trendyol package id ...
  order_number        TEXT,                              -- #1234 shown to customer
  customer_id         BIGINT REFERENCES crm.customers,
  order_date          TIMESTAMPTZ NOT NULL,
  currency            CHAR(3) NOT NULL DEFAULT 'AED',
  subtotal            NUMERIC(12,2) NOT NULL DEFAULT 0,
  discount            NUMERIC(12,2) NOT NULL DEFAULT 0,
  shipping_charged    NUMERIC(12,2) NOT NULL DEFAULT 0,
  tax                 NUMERIC(12,2) NOT NULL DEFAULT 0,
  total               NUMERIC(12,2) NOT NULL DEFAULT 0,
  payment_status      TEXT NOT NULL DEFAULT 'pending'
                      CHECK (payment_status IN ('pending','paid','partially_paid','refunded','partially_refunded','voided')),
  payment_mode        TEXT,                              -- card, cod, bank_transfer, tabby ...
  fulfillment_status  TEXT NOT NULL DEFAULT 'unfulfilled'
                      CHECK (fulfillment_status IN ('unfulfilled','partial','fulfilled','delivered','returned','cancelled')),
  ship_by             DATE,
  fulfilled_at        TIMESTAMPTZ,
  courier             TEXT,
  tracking_number     TEXT,
  eql_invoice_ref     TEXT,                              -- sales invoice created in EQL Plus
  airtable_id         TEXT UNIQUE,
  raw                 JSONB,                             -- original payload from Shopify / marketplace
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel_code, external_order_id)
);
CREATE INDEX ON sales.orders (customer_id, order_date DESC);
CREATE INDEX ON sales.orders (order_date DESC);
CREATE TRIGGER orders_touch BEFORE UPDATE ON sales.orders
  FOR EACH ROW EXECUTE FUNCTION ops.touch_updated_at();

CREATE TABLE sales.order_lines (
  id                BIGSERIAL PRIMARY KEY,
  order_id          BIGINT NOT NULL REFERENCES sales.orders ON DELETE CASCADE,
  product_id        BIGINT REFERENCES core.products,     -- NULL if not matched yet
  bundle_id         BIGINT REFERENCES sales.bundles,
  sku               TEXT,
  title             TEXT NOT NULL,
  qty               INTEGER NOT NULL CHECK (qty > 0),
  unit_price        NUMERIC(12,2) NOT NULL,              -- incl. VAT as sold
  discount          NUMERIC(12,2) NOT NULL DEFAULT 0,
  tax               NUMERIC(12,2) NOT NULL DEFAULT 0,
  line_total        NUMERIC(12,2) NOT NULL,
  unit_cost_at_sale NUMERIC(12,2)                        -- frozen from cost_history when the order arrives
);
CREATE INDEX ON sales.order_lines (order_id);
CREATE INDEX ON sales.order_lines (product_id);

-- Costs of fulfilling an order (courier, gateway, platform fee, COD, duty)
CREATE TABLE sales.order_charges (
  id        BIGSERIAL PRIMARY KEY,
  order_id  BIGINT NOT NULL REFERENCES sales.orders ON DELETE CASCADE,
  type      TEXT NOT NULL CHECK (type IN ('courier','payment_gateway','platform_fee','cod_fee','cash_withdrawal','duty','other')),
  amount    NUMERIC(12,2) NOT NULL,
  note      TEXT
);
CREATE INDEX ON sales.order_charges (order_id);

CREATE TABLE sales.payments (
  id          BIGSERIAL PRIMARY KEY,
  order_id    BIGINT REFERENCES sales.orders ON DELETE SET NULL,
  method      TEXT NOT NULL,
  amount      NUMERIC(12,2) NOT NULL,
  paid_at     TIMESTAMPTZ NOT NULL,
  reference   TEXT,
  created_by  TEXT
);


-- =====================================================================
-- CRM — leads, WhatsApp messages, campaigns
-- =====================================================================

CREATE TABLE crm.leads (
  id                   BIGSERIAL PRIMARY KEY,
  customer_id          BIGINT NOT NULL REFERENCES crm.customers ON DELETE CASCADE,
  source               TEXT NOT NULL DEFAULT 'whatsapp',   -- whatsapp, instagram, website, call, walk_in
  status               TEXT NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new','contacted','interested','quoted','won','lost','no_reply')),
  assigned_to          CITEXT REFERENCES ops.users,
  last_attended_by     CITEXT REFERENCES ops.users,
  created_by           TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_contact_at      TIMESTAMPTZ,
  next_follow_up_at    TIMESTAMPTZ,
  last_summary         TEXT,                              -- summary of last conversation
  all_time_summary     TEXT,
  lost_reason          TEXT,
  converted_order_id   BIGINT REFERENCES sales.orders,
  airtable_id          TEXT UNIQUE
);
CREATE INDEX ON crm.leads (status, assigned_to);
CREATE INDEX ON crm.leads (next_follow_up_at) WHERE status NOT IN ('won','lost');

CREATE TABLE crm.lead_products (
  lead_id        BIGINT NOT NULL REFERENCES crm.leads ON DELETE CASCADE,
  product_id     BIGINT NOT NULL REFERENCES core.products,
  offered_price  NUMERIC(12,2),
  PRIMARY KEY (lead_id, product_id)
);

-- Every WhatsApp message (from MSG91 webhook or typed log)
CREATE TABLE crm.messages (
  id                    BIGSERIAL PRIMARY KEY,
  customer_id           BIGINT NOT NULL REFERENCES crm.customers ON DELETE CASCADE,
  lead_id               BIGINT REFERENCES crm.leads ON DELETE SET NULL,
  channel               TEXT NOT NULL DEFAULT 'whatsapp' CHECK (channel IN ('whatsapp','instagram','sms','email','call','note')),
  direction             TEXT NOT NULL CHECK (direction IN ('in','out','internal')),
  body                  TEXT,
  media_url             TEXT,
  template_name         TEXT,
  provider_message_id   TEXT UNIQUE,                       -- MSG91 / Meta id (stops duplicates)
  delivery_status       TEXT CHECK (delivery_status IN ('queued','sent','delivered','read','failed')),
  sent_by               CITEXT REFERENCES ops.users,
  campaign_id           BIGINT,                            -- FK below
  sent_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON crm.messages (customer_id, sent_at DESC);

CREATE TABLE crm.campaigns (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  channel       TEXT NOT NULL DEFAULT 'whatsapp',
  template_name TEXT,
  audience      JSONB NOT NULL DEFAULT '{}',                -- saved filter (tags, brands bought, last order before ...)
  status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','scheduled','sending','sent','cancelled')),
  scheduled_at  TIMESTAMPTZ,
  created_by    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE crm.messages ADD FOREIGN KEY (campaign_id) REFERENCES crm.campaigns ON DELETE SET NULL;

CREATE TABLE crm.campaign_recipients (
  campaign_id  BIGINT NOT NULL REFERENCES crm.campaigns ON DELETE CASCADE,
  customer_id  BIGINT NOT NULL REFERENCES crm.customers ON DELETE CASCADE,
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','delivered','read','failed','replied','ordered')),
  order_id     BIGINT REFERENCES sales.orders,              -- order that came from the campaign
  PRIMARY KEY (campaign_id, customer_id)
);


-- =====================================================================
-- EXTRACTOR — perfume photo extractor (shelf photos -> items)
-- =====================================================================

-- Physical shops photographed are rows in competitor.shops with kind = 'physical'
-- ("Shop 3" and "Shop3" become one row).
CREATE TABLE extractor.batches (
  id            BIGSERIAL PRIMARY KEY,
  shop_code     TEXT REFERENCES competitor.shops,
  shop_name_raw TEXT,
  photo_url     TEXT NOT NULL,                              -- Cloudinary
  ai_model      TEXT,
  status        TEXT NOT NULL DEFAULT 'extracted' CHECK (status IN ('uploaded','extracted','reviewed','failed')),
  uploaded_by   CITEXT REFERENCES ops.users,
  uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE extractor.items (
  id                  BIGSERIAL PRIMARY KEY,
  batch_id            BIGINT NOT NULL REFERENCES extractor.batches ON DELETE CASCADE,
  raw                 JSONB NOT NULL,                       -- what the AI read, before corrections
  brand_text          TEXT,
  brand_id            INTEGER REFERENCES core.brands,
  name                TEXT,
  size_label          TEXT,
  size_ml             NUMERIC(7,2),
  concentration       TEXT,
  gender              TEXT CHECK (gender IN ('Men','Women','Unisex')),
  condition           TEXT CHECK (condition IN ('Sealed','Tester','Used','Unknown')),
  shelf_price         NUMERIC(12,2),                        -- if a price tag is visible
  matched_product_id  BIGINT REFERENCES core.products,      -- already in our catalog
  match_status        TEXT NOT NULL DEFAULT 'unchecked'
                      CHECK (match_status IN ('unchecked','in_catalog','new','ignored')),
  saved               BOOLEAN NOT NULL DEFAULT FALSE,
  saved_by            CITEXT REFERENCES ops.users,
  saved_at            TIMESTAMPTZ,
  created_product_id  BIGINT REFERENCES core.products       -- product created via Generate SKU
);
CREATE INDEX ON extractor.items (batch_id);
CREATE INDEX ON extractor.items (match_status) WHERE saved;

-- Every correction (feeds back into the extraction prompt)
CREATE TABLE extractor.corrections (
  id               BIGSERIAL PRIMARY KEY,
  item_id          BIGINT NOT NULL REFERENCES extractor.items ON DELETE CASCADE,
  field            TEXT NOT NULL,
  original_value   TEXT,
  corrected_value  TEXT,
  corrected_by     CITEXT REFERENCES ops.users,
  corrected_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Late foreign keys to products
ALTER TABLE ops.job_runs ADD FOREIGN KEY (product_id) REFERENCES core.products ON DELETE SET NULL;
ALTER TABLE ops.alerts   ADD FOREIGN KEY (product_id) REFERENCES core.products ON DELETE SET NULL;


-- =====================================================================
-- VIEWS — calculated values (always up to date, nothing goes stale)
-- =====================================================================

-- Current cost per product (latest date wins; same day = last entered wins)
CREATE VIEW core.v_current_cost AS
SELECT DISTINCT ON (product_id)
       product_id, unit_cost, vat_included, source, effective_at
  FROM core.cost_history
 ORDER BY product_id, (effective_at AT TIME ZONE 'Asia/Dubai')::date DESC, id DESC;

-- Competitor prices that are allowed to count: matched, shop used for pricing,
-- in stock, read successfully, and fresh (within the shop's max_age_days)
CREATE VIEW competitor.v_valid_prices AS
SELECT m.product_id, m.shop_code, s.name AS shop_name, l.id AS listing_id,
       l.price_inc_vat, l.last_success_at, m.confirmed, m.method
  FROM competitor.matches m
  JOIN competitor.listings l ON l.id = m.listing_id
  JOIN competitor.shops    s ON s.code = m.shop_code
 WHERE s.used_for_pricing AND s.active
   AND l.stock_status = 'in_stock'
   AND l.check_status = 'ok'
   AND l.price_inc_vat > 0
   AND l.last_success_at >= now() - make_interval(days => s.max_age_days)
   AND (s.has_gtin OR m.confirmed);          -- name-only shops (Ounass) count only when confirmed

-- Suggested price for the UAE website:
--   cheapest valid competitor x (1 - discount), floor = LOP (manual or cost x (1 + margin)), ceiling = MOP
CREATE VIEW pricing.v_suggested_price AS
WITH s AS (
  SELECT COALESCE((SELECT (value #>> '{}')::numeric FROM ops.settings WHERE key = 'discount_pct'), 0)   AS discount_pct,
         COALESCE((SELECT (value #>> '{}')::numeric FROM ops.settings WHERE key = 'min_margin_pct'), 0) AS margin_pct
),
cheapest AS (
  SELECT DISTINCT ON (product_id) product_id, listing_id, shop_code, shop_name, price_inc_vat
    FROM competitor.v_valid_prices
   ORDER BY product_id, price_inc_vat, shop_code
)
SELECT p.id AS product_id, p.sku, p.product_name,
       l.price                                         AS our_price,
       c.price_inc_vat                                 AS cheapest_price,
       c.shop_name                                     AS cheapest_shop,
       c.listing_id                                    AS cheapest_listing_id,
       COALESCE(b.discount_pct, s.discount_pct)        AS discount_pct,
       cc.unit_cost,
       CASE WHEN r.lop_source = 'cost' AND cc.unit_cost IS NOT NULL
            THEN round(cc.unit_cost * (CASE WHEN cc.vat_included THEN 1 ELSE 1.05 END)
                       * (1 + COALESCE(b.min_margin_pct, s.margin_pct) / 100), 2)
            ELSE r.lop END                             AS lop_effective,
       r.mop,
       COALESCE(r.no_auto, FALSE)                      AS no_auto,
       b.auto_price,
       CASE WHEN c.price_inc_vat IS NULL THEN NULL
            ELSE LEAST(
                   COALESCE(r.mop, 'Infinity'::numeric),
                   GREATEST(
                     COALESCE(CASE WHEN r.lop_source = 'cost' AND cc.unit_cost IS NOT NULL
                                   THEN round(cc.unit_cost * (CASE WHEN cc.vat_included THEN 1 ELSE 1.05 END)
                                              * (1 + COALESCE(b.min_margin_pct, s.margin_pct) / 100), 2)
                                   ELSE r.lop END, 0),
                     round(c.price_inc_vat * (1 - COALESCE(b.discount_pct, s.discount_pct) / 100), 0)))   -- [C] whole AED
       END                                             AS suggested_price
  FROM core.products p
  JOIN core.brands b               ON b.id = p.brand_id
  CROSS JOIN s
  LEFT JOIN core.product_listings l ON l.product_id = p.id AND l.channel_code = 'web_uae'
  LEFT JOIN cheapest c             ON c.product_id = p.id
  LEFT JOIN core.v_current_cost cc ON cc.product_id = p.id
  LEFT JOIN pricing.product_rules r ON r.product_id = p.id
 WHERE p.status = 'active';

-- Profit per order line
CREATE VIEW sales.v_order_line_profit AS
SELECT ol.id AS order_line_id, o.id AS order_id, o.order_date, o.channel_code,
       ol.product_id, ol.sku, ol.qty,
       round(ol.line_total / 1.05, 2)                           AS net_revenue,   -- without VAT
       ol.qty * ol.unit_cost_at_sale                            AS cost,
       round(ol.line_total / 1.05, 2) - ol.qty * ol.unit_cost_at_sale AS gross_profit
  FROM sales.order_lines ol
  JOIN sales.orders o ON o.id = ol.order_id
 WHERE o.payment_status NOT IN ('refunded','voided');

-- Units sold per product in the last 30 days (for the product card)
CREATE VIEW sales.v_units_sold_30d AS
SELECT ol.product_id, sum(ol.qty) AS units_30d
  FROM sales.order_lines ol
  JOIN sales.orders o ON o.id = ol.order_id
 WHERE o.order_date >= now() - interval '30 days'
   AND o.fulfillment_status NOT IN ('cancelled','returned')
   AND ol.product_id IS NOT NULL
 GROUP BY ol.product_id;


-- =====================================================================
-- STARTER DATA
-- =====================================================================

INSERT INTO ops.roles (code, name, description) VALUES
  ('main_admin','Main admin','Everything, cannot be removed'),
  ('admin','Admin','Everything + user management'),
  ('user','User','Everything except user management'),
  ('viewer','Viewer','Read only');

INSERT INTO ops.permissions (code, description) VALUES
  ('competitor.view','See competitor table and shop lists'),
  ('competitor.match','Edit / confirm / reject competitor links'),
  ('pricing.update','Change prices, LOP / MOP, Skip automatic price'),
  ('settings.pricing','Change discount rate, margin %, automatic brands'),
  ('items.edit','Create / edit / delete Item Master products'),
  ('bundles.edit','Create / edit bundles'),
  ('crm.view','See customers, orders and WhatsApp log'),
  ('crm.edit','Edit customers and leads, log messages'),
  ('crm.campaigns','Create and send WhatsApp campaigns'),
  ('extractor.use','Upload shelf photos and save items'),
  ('purchasing.edit','Enter purchase invoices and costs'),
  ('users.manage','Add / change / disable users');

INSERT INTO ops.role_permissions SELECT 'main_admin', code FROM ops.permissions;
INSERT INTO ops.role_permissions SELECT 'admin', code FROM ops.permissions;
INSERT INTO ops.role_permissions SELECT 'user', code FROM ops.permissions WHERE code <> 'users.manage';
INSERT INTO ops.role_permissions VALUES ('viewer','competitor.view'), ('viewer','crm.view');

INSERT INTO ops.settings (key, value, description) VALUES
  ('discount_pct', '2',   'General % below the cheapest in-stock competitor'),
  ('min_margin_pct', '10', 'General margin over cost for the cost-based price floor (LOP)'),
  ('run1_brands_per_day', '10', 'Brands checked per day by Run 1');

INSERT INTO core.channels (code, name, kind, country, currency) VALUES
  ('web_uae','fragrantsouq.com UAE','website','AE','AED'),
  ('web_asia','fragrantsouq.com Asia','website',NULL,'AED'),
  ('web_kuwait','fragrantsouq.com Kuwait','website','KW','KWD'),
  ('web_america','fragrantsouq.com America','website','US','USD'),
  ('web_qatar','fragrantsouq.com Qatar','website','QA','QAR'),
  ('trendyol','Trendyol','marketplace','AE','AED'),
  ('amazon_ae','Amazon.ae','marketplace','AE','AED'),
  ('noon','Noon','marketplace','AE','AED'),
  ('ebay','eBay','marketplace',NULL,'USD'),
  ('tiktok_us','TikTok Shop US','social','US','USD'),
  ('whatsapp','WhatsApp','social','AE','AED'),
  ('store','Walk-in / offline','offline','AE','AED');

INSERT INTO core.locations (code, name) VALUES
  ('warehouse_dxb','Dubai warehouse'), ('shopify','Shopify'), ('amazon_fba','Amazon FBA'), ('trendyol','Trendyol');

INSERT INTO competitor.shops (code, name, kind, platform, refresh_method, refresh_days, max_age_days, follows_shop, has_gtin, used_for_pricing, sort_order) VALUES
  ('ff','French Fragrance','competitor','custom','pc_chrome',75,90,NULL,TRUE,TRUE,1),
  ('samawa','Samawa','competitor','shopify','render',1,7,NULL,TRUE,TRUE,2),
  ('vp','V Perfumes','competitor','custom','render',1,7,NULL,TRUE,TRUE,4),
  ('aj','Al Injaz','competitor','custom','pc_chrome',5,10,NULL,TRUE,TRUE,5),
  ('on','Ounass','competitor','custom','pc_chrome',5,10,NULL,FALSE,TRUE,6),
  ('am','Amberiya','competitor','shopify','render',1,7,NULL,TRUE,TRUE,7),
  ('ng','Niche Gallerie','competitor','custom','pc_chrome',5,10,NULL,TRUE,TRUE,8),
  ('pd','Perfume Dubai','competitor','shopify','render',5,10,NULL,TRUE,FALSE,9),
  ('mh','MHG Boutique','competitor','shopify','render',5,10,NULL,TRUE,TRUE,10),
  ('br','Brands Perfume','competitor','shopify','render',5,10,NULL,TRUE,FALSE,11),
  ('ep','Emirates Pride','brand_store','shopify','pc_chrome',15,20,NULL,TRUE,TRUE,20),
  ('an','Anfasic','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,21),
  ('kh','Khaltat','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,22),
  ('oe','Oud Elite','brand_store','shopify','pc_chrome',15,20,NULL,TRUE,TRUE,23),
  ('aq','Abdul Samad Al Qurashi','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,24),
  ('ao','Arabian Oud','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,25),
  ('jz','Al Jazeera Perfumes','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,26),
  ('jm','Ajmal','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,27),
  ('ah','Al Haramain','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,28),
  ('rf','Reef Perfumes','brand_store','shopify','render',15,20,NULL,TRUE,TRUE,29);
INSERT INTO competitor.shops (code, name, kind, platform, refresh_method, refresh_days, max_age_days, follows_shop, has_gtin, used_for_pricing, sort_order) VALUES
  ('bp','Branded Perfume','competitor','custom','follows',75,90,'ff',TRUE,TRUE,3),
  ('es','Essenzi','competitor','custom','follows',75,90,'ff',TRUE,TRUE,3);
