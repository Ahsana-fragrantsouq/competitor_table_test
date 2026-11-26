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
def job(save):
    started = time.time()
    report = {"mode": "RUN (saved)" if save else "TEST (nothing saved)", "database": db_name(),
              "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    status.clear()
    status.update(state="running", report=report)
    conn = None
    try:
        conn = psycopg2.connect(os.environ["FF_DATABASE_URL"], sslmode="require")
        cur = conn.cursor()
        apply_migrations(cur, report)                # step 1 and step 2 in ONE transaction:
        copy_reference_data(cur, report)             # any error -> nothing at all is saved
        report["new_table_counts"] = counts(cur)
        if save:
            conn.commit()
            report["result"] = "SAVED"
        else:
            conn.rollback()
            report["result"] = "TEST OK - everything undone, nothing saved. Open /newdb/run to save."
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
        lock.release()


def start(save):
    if not secret_ok():
        return jsonify({"ok": False, "error": "wrong secret"}), 403
    if db_name() == PRODUCTION_DB:
        log("[NEWDB] Refused: this is the production database")
        return jsonify({"ok": False, "error": f"refused: {PRODUCTION_DB} is the production database. "
                                              "The new tables are tried on the test copy first."}), 403
    if not lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "already running - see /newdb/status"}), 409
    threading.Thread(target=job, args=(save,), daemon=True, name="newdb").start()
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


@newdb_bp.route("/newdb/status")
def newdb_status():
    if not secret_ok():
        return jsonify({"ok": False, "error": "wrong secret"}), 403
    out = dict(status)
    try:
        conn = psycopg2.connect(os.environ["FF_DATABASE_URL"], sslmode="require")
        cur = conn.cursor()
        out["saved_now"] = counts(cur)
        if exists(cur, "public.newdb_migrations"):
            cur.execute("SELECT name, applied_at::text FROM public.newdb_migrations ORDER BY name")
            out["applied_files"] = cur.fetchall()
        conn.close()
    except Exception as ex:
        out["saved_now"] = f"could not read: {ex}"
    return jsonify(out)
