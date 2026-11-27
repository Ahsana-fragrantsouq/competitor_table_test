"""
New database (v2) - steps 1 and 2 of the migration plan
=======================================================
(2026-11-26, user: "i want to redesign my database as explained in files" -
 design: "FragrantSouq Platform Database v2 - Design", 25 Nov 2026)

The new tables are built NEXT TO the old ones, in the same database, in 8 new
folders (schemas): ops, core, competitor, pricing, sales, crm, purchasing, extractor.
Nothing old is changed or deleted. The website keeps using the old tables until
later steps move each page over.

  Step 1  create the new tables      -> the files in db_v2/ (001_schema.sql, later 002_...)
                                        each file runs ONCE (remembered in table newdb_migrations)
  Step 2  copy the reference data    -> old table                       new table
                                        brand_sku + brand_automation
                                          + brand_discount               core.brands
                                        item_options (+ values in use)   core.option_values
                                        item_master                      core.products
                                          5 market prices                core.product_listings
                                          Shopify qty                    core.stock_levels
                                        competitor_table LOP/MOP/Skip    pricing.product_rules
                                        app_users                        ops.users
                                        app_settings                     ops.settings + ops.run_state
                                     Copying again later is safe: rows are updated, never doubled.

Links (open in the browser, ?secret=RUN_SECRET):
  /newdb/test?secret=...    does steps 1 + 2 completely, shows what WOULD be saved, then UNDOES
                            everything (nothing is saved). Run this first.
  /newdb/run?secret=...     does steps 1 + 2 and SAVES them.
  /newdb/status?secret=...  result of the last test / run + how many rows each new table has.
  /newdb-cost/test?secret=...  READS "Cost" of every product from Airtable French Inventories, shows what would be
                               saved in core.cost_history, then undoes it. (Airtable is only read, never changed.)
  /newdb-cost/run?secret=...   same, SAVED. Needs /newdb/run done first. Safe to repeat: only changed costs are added.
  /newdb-shops/test?secret=...&shops=am,mh   step 3: copies those shops' lists (default: all shops) into
                               competitor.listings, shows the result, then undoes it.
  /newdb-shops/run?secret=...&shops=am,mh    same, SAVED. Safe to repeat (rows updated, never doubled).
  /newdb-matches/test?secret=...&shops=am,vp  step 4: links of the old Competitor table -> competitor.matches
                               (exact page + size), suggestions + rejected links -> competitor.match_suggestions.
                               Undone at the end. Default: all shops.
  /newdb-matches/run?secret=...&shops=am,vp   same, SAVED. Safe to repeat; after the first run the automatic
                               check (below) keeps the links in step too.
  /newdb-sync/run?secret=...   copies the shop-list rows changed since the last check, now. The website also does
                               this by itself every 15 minutes (SECTION 3d; Render env NEWDB_SYNC=off stops it).

SAFETY: refuses to run on the production database (french_fragrance_db). This is the test copy.

Times: the old tables store times without a time zone. They were written by Render (UTC),
so they are read as UTC.
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, request

newdb_bp = Blueprint("newdb", __name__)

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(HERE, "db_v2")
PRODUCTION_DB = "french_fragrance_db"
MAIN_ADMIN = "ashref"                    # same as auth.MAIN_ADMIN -> role main_admin
JUNK = "085715166586"                    # junk record: not copied (design, migration plan step 2)

# old Item Master price columns -> new channel
MARKETS = [("uae", "web_uae"), ("asia", "web_asia"), ("kuwait", "web_kuwait"),
           ("america", "web_america"), ("qatar", "web_qatar")]
# old app_settings key -> new place.  ("settings", new key) or ("run_state", run code)
SETTING_MAP = {
    "discount_rate": ("settings", "discount_pct"),
    "daily_brands_per_day": ("settings", "run1_brands_per_day"),
    "daily_update": ("run_state", "run1_daily_brands"),
    "full_lists": ("run_state", "run2_full_lists"),
}

lock = threading.Lock()
status = {"state": "idle"}               # shown by /newdb/status


def log(*a):
    """Print to the Render log (lines start with [NEWDB])."""
    print(*a, flush=True)


def db_name():
    return (os.environ.get("FF_DATABASE_URL") or "").partition("?")[0].rpartition("/")[2]


def secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


# ============================================================ SECTION 1: step 1 - new tables
def migration_files():
    """[(name, path)] of db_v2/NNN_*.sql in number order."""
    names = sorted(f for f in os.listdir(MIGRATIONS_DIR) if re.match(r"^\d{3}_.*\.sql$", f))
    return [(n, os.path.join(MIGRATIONS_DIR, n)) for n in names]


def apply_migrations(cur, report):
    """Run every db_v2 file that has not run yet on this database."""
    cur.execute("""CREATE TABLE IF NOT EXISTS public.newdb_migrations (
                       name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    cur.execute("SELECT name FROM public.newdb_migrations")
    done = {r[0] for r in cur.fetchall()}
    applied = []
    for name, path in migration_files():
        if name in done:
            continue
        log(f"[NEWDB] Applying {name}")
        with open(path, encoding="utf-8") as f:
            cur.execute(f.read())
        cur.execute("INSERT INTO public.newdb_migrations (name) VALUES (%s)", (name,))
        applied.append(name)
    report["step1_new_files"] = applied or "none (all files already applied)"
    report["step1_all_files"] = sorted(done | set(applied))


# ============================================================ SECTION 2: step 2 - helpers
def exists(cur, table):
    cur.execute("SELECT to_regclass(%s)", (table,))
    return cur.fetchone()[0] is not None


def columns(cur, table):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = %s""", (table,))
    return {r[0] for r in cur.fetchall()}


def rows(cur, table, wanted):
    """SELECT the wanted columns that exist (missing ones come back as None). [] when the table is missing."""
    if not exists(cur, "public." + table):
        return []
    have = columns(cur, table)
    sel = ", ".join(c if c in have else f"NULL AS {c}" for c in wanted)
    cur.execute(f"SELECT {sel} FROM public.{table}")
    return cur.fetchall()


def text(v):
    v = str(v).strip() if v is not None else ""
    return v or None


def key(v):
    return (text(v) or "").lower()


def money(v):
    try:
        return None if v is None or float(v) < 0 else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def size_ml(label):
    """'100 ml' / '100ML' / '7.5 ml' -> number. '3 x 30 ml' or no ml -> None."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*ml\.?\s*", label or "", re.I)
    return float(m.group(1)) if m and float(m.group(1)) > 0 else None


def as_json(v):
    """Old app_settings value (text) -> JSON value. '3' -> 3, '{"a":1}' -> object, 'abc' -> "abc"."""
    if v is None:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


def example(lst, n=5):
    return lst[:n] + ([f"... {len(lst) - n} more"] if len(lst) > n else [])


# ============================================================ SECTION 3: step 2 - copy
def copy_brands(cur, report, im):
    sku_rows = rows(cur, "brand_sku", ["brand", "sku_prefix", "next_sku_number", "airtable_id", "created_by"])
    auto_rows = rows(cur, "brand_automation", ["brand", "auto"])
    auto = {key(b): bool(a) for b, a in auto_rows}
    disc = {key(k or b): (text(b), r) for k, b, r in rows(cur, "brand_discount", ["brand_key", "brand", "rate"])}

    brands = {}                                     # lower(name) -> dict
    def add(name):
        k = key(name)
        if k and k not in brands:
            brands[k] = {"name": text(name), "prefix": None, "next": 1001, "airtable_id": None, "created_by": None}
        return brands.get(k)

    bad_prefix, dup_prefix, used_prefix, used_at = [], [], set(), set()
    # 2026-11-26 (test run report): prefix -> brand, used when a product's brand is empty (ADP1022 -> Acqua Di Parma).
    # Only prefixes that belong to ONE brand (LXR is shared by 5 brands -> not used).
    owners = {}
    for name, prefix, _, _, _ in sku_rows:
        p = (text(prefix) or "").upper()
        if p and key(name) and key(name) != JUNK:
            owners.setdefault(p, set()).add(key(name))
    for name, prefix, nxt, at_id, by in sku_rows:
        if key(name) == JUNK:                       # junk record "085715166586" is not a brand
            continue
        b = add(name)
        if not b:
            continue
        p = (text(prefix) or "").upper()
        if p and not re.fullmatch(r"[A-Z]{3,4}", p):
            bad_prefix.append(f"{b['name']}: {prefix!r}")
        elif p and p in used_prefix:
            dup_prefix.append(f"{b['name']}: {p}")
        elif p:
            b["prefix"] = p
            used_prefix.add(p)
        try:
            b["next"] = max(1001, int(nxt)) if nxt is not None else 1001
        except (TypeError, ValueError):
            pass
        at_id = text(at_id)
        if at_id and at_id not in used_at:
            b["airtable_id"] = at_id
            used_at.add(at_id)
        b["created_by"] = text(by)
    for k, (name, _) in disc.items():
        add(name or k)
    for name, _ in auto_rows:
        add(name)
    for r in im:
        if key(r["brand"]) != JUNK:
            add(r["brand"])

    data =[(b["name"], b["prefix"], b["next"], auto.get(k, False),
             money(disc[k][1]) if k in disc else None, b["airtable_id"], b["created_by"] or "import")
            for k, b in brands.items()]
    psycopg2.extras.execute_values(cur, """
        INSERT INTO core.brands (name, sku_prefix, next_sku_number, auto_price, discount_pct, airtable_id, created_by)
        VALUES %s
        ON CONFLICT (name) DO UPDATE SET
          sku_prefix      = EXCLUDED.sku_prefix,
          next_sku_number = GREATEST(core.brands.next_sku_number, EXCLUDED.next_sku_number),
          auto_price      = EXCLUDED.auto_price,
          discount_pct    = EXCLUDED.discount_pct,
          airtable_id     = EXCLUDED.airtable_id,
          updated_by      = 'import'""", data)
    cur.execute("SELECT lower(name::text), id FROM core.brands")
    report["brands"] = {"copied": len(data), "with_sku_prefix": sum(1 for d in data if d[1]),
                        "without_sku_prefix": [d[0] for d in data if not d[1]][:30],
                        "bad_prefix_left_empty": bad_prefix, "duplicate_prefix_left_empty": dup_prefix,
                        "automatic": sum(1 for d in data if d[3]), "own_discount": sum(1 for d in data if d[4] is not None)}
    brand_ids = dict(cur.fetchall())
    by_prefix = {p: brand_ids[next(iter(ks))] for p, ks in owners.items() if len(ks) == 1 and next(iter(ks)) in brand_ids}
    return brand_ids, by_prefix


def copy_options(cur, report, im):
    data, seen = [], set()
    for field, value, by, at in rows(cur, "item_options", ["field", "value", "added_by", "added_at"]):
        k = (key(field), key(value))
        if all(k) and k not in seen:
            seen.add(k)
            data.append((text(field), text(value), by, at or datetime.now(timezone.utc)))
    added = 0
    for field in ("product_type", "type", "category", "size"):         # values already used in Item Master
        for r in im:
            k = (field, key(r[field]))
            if k[1] and k not in seen:
                seen.add(k)
                data.append((field, text(r[field]), "import", datetime.now(timezone.utc)))
                added += 1
    if data:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO core.option_values (field, value, added_by, added_at) VALUES %s
            ON CONFLICT DO NOTHING""", data)
    report["option_values"] = {"copied": len(data), "of_them_from_item_master_values": added}


IM_COLS = ["fi_record_id", "sku", "brand", "product_type", "perfume_name", "product_name", "type", "category",
           "size", "barcode", "top_notes", "middle_notes", "base_notes", "shopify_description", "desc_created_at",
           "gift_items", "source", "edited_fields", "shopify_id", "stock_qty"] \
    + [f"{m}_price" for m, _ in MARKETS] + [f"{m}_comparison_price" for m, _ in MARKETS]


def read_item_master(cur):
    deleted = {r[0] for r in rows(cur, "item_master_deleted", ["fi_record_id"])}
    out, skipped = [], {"deleted_with_item_master_delete": 0, "junk_record": 0}
    for r in rows(cur, "item_master", IM_COLS):
        r = dict(zip(IM_COLS, r))
        if r["fi_record_id"] in deleted:
            skipped["deleted_with_item_master_delete"] += 1
        elif JUNK in (text(r["sku"]), text(r["barcode"]), text(r["fi_record_id"])):
            skipped["junk_record"] += 1
        else:
            out.append(r)
    return out, skipped


def completeness(r):
    """Higher = better record. Used when one SKU is in Item Master twice (2026-11-26: PRD1043 = a full record +
    a leftover with no brand and name 'Perfume') -> the fuller one is copied."""
    return (bool(text(r["brand"])), bool(text(r["barcode"])), r["uae_price"] is not None,
            bool(text(r["shopify_id"])), len(text(r["product_name"]) or ""), bool(text(r["perfume_name"])))


def is_leftover(r):
    """An empty leftover record: no perfume name, and the name is only brand + 'Perfume' ('Xerjoff     Perfume')."""
    if text(r["perfume_name"]):
        return False
    rest = " ".join((text(r["product_name"]) or "").lower().split())
    brand = key(r["brand"])
    if brand and rest.startswith(brand):
        rest = rest[len(brand):].strip()
    return rest in ("", "perfume")


def copy_products(cur, report, im, skipped, brand_ids, by_prefix):
    data, no_sku, no_brand, dup_sku, bad_barcode, seen = [], [], [], [], [], set()
    temp_sku, all_skus = [], {text(r["sku"]) for r in im if text(r["sku"])}
    brand_from_prefix = 0
    for r in sorted(im, key=completeness, reverse=True):      # best record of each SKU first
        fid, sku = r["fi_record_id"], text(r["sku"])
        if not sku:
            no_sku.append(f"{fid}: {text(r['product_name']) or '(no name)'}")
            continue
        if sku in seen:
            # 2026-11-26 (2nd test report): most "duplicates" are DIFFERENT products with the same SKU (VRS1001S = 4
            # Versace gift sets). Leftovers with no perfume name ("Perfume", "Xerjoff Perfume") are dropped; real
            # products are kept with a temporary SKU "VRS1001S-2" so nothing is lost -> fix the SKU in Item Master.
            if is_leftover(r):
                dup_sku.append(f"{sku}: {fid} '{text(r['product_name']) or ''}' not copied (empty leftover)")
                continue
            n = 2
            while f"{sku}-{n}" in seen or f"{sku}-{n}" in all_skus:
                n += 1
            temp_sku.append(f"{sku} -> {sku}-{n}: {fid} '{text(r['product_name']) or ''}'")
            sku = f"{sku}-{n}"
        bid = brand_ids.get(key(r["brand"]))
        if not bid and not text(r["brand"]):                   # empty brand -> brand of the SKU prefix
            m = re.match(r"([A-Za-z]{3,4})[0-9]", sku)
            bid = by_prefix.get(m.group(1).upper()) if m else None
            brand_from_prefix += 1 if bid else 0
        if not bid:
            no_brand.append(f"{sku}: brand '{text(r['brand']) or ''}' {text(r['product_name']) or ''}")
            continue
        seen.add(sku)
        # '810876037013.' -> stray dot / spaces removed. '3.34613E+12' (spoilt by Excel) stays not valid -> empty
        barcode = re.sub(r"\s", "", text(r["barcode"]) or "").rstrip(".`'") or None   # '6437842514046`' too
        if barcode and not re.fullmatch(r"[0-9]{8,14}", barcode):
            bad_barcode.append(f"{sku}: {barcode}")
            barcode = None
        cat = (text(r["category"]) or "").capitalize()
        kinds = " ".join(filter(None, [text(r["product_type"]), text(r["type"]), text(r["product_name"])])).lower()
        size = text(r["size"])
        parts = [text(r["brand"]), text(r["perfume_name"]), size, text(r["type"]), text(r["category"]), text(r["product_type"])]
        data.append((
            sku, bid, text(r["perfume_name"]) or text(r["product_name"]) or sku,
            text(r["product_name"]) or " ".join(p for p in parts if p) or sku,
            text(r["product_type"]), text(r["type"]), cat if cat in ("Men", "Women", "Unisex") else None,
            size_ml(size), size, barcode, "tester" in kinds, "gift" in kinds or bool(text(r["gift_items"])),
            text(r["gift_items"]), text(r["top_notes"]), text(r["middle_notes"]), text(r["base_notes"]),
            text(r["shopify_description"]), r["desc_created_at"],
            fid if (fid or "").startswith("rec") else None, fid,
            "manual" if r["source"] == "website" else "import",
            psycopg2.extras.Json(r["edited_fields"] if isinstance(r["edited_fields"], dict) else {})))
    if data:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO core.products (sku, brand_id, perfume_name, product_name, product_type, concentration, gender,
                size_ml, size_label, barcode, is_tester, is_gift_set, gift_items, top_notes, middle_notes, base_notes,
                description_html, description_at, airtable_id, legacy_id, source, edited_fields, created_by)
            VALUES %s
            ON CONFLICT (legacy_id) DO UPDATE SET
              sku = EXCLUDED.sku, brand_id = EXCLUDED.brand_id, perfume_name = EXCLUDED.perfume_name,
              product_name = EXCLUDED.product_name, product_type = EXCLUDED.product_type,
              concentration = EXCLUDED.concentration, gender = EXCLUDED.gender, size_ml = EXCLUDED.size_ml,
              size_label = EXCLUDED.size_label, barcode = EXCLUDED.barcode, is_tester = EXCLUDED.is_tester,
              is_gift_set = EXCLUDED.is_gift_set, gift_items = EXCLUDED.gift_items, top_notes = EXCLUDED.top_notes,
              middle_notes = EXCLUDED.middle_notes, base_notes = EXCLUDED.base_notes,
              description_html = EXCLUDED.description_html, description_at = EXCLUDED.description_at,
              airtable_id = EXCLUDED.airtable_id, edited_fields = EXCLUDED.edited_fields, updated_by = 'import'""",
            data, template="(" + ", ".join(["%s"] * 22) + ", 'import')")
    report["products"] = {"in_item_master": len(im) + sum(skipped.values()), "copied": len(data),
                          "not_copied": dict(skipped, no_sku=len(no_sku), duplicate_sku=len(dup_sku),
                                             brand_not_found=len(no_brand)),
                          "brand_taken_from_sku_prefix": brand_from_prefix,
                          "same_sku_different_product_kept_with_temporary_sku": len(temp_sku),
                          "barcode_not_8_14_digits_left_empty": len(bad_barcode)}
    # full lists (2026-11-26, so every product left out can be fixed in Item Master)
    report["not_copied_details"] = {"no_sku": no_sku, "duplicate_sku": dup_sku, "brand_not_found": no_brand,
                                    "bad_barcode_left_empty": bad_barcode}
    report["temporary_sku_fix_in_item_master"] = temp_sku
    report["products"].update({
                          "with_size_in_ml": sum(1 for d in data if d[7]),
                          "with_gender": sum(1 for d in data if d[6])})
    cur.execute("SELECT legacy_id, id FROM core.products WHERE legacy_id IS NOT NULL")
    return dict(cur.fetchall())


def copy_listings_and_stock(cur, report, im, product_ids):
    listings, stock = [], []
    for r in im:
        pid = product_ids.get(r["fi_record_id"])
        if not pid:
            continue
        for m, ch in MARKETS:
            price, compare = money(r[f"{m}_price"]), money(r[f"{m}_comparison_price"])
            if price is None and compare is None:
                continue
            listings.append((pid, ch, price, compare, text(r["shopify_id"]),
                             "active" if text(r["shopify_id"]) and price else "not_listed"))
        if r["stock_qty"] is not None:
            stock.append((pid, "shopify", int(round(float(r["stock_qty"])))))
    if listings:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO core.product_listings (product_id, channel_code, price, compare_at_price, external_id, status,
                                               price_source, updated_by)
            VALUES %s
            ON CONFLICT (product_id, channel_code) DO UPDATE SET
              price = EXCLUDED.price, compare_at_price = EXCLUDED.compare_at_price, external_id = EXCLUDED.external_id,
              status = EXCLUDED.status, price_source = 'import', updated_by = 'import', updated_at = now()""",
            listings, template="(%s, %s, %s, %s, %s, %s, 'import', 'import')")
    if stock:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO core.stock_levels (product_id, location_code, qty) VALUES %s
            ON CONFLICT (product_id, location_code) DO UPDATE SET qty = EXCLUDED.qty, updated_at = now()""", stock)
    per_channel = {}
    for l in listings:
        per_channel[l[1]] = per_channel.get(l[1], 0) + 1
    report["product_listings"] = {"copied": len(listings), "per_channel": per_channel}
    report["stock_levels"] = {"copied": len(stock), "location": "shopify"}


def copy_rules(cur, report, product_ids):
    cols = ["fi_record_id", "lop", "mop", "limits_by", "limits_at", "no_auto", "no_auto_by", "no_auto_at"]
    data, bad, no_product = [], [], 0
    for r in rows(cur, "competitor_table", cols):
        fid, lop, mop, lby, lat, no_auto, nby, nat = r
        if lop is None and mop is None and not no_auto:
            continue
        pid = product_ids.get(fid)
        if not pid:
            no_product += 1
            continue
        lop, mop = money(lop), money(mop)
        if lop is not None and mop is not None and lop > mop:
            bad.append(f"{fid}: LOP {lop} > MOP {mop}")
            continue
        data.append((pid, lop, "manual" if lop is not None else "none", mop, bool(no_auto), lby, lat, nby, nat))
    if data:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO pricing.product_rules (product_id, lop, lop_source, mop, no_auto, limits_by, limits_at,
                                               no_auto_by, no_auto_at)
            VALUES %s
            ON CONFLICT (product_id) DO UPDATE SET
              lop = EXCLUDED.lop, lop_source = EXCLUDED.lop_source, mop = EXCLUDED.mop, no_auto = EXCLUDED.no_auto,
              limits_by = EXCLUDED.limits_by, limits_at = EXCLUDED.limits_at,
              no_auto_by = EXCLUDED.no_auto_by, no_auto_at = EXCLUDED.no_auto_at""", data)
    report["product_rules"] = {"copied": len(data), "skip_automatic_price": sum(1 for d in data if d[4]),
                               "not_copied_lop_above_mop": bad, "not_copied_product_not_in_item_master": no_product}


def copy_users(cur, report):
    data = []
    for u, pw, role, active, by, at, last, fails, locked in rows(
            cur, "app_users", ["username", "password_hash", "role", "active", "created_by", "created_at",
                               "last_login", "failed_logins", "locked_until"]):
        role = "main_admin" if key(u) == MAIN_ADMIN else ("admin" if role == "admin" else "user")
        data.append((key(u), pw, role, active is not False, by, at or datetime.now(timezone.utc), last,
                     max(0, fails or 0), locked))
    if data:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO ops.users (username, password_hash, role_code, active, created_by, created_at, last_login,
                                   failed_logins, locked_until)
            VALUES %s
            ON CONFLICT (username) DO UPDATE SET
              password_hash = EXCLUDED.password_hash, role_code = EXCLUDED.role_code, active = EXCLUDED.active,
              last_login = EXCLUDED.last_login, failed_logins = EXCLUDED.failed_logins,
              locked_until = EXCLUDED.locked_until""", data)
    report["users"] = {"copied": len(data), "roles": sorted({d[2] for d in data})}


def copy_settings(cur, report):
    settings, runs = [], []
    for k, v, by, at in rows(cur, "app_settings", ["key", "value", "updated_by", "updated_at"]):
        if v is None:
            continue
        where, new_key = SETTING_MAP.get(k, ("settings", k))
        item = (new_key, psycopg2.extras.Json(as_json(v)), by, at or datetime.now(timezone.utc))
        (runs if where == "run_state" else settings).append(item)
    if settings:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO ops.settings (key, value, updated_by, updated_at) VALUES %s
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                            updated_at = EXCLUDED.updated_at""", settings)
    if runs:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO ops.run_state (run_code, state, updated_by, updated_at) VALUES %s
            ON CONFLICT (run_code) DO UPDATE SET state = EXCLUDED.state, updated_by = EXCLUDED.updated_by,
                                                 updated_at = EXCLUDED.updated_at""", runs)
    cur.execute("SELECT value FROM ops.settings WHERE key = 'discount_pct'")
    d = cur.fetchone()
    report["settings"] = {"copied": len(settings), "run_state_rows": [r[0] for r in runs],
                          "discount_pct_now": d[0] if d else None,
                          "note": "min_margin_pct = 10 is a placeholder (open decision in the design)"}


def copy_reference_data(cur, report):
    cur.execute("SET LOCAL TIME ZONE 'UTC'")        # old times without zone were written by Render (UTC)
    im, skipped = read_item_master(cur)
    brand_ids, by_prefix = copy_brands(cur, report, im)
    copy_options(cur, report, im)
    product_ids = copy_products(cur, report, im, skipped, brand_ids, by_prefix)
    copy_listings_and_stock(cur, report, im, product_ids)
    copy_rules(cur, report, product_ids)
    copy_users(cur, report)
    copy_settings(cur, report)
    report["cost_history"] = "nothing to copy: the old tables have no cost column (Airtable Cost was never copied)"
    if exists(cur, "public.competitor_table"):
        cur.execute("""SELECT count(*) FROM public.competitor_table c
                       WHERE NOT EXISTS (SELECT 1 FROM core.products p WHERE p.legacy_id = c.fi_record_id)""")
        report["competitor_table_rows_without_product"] = cur.fetchone()[0]


# ============================================================ SECTION 3b: Airtable Cost -> core.cost_history
# 2026-11-26 (user: "i need to add cost from airtable. for now just keep in database later use in website").
# French Inventories "Cost" is READ from Airtable (nothing is written to Airtable) and saved in core.cost_history,
# source 'airtable'. Not used by any page yet. Run again later: only products whose Airtable cost CHANGED get a
# new row (the old one stays as history), so nothing is doubled.
# VAT: saved as WITHOUT 5% VAT (vat_included = FALSE), the design's assumption - still an open decision.
# Same Airtable base / table as app.py BASE_ID / FI_TABLE (copied here: app.py imports this file).
AT_BASE_ID = "app5gOqDt9aZrW5bV"
AT_FI_TABLE = "tblL03CEHdYy1kUdQ"            # French Inventories
COST_FIELDS = ["Cost", "Cost Price", "Unit Cost", "Purchase Cost"]   # first one found is used


def norm_field(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def read_airtable_costs(report):
    """[(airtable record id, {field: value})] of French Inventories - READ only, all fields (field names vary)."""
    import requests
    token = os.environ.get("AIRTABLE_TOKEN", "")
    if not token:
        raise RuntimeError("AIRTABLE_TOKEN is not set in Render -> Environment, so Airtable cannot be read")
    url = f"https://api.airtable.com/v0/{AT_BASE_ID}/{AT_FI_TABLE}"
    out, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
        if r.status_code == 429:                   # too many requests -> wait and try again
            log("[NEWDB-COST] Airtable rate limit, waiting 30 s")
            time.sleep(30)
            continue
        if not r.ok:
            raise RuntimeError(f"Airtable read error {r.status_code}: {r.text[:300]}")
        data = r.json()
        out.extend((rec["id"], rec.get("fields", {})) for rec in data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(0.25)
    log(f"[NEWDB-COST] Read {len(out)} French Inventories records from Airtable")
    report["airtable_records_read"] = len(out)
    return out


def cost_number(v):
    """Airtable number / lookup list / text 'AED 95.50' -> 95.5, or None (empty, 0, not a number)."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, str):
        v = re.sub(r"[^0-9.]", "", v.replace(",", ""))
    try:
        v = round(float(v), 2)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def copy_costs(cur, report, records):
    if not exists(cur, "core.products"):
        raise RuntimeError("the new tables are not saved yet - open /newdb/run first")
    seen = {}
    for _, f in records:
        for name in f:
            seen.setdefault(norm_field(name), name)
    report["airtable_fields_with_cost_in_name"] = sorted(n for k, n in seen.items() if "cost" in k)
    field = next((seen[norm_field(c)] for c in COST_FIELDS if norm_field(c) in seen), None)
    if not field:
        raise RuntimeError("no cost field found in French Inventories. Fields with 'cost' in the name: "
                           f"{report['airtable_fields_with_cost_in_name'] or 'none'} - add the right name to "
                           "COST_FIELDS in newdb.py")
    report["cost_field_used"] = field
    cur.execute("SELECT airtable_id, id FROM core.products WHERE airtable_id IS NOT NULL")
    products = dict(cur.fetchall())
    cur.execute("""SELECT DISTINCT ON (product_id) product_id, unit_cost FROM core.cost_history
                   WHERE source = 'airtable' ORDER BY product_id, effective_at DESC, id DESC""")
    last = {pid: float(c) for pid, c in cur.fetchall()}
    new, unchanged, no_cost, no_product = [], 0, 0, []
    for rid, f in records:
        cost = cost_number(f.get(field))
        if cost is None:
            no_cost += 1
            continue
        pid = products.get(rid)
        if not pid:
            no_product.append(f"{rid}: {f.get('SKU') or ''} {f.get('Product Name') or ''} cost {cost}".strip())
            continue
        if last.get(pid) == cost:
            unchanged += 1
            continue
        new.append((pid, cost, f"Airtable French Inventories '{field}'"))
    if new:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO core.cost_history (product_id, unit_cost, vat_included, source, created_by, note)
            VALUES %s""", new, template="(%s, %s, FALSE, 'airtable', 'import', %s)")
    report["cost"] = {"records_with_cost": len(records) - no_cost, "records_without_cost": no_cost,
                      "saved_new_or_changed": len(new), "unchanged_since_last_import": unchanged,
                      "cost_for_product_not_in_new_tables": len(no_product),
                      "vat": "saved as WITHOUT 5% VAT (open decision in the design)"}
    report["cost_not_saved_details"] = no_product


# ============================================================ SECTION 3c: step 3 - shop lists -> competitor.listings
# 2026-11-26 (user: "start step 3"). Design step 3: every shop's product list goes into ONE table,
# competitor.listings, one row per shop + page + size (Shopify variant). The old <shop>_catalog tables and the
# scrapers are NOT changed: this copies from them (safe to repeat - rows are updated, never doubled), so the
# Competitor table keeps working on the old tables meanwhile.
#   price_inc_vat : ALWAYS with 5% VAT. Samawa's 'price' already has VAT; the others: price_inc_tax, or price x 1.05
#   stock_status  : In stock -> in_stock, Out of stock -> out_of_stock, Not on site -> not_on_site
#   check_status  : Branded Perfume / Essenzi 'status' ok -> ok, not_found -> page_missing, blocked/error -> failed
#   last_success_at only moves when the page was read OK -> old prices of failed pages stop counting (design)
#   text cleaned on the way in ("Â", "â€™" from wrong encoding)
# Shops and their columns: db_triggers.SHOP_LISTS (the same list Trigger A uses).
SHOP_EXTRA = {   # shop -> (name column, size column, status column) where they differ from name / volume / -
    "bp": ("bp_title", "bp_size", "bp_status"),
    "es": ("es_title", "es_size", "es_status"),
}
STOCK_MAP = {"in stock": "in_stock", "out of stock": "out_of_stock", "not on site": "not_on_site"}
GIFT_RE = re.compile(r"\b(gift set|set|coffret)\b", re.I)


def clean_text(v):
    """Fix text saved with the wrong encoding: 'CrÃ¨me' -> 'Crème', 'Itâ€™s' -> 'It's', stray 'Â' removed."""
    v = text(v)
    if not v:
        return None
    if "Ã" in v or "â€" in v or "Â" in v:
        for enc in ("cp1252", "latin-1"):              # UTF-8 that was read as Windows-1252 / Latin-1
            try:
                v = v.encode(enc).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
    v = v.replace("Â ", " ").replace("Â", "").replace(" ", " ")
    return " ".join(v.split()) or None


def clean_gtin(v):
    d = re.sub(r"\D", "", str(v or ""))
    return d if 8 <= len(d) <= 14 else None


def any_size_ml(label):
    """'100 ml' / 'EDP 100ML' / '3.4 oz' -> ml (first size found), else None."""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*ml\b", label or "", re.I)
    if m and float(m.group(1)) > 0:
        return float(m.group(1))
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:fl\.?\s*)?oz\b", label or "", re.I)
    return round(float(m.group(1)) * 29.5735, 0) if m and float(m.group(1)) > 0 else None


class BrandFinder:
    """Brand of a shop row: from its brand column (any capitals, ' Beauty' / ' Parfums' dropped) or, for shops
    without one (FF, V Perfumes, Al Injaz ...), from the start of the product name. Longest brand name wins."""
    SUFFIX = re.compile(r"\s+(beauty|fragrances?|parfums?|perfumes?|cosmetics|paris)$", re.I)

    def __init__(self, cur):
        cur.execute("SELECT id, name::text, aliases FROM core.brands")
        self.by_name, self.by_first = {}, {}
        for bid, name, aliases in cur.fetchall():
            for n in [name] + list(aliases or []):
                k = " ".join(n.lower().split())
                if not k:
                    continue
                self.by_name.setdefault(k, bid)
                self.by_first.setdefault(k.split()[0], []).append((k, bid))
        for lst in self.by_first.values():
            lst.sort(key=lambda x: -len(x[0]))

    def find(self, brand_raw, name):
        k = " ".join((brand_raw or "").lower().split())
        if k:
            if k in self.by_name:
                return self.by_name[k]
            k2 = self.SUFFIX.sub("", k).strip()
            if k2 in self.by_name:
                return self.by_name[k2]
        n = " ".join((name or "").lower().split())
        for cand, bid in self.by_first.get(n.split()[0] if n else "", []):
            if n == cand or n.startswith(cand + " "):
                return bid
        return None


def copy_shop_lists(cur, report, only=None, since=None):
    from db_triggers import SHOP_LISTS                     # (shop, table, link column, price columns, stock column)
    cur.execute("SET LOCAL TIME ZONE 'UTC'")               # old times without zone were written by NOW() (UTC)
    if not exists(cur, "core.brands"):
        raise RuntimeError("the new tables are not saved yet - open /newdb/run first")
    cur.execute("SELECT code FROM competitor.shops")
    known = {r[0] for r in cur.fetchall()}
    lists = [x for x in SHOP_LISTS if only is None or x[0] in only]
    unknown = sorted(set(only or []) - {x[0] for x in SHOP_LISTS})
    if unknown:
        raise RuntimeError(f"unknown shop code(s): {unknown}. Codes: {[x[0] for x in SHOP_LISTS]}")
    brands = BrandFinder(cur)
    report["shops"] = {}
    for shop, table, link_col, price_cols, stock_col in lists:
        r = report["shops"][shop] = {"from_table": table}
        if shop not in known:
            r["result"] = "skipped: shop not in competitor.shops"
            continue
        if not exists(cur, "public." + table):
            r["result"] = "skipped: table does not exist (shop never downloaded)"
            continue
        have = columns(cur, table)
        name_c, size_c, status_c = SHOP_EXTRA.get(shop, ("name", "volume", None))

        def col(c, fallback=None):
            return c if c and c in have else (fallback if fallback and fallback in have else "NULL")
        inc_c = price_cols[0] if len(price_cols) == 2 else None      # with VAT
        ex_c = price_cols[-1]                                       # Samawa: its only 'price' (already with VAT)
        sel = [col(link_col), col("variant_id"), col("product_id"), col("gtin"), col("sku"), col("brand"),
               f"COALESCE({col(name_c)}, {col('name')})" if name_c != "name" else col("name"),
               col("product_type"),
               f"COALESCE({col(size_c)}, {col('volume')})" if size_c != "volume" else col("volume"),
               col(inc_c), col(ex_c), col(stock_col), col(status_c), col("checked_at"), col("updated_at")]
        if since is not None and "updated_at" in have:     # automatic sync: only rows changed since last time
            cur.execute(f"SELECT {', '.join(sel)} FROM public.{table} WHERE updated_at > %s", (since,))
        else:
            cur.execute(f"SELECT {', '.join(sel)} FROM public.{table}")
        rows, seen = [], set()
        stats = {"rows_in_old_list": 0, "no_link": 0, "same_page_twice": 0, "no_price": 0,
                 "brand_found": 0, "with_barcode": 0, "with_size": 0, "price_from_ex_vat_x_1_05": 0}
        for (url, variant, pid, gtin, sku, brand, name, ptype, size, inc, ex, stock, status, checked,
             updated) in cur.fetchall():
            stats["rows_in_old_list"] += 1
            url = text(url)
            if not url:
                stats["no_link"] += 1                  # Branded Perfume / Essenzi page not found yet
                continue
            variant = text(variant)
            k = (url, variant or "")
            if k in seen:
                stats["same_page_twice"] += 1
                continue
            seen.add(k)
            name = clean_text(name) or url
            if shop == "samawa":
                price = money(ex)
            elif inc is not None:
                price = money(inc)
            elif ex is not None:
                price = money(float(ex) * 1.05)
                stats["price_from_ex_vat_x_1_05"] += 1
            else:
                price = None
            if price is None:
                stats["no_price"] += 1
            st = STOCK_MAP.get((stock or "").strip().lower(), "unknown")
            s_ = (status or "").strip().lower()
            if status_c and s_:
                check = "ok" if s_ == "ok" else ("page_missing" if "not_found" in s_ or "missing" in s_ else "failed")
            else:
                check = "page_missing" if st == "not_on_site" else "ok"
            when = checked or updated
            success = when if check == "ok" and price is not None else None
            bid = brands.find(brand, name)
            gt = clean_gtin(gtin)
            size = clean_text(size)
            ml = any_size_ml(size) or any_size_ml(name)
            stats["brand_found"] += 1 if bid else 0
            stats["with_barcode"] += 1 if gt else 0
            stats["with_size"] += 1 if ml else 0
            low = name.lower()
            rows.append((shop, url, text(pid), variant, gt, text(sku), clean_text(brand), bid, name,
                         clean_text(ptype), ml, size, "tester" in low, bool(GIFT_RE.search(low)), price, st,
                         check, when, success))
        for i in range(0, len(rows), 1000):
            psycopg2.extras.execute_values(cur, """
                INSERT INTO competitor.listings (shop_code, url, external_product_id, external_variant_id, gtin,
                    shop_sku, brand_raw, brand_id, name, product_type, size_ml, size_label, is_tester, is_gift_set,
                    price_inc_vat, stock_status, check_status, last_checked_at, last_success_at)
                VALUES %s
                ON CONFLICT (shop_code, url, COALESCE(external_variant_id, '')) DO UPDATE SET
                  external_product_id = EXCLUDED.external_product_id, gtin = EXCLUDED.gtin,
                  shop_sku = EXCLUDED.shop_sku, brand_raw = EXCLUDED.brand_raw,
                  brand_id = COALESCE(EXCLUDED.brand_id, competitor.listings.brand_id), name = EXCLUDED.name,
                  product_type = EXCLUDED.product_type, size_ml = EXCLUDED.size_ml, size_label = EXCLUDED.size_label,
                  is_tester = EXCLUDED.is_tester, is_gift_set = EXCLUDED.is_gift_set,
                  price_inc_vat = COALESCE(EXCLUDED.price_inc_vat, competitor.listings.price_inc_vat),
                  stock_status = EXCLUDED.stock_status, check_status = EXCLUDED.check_status,
                  last_checked_at = EXCLUDED.last_checked_at,
                  last_success_at = COALESCE(EXCLUDED.last_success_at, competitor.listings.last_success_at)""",
                rows[i:i + 1000])
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE stock_status = 'in_stock'),
                              max(last_success_at)::text FROM competitor.listings WHERE shop_code = %s""", (shop,))
        total, in_stock, newest = cur.fetchone()
        cur.execute("UPDATE competitor.shops SET last_success_at = %s::timestamptz WHERE code = %s AND %s IS NOT NULL",
                    (newest, shop, newest))
        stats.update(copied=len(rows), listings_now=total, in_stock_now=in_stock, newest_price_read=newest)
        r.update(stats)
        r["result"] = "ok"
        log(f"[NEWDB-SHOPS] {shop}: {len(rows)} rows -> competitor.listings ({total} now)")
    cur.execute("SELECT count(*) FROM competitor.listing_history")
    report["listing_history_rows"] = cur.fetchone()[0]


# ============================================================ SECTION 3d: step 3 - keep competitor.listings up to date
# 2026-11-27 (step 3, last part): every SYNC_MINUTES the website copies the shop-list rows that changed since the
# last check (each download - PC or website - stamps its rows with updated_at). So competitor.listings follows the old
# lists automatically, whoever downloaded the shop. Several website processes -> a database lock lets only one copy
# at a time. Progress: ops.run_state 'newdb_shop_sync' (shown on /newdb/status).
# Switch off: Render env NEWDB_SYNC=off.  Never runs on the production database.
SYNC_MINUTES = 15
SYNC_RUN_CODE = "newdb_shop_sync"


def sync_shop_lists_once():
    """Copy the changed rows of every shop list. Returns a short summary (or why it was skipped)."""
    if db_name() == PRODUCTION_DB:
        return {"skipped": "production database"}
    conn = psycopg2.connect(os.environ["FF_DATABASE_URL"], sslmode="require")
    try:
        cur = conn.cursor()
        if not exists(cur, "competitor.listings") or not exists(cur, "ops.run_state"):
            return {"skipped": "new tables not saved yet"}
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('newdb_job'))")   # same lock as the manual jobs
        if not cur.fetchone()[0]:
            return {"skipped": "another /newdb job or sync is running"}
        cur.execute("SELECT now(), (SELECT state->>'last_started' FROM ops.run_state WHERE run_code = %s)",
                    (SYNC_RUN_CODE,))
        now, last = cur.fetchone()
        # first automatic run: start from the newest copy already made by hand (nothing missed, nothing redone)
        since = last
        if since is None:
            cur.execute("SELECT max(updated_at)::text FROM competitor.listings")
            since = cur.fetchone()[0]
        cur.execute("SELECT (%s::timestamptz - interval '5 minutes')::text", (since,))   # small overlap, safe
        since = cur.fetchone()[0]
        report = {}
        copy_shop_lists(cur, report, None, since)
        changed = {k: v.get("copied") for k, v in report.get("shops", {}).items() if v.get("copied")}
        state = {"last_started": now.isoformat(), "changed_since": since, "rows_copied": changed,
                 "total_rows_copied": sum(changed.values())}
        # step 4 (2026-11-27): once the links were copied by hand (/newdb-matches/run), keep them in step too -
        # links still change on the old Competitor table (match runs, OK / Not OK, Edit link)
        cur.execute("SELECT EXISTS (SELECT 1 FROM competitor.matches)")
        if cur.fetchone()[0]:
            mrep = {}
            copy_matches(cur, mrep, None)
            state["links_changed"] = {k: v.get("changed") for k, v in mrep.get("shops", {}).items()
                                      if v.get("changed")}
        cur.execute("""INSERT INTO ops.run_state (run_code, state, updated_by) VALUES (%s, %s, 'newdb sync')
                       ON CONFLICT (run_code) DO UPDATE SET state = EXCLUDED.state, updated_by = EXCLUDED.updated_by,
                                                            updated_at = now()""",
                    (SYNC_RUN_CODE, psycopg2.extras.Json(state)))
        conn.commit()
        if changed:
            log(f"[NEWDB-SYNC] {state['total_rows_copied']} changed shop-list rows copied: {changed}")
        return state
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_loop():
    time.sleep(120)                                   # let the website start first
    while True:
        try:
            sync_shop_lists_once()
        except Exception as ex:                       # never stops the website; tries again next time
            log(f"[NEWDB-SYNC] ERROR (will try again in {SYNC_MINUTES} min): {str(ex).strip()[:300]}")
        time.sleep(SYNC_MINUTES * 60)


if os.environ.get("NEWDB_SYNC", "on").lower() == "off":
    log("[NEWDB-SYNC] switched off (NEWDB_SYNC=off)")
elif db_name() == PRODUCTION_DB:
    log("[NEWDB-SYNC] not started: production database")
elif os.environ.get("FF_DATABASE_URL"):
    threading.Thread(target=sync_loop, daemon=True, name="newdb-sync").start()
    log(f"[NEWDB-SYNC] started: changed shop-list rows -> competitor.listings every {SYNC_MINUTES} min")


# ============================================================ SECTION 3e: step 4 - links -> competitor.matches
# 2026-11-27 (user: "start step 4"). Every link on the old Competitor table becomes ONE row in competitor.matches,
# pointing at the EXACT row (page + size) in competitor.listings. Old table columns per shop -> new place:
#   <shop>_link        -> competitor.matches (one per product per shop)
#   <shop>_method      -> method: 'barcode...' barcode | 'fuzzy...' / 'confirmed' fuzzy | 'set by X' manual |
#                         Branded Perfume / Essenzi follow French Fragrance -> 'follows'
#   <shop>_confirmed   -> confirmed (OK clicked or link set by an admin)
#   <shop>_suggestion  -> competitor.match_suggestions, status 'pending'
#   <shop>_rejected    -> competitor.match_suggestions, status 'rejected' (every refused link, all sizes of it)
# One page with several sizes: the size is chosen by 1 same barcode, 2 same size (ml), 3 same price as the old
# table shows. Still unclear -> no link, every size goes to the OK / Not OK queue ('pending'), as the design says.
# The old table stays the master until step 5: running this again makes the new tables follow it (links removed
# there are removed here, rejected links are never removed). Unlinked shops (UNLINKED_SHOPS in app.py) have no links.
def url_key(u):
    """Same page written slightly differently: no http(s)://, no www., no trailing /, lower case."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def map_method(shop, method):
    m = (method or "").strip().lower()
    if shop in ("bp", "es"):
        return "follows"
    if m.startswith("barcode"):
        return "barcode"
    if m.startswith("set by") or m.startswith("manual") or m.startswith("added by"):
        return "manual"
    return "fuzzy"                                   # fuzzy..., confirmed (a name suggestion accepted with OK)


def choose_size(cands, product, old_price):
    """cands: [(listing_id, size_ml, gtin, price)] on the same page. Returns (listing_id, how) or (None, 'unclear')."""
    if len(cands) == 1:
        return cands[0][0], "one size"
    barcode, ml = product
    if barcode:
        hit = [c for c in cands if c[2] and c[2].lstrip("0") == barcode.lstrip("0")]
        if len(hit) == 1:
            return hit[0][0], "barcode"
    if ml:
        hit = [c for c in cands if c[1] and abs(float(c[1]) - float(ml)) < 0.5]
        if len(hit) == 1:
            return hit[0][0], "size"
        if hit:
            cands = hit
    if old_price is not None:
        hit = [c for c in cands if c[3] is not None and abs(float(c[3]) - float(old_price)) < 0.01]
        if len(hit) == 1:
            return hit[0][0], "price"
    return None, "unclear"


def copy_matches(cur, report, only=None):
    from db_triggers import COMPETITORS                # (label, prefix) - prefix = shop code in competitor.shops
    if not exists(cur, "competitor.listings"):
        raise RuntimeError("the new tables are not saved yet - open /newdb/run first")
    codes = [c for _, c in COMPETITORS]
    unknown = sorted(set(only or []) - set(codes))
    if unknown:
        raise RuntimeError(f"unknown shop code(s): {unknown}. Codes: {codes}")
    have = columns(cur, "competitor_table")
    cur.execute("SELECT legacy_id, id, barcode, size_ml FROM core.products WHERE legacy_id IS NOT NULL")
    products = {lid: (pid, (bc, ml)) for lid, pid, bc, ml in cur.fetchall()}
    report["shops"], details = {}, {}
    for shop in [c for c in codes if only is None or c in only]:
        r = report["shops"][shop] = {}
        if f"{shop}_link" not in have:
            r["result"] = "skipped: no link column in competitor_table"
            continue
        # this shop's listings: by exact link and by a forgiving link key
        cur.execute("""SELECT id, url, size_ml, gtin, price_inc_vat FROM competitor.listings
                       WHERE shop_code = %s""", (shop,))
        by_url, by_key = {}, {}
        for lid, url, ml, gt, price in cur.fetchall():
            by_url.setdefault(url, []).append((lid, ml, gt, price))
            by_key.setdefault(url_key(url), []).append((lid, ml, gt, price))

        def find(link):
            return by_url.get(link) or by_key.get(url_key(link)) or []

        def col(c):
            return f"{shop}_{c}" if f"{shop}_{c}" in have else "NULL"
        cur.execute(f"""SELECT fi_record_id, {shop}_link, {col('method')}, {col('score')}, {col('confirmed')},
                               {col('price')}, {col('suggestion')}, {col('rejected')} FROM competitor_table""")
        new_matches, pending, rejected = {}, set(), set()
        st = {"links_in_old_table": 0, "linked": 0, "size_chosen_by": {}, "unclear_size_sent_to_ok_queue": 0,
              "link_not_in_shop_list": 0, "product_not_in_new_tables": 0, "suggestions_pending": 0,
              "rejected_links": 0}
        missing, unclear = [], []
        for fid, link, method, score, confirmed, old_price, suggestion, rej in cur.fetchall():
            link, suggestion = text(link), text(suggestion)
            if not (link or suggestion or text(rej)):
                continue
            if fid not in products:
                st["product_not_in_new_tables"] += 1
                continue
            pid, prod = products[fid]
            if link:
                st["links_in_old_table"] += 1
                cands = find(link)
                if not cands:
                    st["link_not_in_shop_list"] += 1
                    missing.append(f"{fid}: {link}")
                else:
                    lid, how = choose_size(cands, prod, old_price)
                    if lid:
                        st["linked"] += 1
                        st["size_chosen_by"][how] = st["size_chosen_by"].get(how, 0) + 1
                        sc = float(score) if score is not None and 0 <= float(score) <= 1 else None
                        new_matches[pid] = (lid, map_method(shop, method), sc, bool(confirmed),
                                            text(method)[7:].strip() if (text(method) or "").lower().startswith("set by") else None)
                    else:
                        st["unclear_size_sent_to_ok_queue"] += 1
                        unclear.append(f"{fid}: {link} ({len(cands)} sizes)")
                        pending.update((pid, c[0]) for c in cands)
            if suggestion:
                cands = find(suggestion)
                lid, _ = choose_size(cands, prod, None) if cands else (None, "")
                pending.update([(pid, lid)] if lid else [(pid, c[0]) for c in cands])
            for u in (rej or "").split("\n"):
                if text(u):
                    rejected.update((pid, c[0]) for c in find(text(u)))
        pending -= rejected                              # refused once -> never suggested again
        pending -= {(pid, m[0]) for pid, m in new_matches.items()}
        st["suggestions_pending"], st["rejected_links"] = len(pending), len(rejected)

        # make competitor.matches follow the old table (only rows that changed are written)
        cur.execute("SELECT product_id, listing_id, method, confirmed FROM competitor.matches WHERE shop_code = %s",
                    (shop,))
        old = {pid: (lid, m, c) for pid, lid, m, c in cur.fetchall()}
        gone = [pid for pid in old if pid not in new_matches]
        if gone:
            cur.execute("DELETE FROM competitor.matches WHERE shop_code = %s AND product_id = ANY(%s)", (shop, gone))
        upd = [(pid, shop, lid, m, sc, c, by) for pid, (lid, m, sc, c, by) in new_matches.items()
               if old.get(pid) != (lid, m, c)]
        for i in range(0, len(upd), 1000):
            psycopg2.extras.execute_values(cur, """
                INSERT INTO competitor.matches (product_id, shop_code, listing_id, method, score, confirmed, matched_by)
                VALUES %s
                ON CONFLICT (product_id, shop_code) DO UPDATE SET listing_id = EXCLUDED.listing_id,
                  method = EXCLUDED.method, score = EXCLUDED.score, confirmed = EXCLUDED.confirmed,
                  matched_by = EXCLUDED.matched_by, matched_at = now()""", upd[i:i + 1000])
        # suggestions: rejected kept for ever; pending follows the old table
        if rejected:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO competitor.match_suggestions (product_id, listing_id, shop_code, method, status,
                                                          decided_by, decided_at)
                VALUES %s
                ON CONFLICT (product_id, listing_id) DO UPDATE SET status = 'rejected'
                WHERE competitor.match_suggestions.status <> 'rejected'""",
                [(a, b, shop) for a, b in rejected], template="(%s, %s, %s, 'imported', 'rejected', 'import', now())")
        cur.execute("""SELECT product_id, listing_id FROM competitor.match_suggestions
                       WHERE shop_code = %s AND status = 'pending'""", (shop,))
        old_pending = set(cur.fetchall())
        stale = list(old_pending - pending)
        if stale:
            psycopg2.extras.execute_values(cur, """
                DELETE FROM competitor.match_suggestions s USING (VALUES %s) AS v(p, l)
                WHERE s.product_id = v.p AND s.listing_id = v.l AND s.status = 'pending'""", stale)
        add = list(pending - old_pending)
        if add:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO competitor.match_suggestions (product_id, listing_id, shop_code, method, status)
                VALUES %s ON CONFLICT (product_id, listing_id) DO NOTHING""",
                [(a, b, shop) for a, b in add], template="(%s, %s, %s, 'imported', 'pending')")
        st["changed"] = len(gone) + len(upd) + len(stale) + len(add)
        st["result"] = "ok"
        r.update(st)
        details[shop] = {"link_not_in_shop_list": missing, "unclear_size": unclear}
        log(f"[NEWDB-MATCHES] {shop}: {st['linked']} linked, {st['suggestions_pending']} pending, "
            f"{st['rejected_links']} rejected, {st['changed']} rows changed")
    report["matches_details"] = details
    cur.execute("SELECT count(*), count(*) FILTER (WHERE confirmed) FROM competitor.matches")
    report["matches_total"], report["matches_confirmed"] = cur.fetchone()
    # how many products now get a price from the new tables (fresh, in stock, matched)
    cur.execute("SELECT count(DISTINCT product_id) FROM competitor.v_valid_prices")
    report["products_with_a_valid_competitor_price_now"] = cur.fetchone()[0]


# ============================================================ SECTION 4: counts
V2_TABLES = ["ops.users", "ops.settings", "ops.run_state", "core.brands", "core.option_values", "core.products",
             "core.product_listings", "core.stock_levels", "core.cost_history", "competitor.shops",
             "competitor.listings", "competitor.matches", "pricing.product_rules"]


def counts(cur):
    out = {}
    for t in V2_TABLES:
        if exists(cur, t):
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        else:
            out[t] = "not created yet"
    return out


# ============================================================ SECTION 5: the job (background thread)
JOB_NAMES = {"all": "steps 1 + 2", "cost": "Airtable cost -> core.cost_history",
             "shops": "step 3: shop lists -> competitor.listings",
             "matches": "step 4: links -> competitor.matches"}


def job(save, kind="all", shops=None):
    started = time.time()
    report = {"job": JOB_NAMES[kind],
              "mode": "RUN (saved)" if save else "TEST (nothing saved)", "database": db_name(),
              "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    status.clear()
    status.update(state="running", report=report)
    conn = None
    try:
        # Airtable is read BEFORE the database transaction opens (reading 9,600 records takes about a minute)
        airtable = read_airtable_costs(report) if kind == "cost" else None
        conn = psycopg2.connect(os.environ["FF_DATABASE_URL"], sslmode="require")
        cur = conn.cursor()
        # one run at a time across ALL website processes (the threading lock only covers one process)
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('newdb_job'))")
        if not cur.fetchone()[0]:
            raise RuntimeError("another /newdb run is still going - wait for it to finish")
        if kind == "cost":
            copy_costs(cur, report, airtable)
        elif kind == "shops":
            copy_shop_lists(cur, report, shops)
        elif kind == "matches":
            copy_matches(cur, report, shops)
        else:
            apply_migrations(cur, report)            # step 1 and step 2 in ONE transaction:
            copy_reference_data(cur, report)         # any error -> nothing at all is saved
        report["new_table_counts"] = counts(cur)
        if save:
            conn.commit()
            report["result"] = "SAVED"
        else:
            conn.rollback()
            again = {"all": "/newdb/run", "cost": "/newdb-cost/run", "shops": "/newdb-shops/run",
                     "matches": "/newdb-matches/run"}[kind]
            report["result"] = f"TEST OK - everything undone, nothing saved. Open {again} to save."
        status["state"] = "done"
    except Exception as ex:
        if conn:
            conn.rollback()
        report["result"] = "ERROR - nothing saved"
        report["error"] = str(ex).strip()[:1500]
        status["state"] = "error"
        log(f"[NEWDB] ERROR: {ex}")
    finally:
        if conn:
            conn.close()
        report["seconds"] = round(time.time() - started, 1)
        log(f"[NEWDB] {report['result']} in {report['seconds']} s")
        save_report(report)
        lock.release()


# 2026-11-26: the website runs in several processes; each had its own `status`, so /newdb/status could show an
# old "running". The finished report is now saved in table public.newdb_reports -> every process shows the same.
def save_report(report):
    try:
        c = psycopg2.connect(os.environ["FF_DATABASE_URL"], sslmode="require")
        cur = c.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS public.newdb_reports (
                           id SERIAL PRIMARY KEY, finished_at TIMESTAMPTZ NOT NULL DEFAULT now(), report JSONB)""")
        cur.execute("INSERT INTO public.newdb_reports (report) VALUES (%s)", (psycopg2.extras.Json(report, dumps=lambda o: json.dumps(o, default=str)),))
        c.commit()
        c.close()
    except Exception as ex:
        log(f"[NEWDB] could not save the report: {ex}")


def start(save, kind="all", shops=None):
    if not secret_ok():
        return jsonify({"ok": False, "error": "wrong secret"}), 403
    if db_name() == PRODUCTION_DB:
        log("[NEWDB] Refused: this is the production database")
        return jsonify({"ok": False, "error": f"refused: {PRODUCTION_DB} is the production database. "
                                              "The new tables are tried on the test copy first."}), 403
    if not lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "already running - see /newdb/status"}), 409
    threading.Thread(target=job, args=(save, kind, shops), daemon=True, name="newdb").start()
    return jsonify({"ok": True, "started": "run (saves)" if save else "test (saves nothing)",
                    "see": "/newdb/status?secret=..."})


@newdb_bp.route("/newdb/test")
def newdb_test():
    """Steps 1 + 2 completely, then everything undone."""
    return start(save=False)


@newdb_bp.route("/newdb/run")
def newdb_run():
    """Steps 1 + 2, saved."""
    return start(save=True)


@newdb_bp.route("/newdb-cost/test")
def newdb_cost_test():
    """Airtable Cost -> core.cost_history, then undone (nothing saved)."""
    return start(save=False, kind="cost")


@newdb_bp.route("/newdb-cost/run")
def newdb_cost_run():
    """Airtable Cost -> core.cost_history, saved."""
    return start(save=True, kind="cost")


def wanted_shops():
    """?shops=am,mh -> ['am', 'mh'];  ?shops=all or nothing -> None (= every shop)."""
    v = (request.args.get("shops") or "").strip().lower()
    return None if v in ("", "all") else [x.strip() for x in v.split(",") if x.strip()]


@newdb_bp.route("/newdb-shops/test")
def newdb_shops_test():
    """Shop lists -> competitor.listings, then undone (nothing saved). ?shops=am,mh (default: all shops)."""
    return start(save=False, kind="shops", shops=wanted_shops())


@newdb_bp.route("/newdb-shops/run")
def newdb_shops_run():
    """Shop lists -> competitor.listings, saved. ?shops=am,mh (default: all shops)."""
    return start(save=True, kind="shops", shops=wanted_shops())


@newdb_bp.route("/newdb-matches/test")
def newdb_matches_test():
    """Step 4: competitor_table links -> competitor.matches / match_suggestions, then undone. ?shops=am,vp"""
    return start(save=False, kind="matches", shops=wanted_shops())


@newdb_bp.route("/newdb-matches/run")
def newdb_matches_run():
    """Step 4, saved. ?shops=am,vp (default: all shops)."""
    return start(save=True, kind="matches", shops=wanted_shops())


@newdb_bp.route("/newdb-sync/run")
def newdb_sync_run():
    """Copy the changed shop-list rows NOW (the same thing the automatic check does every SYNC_MINUTES)."""
    if not secret_ok():
        return jsonify({"ok": False, "error": "wrong secret"}), 403
    try:
        return jsonify({"ok": True, "result": sync_shop_lists_once()})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex).strip()[:500]}), 500


@newdb_bp.route("/newdb/status")
def newdb_status():
    if not secret_ok():
        return jsonify({"ok": False, "error": "wrong secret"}), 403
    # 2026-11-26: the LAST report of EACH job is shown separately (a main test after the cost test hid the cost
    # report). Long lists are shortened to counts; add &full=1 to see them.
    full = request.args.get("full") == "1"
    running = status.get("state") == "running"
    out = {"running_now_in_this_process": (status.get("report") or {}).get("job") if running else None}
    try:
        conn = psycopg2.connect(os.environ["FF_DATABASE_URL"], sslmode="require")
        cur = conn.cursor()
        for label, job_name in (("last_matches", JOB_NAMES["matches"]),
                                ("last_shop_lists", JOB_NAMES["shops"]),
                                ("last_cost_import", JOB_NAMES["cost"]),
                                ("last_steps_1_2", JOB_NAMES["all"])):
            row = None
            if exists(cur, "public.newdb_reports"):
                cur.execute("""SELECT finished_at::text, report FROM public.newdb_reports
                               WHERE COALESCE(report->>'job', 'steps 1 + 2') = %s ORDER BY id DESC LIMIT 1""",
                            (job_name,))
                row = cur.fetchone()
            if not row:
                out[label] = "not run yet (or still running)"
                continue
            rep = dict(row[1])
            if not full:
                for k in ("temporary_sku_fix_in_item_master", "not_copied_details", "cost_not_saved_details",
                          "matches_details"):
                    if isinstance(rep.get(k), list):
                        rep[k] = f"{len(rep[k])} rows (add &full=1 to see them)"
                    elif isinstance(rep.get(k), dict):
                        rep[k] = {a: ({x: len(y) for x, y in b.items()} if isinstance(b, dict) else len(b))
                                  for a, b in rep[k].items()}
            out[label] = {"finished_at": row[0], "result": rep.get("result"), "report": rep}
        if exists(cur, "ops.run_state"):
            cur.execute("SELECT state, updated_at::text FROM ops.run_state WHERE run_code = %s", (SYNC_RUN_CODE,))
            row = cur.fetchone()
            out["automatic_shop_sync"] = ({"every_minutes": SYNC_MINUTES, "last": row[0], "saved_at": row[1]}
                                          if row else f"not run yet (first check about 2 min after start, "
                                                      f"then every {SYNC_MINUTES} min)")
        out["saved_now"] = counts(cur)
        if exists(cur, "public.newdb_migrations"):
            cur.execute("SELECT name, applied_at::text FROM public.newdb_migrations ORDER BY name")
            out["applied_files"] = cur.fetchall()
        conn.close()
    except Exception as ex:
        out["saved_now"] = f"could not read: {ex}"
    return jsonify(out)
