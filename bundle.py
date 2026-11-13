r"""
Bundle menu: list of bundles + Create bundle form (2026-11-12/13)
=================================================================
User: menu "Bundle" after Item Master with "Create bundle form"; then the form:
  1. pick products: minimum 2, up to 5 (prod1 + prod2 + ...), each with a QUANTITY (user's choice: "yes, with quantity")
  2. Bundle title - default "1st + 2nd + ..." (product names), can be edited
  3. Bundle discount - an AMOUNT (AED) or a %
  4. Auto SKU - the product SKUs joined together, e.g. AFN1057ARFM1002 (user: "for now ... can change later")
  5. Add - ONLY when EVERY product has enough stock in SHOPIFY (live check at Add; out of stock -> not added)
  Saved in OUR database (table `bundles`), shown in the list "Bundles" like Item Master (user's choice) - NOT in Shopify.
  Price = sum of each product's UAE price x quantity (user's choice: UAE price), minus the discount.

Menu Bundle ▾ -> Bundles (/bundle) | Create bundle form (/bundle/new). Creating = admins only; everyone logged in sees the list.

Where things are (change them here):
  SECTION 1  settings        -> BUNDLE_OPTIONS (menu), MIN_ITEMS / MAX_ITEMS / MAX_QTY, SKU_JOIN
  SECTION 2  table           -> BUNDLES_CREATE (bundles)
  SECTION 3  products        -> product_rows(), GET /bundle/products?q= (search for the form)
  SECTION 4  Shopify stock   -> shopify_stock() (live 'available' of one SKU)
  SECTION 5  create          -> POST /bundle/create
  SECTION 6  pages           -> FORM_HTML (/bundle/new), LIST_HTML (/bundle)
  SECTION 7  hooks           -> init(): database connection from app.py
"""

import json
import re
from decimal import Decimal, ROUND_HALF_UP

import psycopg2.extras
from flask import Blueprint, render_template_string, request, jsonify, g

from auth import is_admin

bundle_bp = Blueprint("bundle", __name__)


def log(*a):
    """Print to the Render log (lines start with [BUNDLE])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
# Bundle dropdown: (key = cur_key of that page, link, text). New pages = one more line here.
BUNDLE_OPTIONS = [
    ("bundle-list", "/bundle", "Bundles"),
    ("bundle-new", "/bundle/new", "Create bundle form"),
]
MIN_ITEMS, MAX_ITEMS = 2, 5          # products in one bundle
MAX_QTY = 20                         # quantity of one product in a bundle
SKU_JOIN = ""                        # AFN1057 + ARM1002 -> "AFN1057ARM1002" (change to "-" / "+" later if wanted)


# ============================================================ SECTION 2: table
BUNDLES_CREATE = """
CREATE TABLE IF NOT EXISTS bundles (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    sku             TEXT NOT NULL,
    items           JSONB NOT NULL,          -- [{fi, sku, name, qty, price, image}] in the order picked
    total_price     NUMERIC(10,2),           -- sum of UAE price x qty
    discount_type   TEXT,                    -- 'amount' (AED) or 'percent'
    discount_value  NUMERIC(10,2),
    discount_aed    NUMERIC(10,2),           -- the discount in AED
    bundle_price    NUMERIC(10,2),           -- total_price - discount_aed
    stock_check     JSONB,                   -- Shopify stock of every product at Add: {sku: available}
    created_by      TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);
"""


def money(x):
    """Decimal with 2 places (AED)."""
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ============================================================ SECTION 3: products
_hooks = {}

PRODUCT_SQL = """
SELECT im.fi_record_id AS fi, im.sku, COALESCE(NULLIF(im.product_name, ''), ct.product_name) AS name, im.brand,
       COALESCE(ct.uae_price, im.uae_price) AS price, im.image_urls, im.shopify_id,
       ct.our_stock_qty AS stock, ct.our_link
  FROM item_master im LEFT JOIN competitor_table ct ON ct.fi_record_id = im.fi_record_id
"""


def first_image(urls):
    """First link of image_urls (one link per line) or None."""
    for line in (urls or "").splitlines():
        if line.strip().startswith("http"):
            return line.strip()
    return None


def product_rows(cur, where, params, limit=15):
    cur.execute(PRODUCT_SQL + where + " LIMIT %s", (*params, limit))
    out = []
    for r in cur.fetchall():
        out.append({"fi": r["fi"], "sku": (r["sku"] or "").strip(), "name": r["name"] or r["sku"] or r["fi"],
                    "brand": r["brand"], "price": float(r["price"]) if r["price"] is not None else None,
                    "image": first_image(r["image_urls"]), "shopify_id": (r["shopify_id"] or "").strip(),
                    "stock": float(r["stock"]) if r["stock"] is not None else None, "on_site": bool(r["our_link"])})
    return out


@bundle_bp.get("/bundle/products")
def bundle_products():
    """?q=words -> up to 15 products (every word in SKU / brand / name / barcode, like the Item Master search)."""
    q = (request.args.get("q") or "").strip()
    words = [w for w in re.split(r"\s+", q) if w][:6]
    if not words:
        return jsonify({"ok": True, "items": []})
    where = " WHERE " + " AND ".join(
        "(COALESCE(im.sku,'') || ' ' || COALESCE(im.brand,'') || ' ' || COALESCE(im.product_name,'') || ' ' || "
        "COALESCE(im.perfume_name,'') || ' ' || COALESCE(im.barcode,'')) ILIKE %s" for _ in words)
    where += " ORDER BY (ct.our_link IS NULL), im.brand, im.product_name"
    conn = _hooks["connect"]()
    try:
        items = product_rows(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor), where,
                             [f"%{w}%" for w in words])
    finally:
        conn.close()
    log(f"[BUNDLE] search {q!r}: {len(items)} products")
    return jsonify({"ok": True, "items": items})


# ============================================================ SECTION 4: Shopify stock
def shopify_stock(sku, shopify_id):
    """Live Shopify 'available' of this SKU (all locations) -> (number, None) or (None, reason)."""
    import shopify_create as sc
    import item_edit
    if not (sc.SHOP and sc.TOKEN):
        return None, "Shopify is not set up (SHOPIFY_SHOP / token missing in Render)"
    found = item_edit._find_in_shopify(sc, sku.upper(), shopify_id)
    if not found:
        return None, "not found in Shopify"
    _, vid, _ = found
    r = sc.shopify_rest("GET", f"variants/{vid}.json?fields=id,sku,inventory_quantity")
    if not r.ok:
        return None, f"Shopify answered HTTP {r.status_code}"
    return int((r.json().get("variant") or {}).get("inventory_quantity") or 0), None


# ============================================================ SECTION 5: create
@bundle_bp.post("/bundle/create")
def bundle_create():
    """Body: {items: [{fi, qty}], title, discount_type: 'amount'|'percent', discount_value}.
    Prices / SKUs / names come from OUR database (not from the page). Every product must have stock in Shopify
    (>= its quantity), else nothing is saved and the answer lists the products that block it."""
    if not is_admin():
        return jsonify({"ok": False, "error": "admins only"}), 403
    body = request.get_json(silent=True) or {}
    lines = body.get("items") or []
    # ---- check what was sent
    picked, seen = [], set()
    for it in lines:
        fi = str((it or {}).get("fi") or "").strip()
        try:
            qty = int((it or {}).get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not fi:
            continue
        if fi in seen:
            return jsonify({"ok": False, "error": "The same product is picked twice - use its quantity instead"}), 400
        if not 1 <= qty <= MAX_QTY:
            return jsonify({"ok": False, "error": f"Quantity must be 1 - {MAX_QTY}"}), 400
        seen.add(fi)
        picked.append((fi, qty))
    if not MIN_ITEMS <= len(picked) <= MAX_ITEMS:
        return jsonify({"ok": False, "error": f"Pick {MIN_ITEMS} to {MAX_ITEMS} products"}), 400
    dtype = body.get("discount_type") if body.get("discount_type") in ("amount", "percent") else "amount"
    try:
        dval = money(body.get("discount_value") or 0)
    except Exception:
        return jsonify({"ok": False, "error": "Discount must be a number"}), 400
    if dval < 0 or (dtype == "percent" and dval > 100):
        return jsonify({"ok": False, "error": "Discount must be 0 - 100 %" if dtype == "percent"
                        else "Discount cannot be below 0"}), 400

    conn = _hooks["connect"]()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(BUNDLES_CREATE)
        rows = {r["fi"]: r for r in product_rows(cur, " WHERE im.fi_record_id = ANY(%s)",
                                                 [[fi for fi, _ in picked]], limit=MAX_ITEMS)}
        conn.commit()
        problems, items = [], []
        for fi, qty in picked:
            r = rows.get(fi)
            if not r:
                problems.append(f"{fi}: not in Item Master")
                continue
            if not r["sku"]:
                problems.append(f"{r['name']}: has no SKU")
            if r["price"] is None or r["price"] <= 0:
                problems.append(f"{r['name']}: has no UAE price")
            items.append({"fi": fi, "sku": r["sku"], "name": r["name"], "qty": qty, "price": r["price"],
                          "image": r["image"], "shopify_id": r["shopify_id"]})
        if problems:
            return jsonify({"ok": False, "error": "Cannot add: " + "; ".join(problems)}), 400
        # ---- 5. every product must have stock in SHOPIFY (>= quantity)
        stock, out = {}, []
        for it in items:
            try:
                avail, why = shopify_stock(it["sku"], it["shopify_id"])
            except Exception as ex:
                avail, why = None, f"Shopify error: {str(ex)[:120]}"
            stock[it["sku"]] = avail
            if avail is None:
                out.append(f"{it['name']} ({it['sku']}): {why}")
            elif avail < it["qty"]:
                out.append(f"{it['name']} ({it['sku']}): only {avail} in stock in Shopify, bundle needs {it['qty']}")
        log(f"[BUNDLE] {g.get('user')} Add: Shopify stock {stock}")
        if out:
            log(f"[BUNDLE] NOT added - out of stock: {out}")
            return jsonify({"ok": False, "error": "Not added - out of stock in Shopify: " + "; ".join(out),
                            "stock": stock}), 409
        # ---- price, title, SKU
        total = sum(money(it["price"]) * it["qty"] for it in items)
        disc = money(total * dval / 100) if dtype == "percent" else dval
        if disc >= total:
            return jsonify({"ok": False, "error": f"Discount (AED {disc}) must be smaller than the total (AED {total})"}), 400
        default_title = " + ".join(it["name"] for it in items)
        title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:300] or default_title
        sku = SKU_JOIN.join(it["sku"].upper() for it in items)
        for it in items:
            it.pop("shopify_id", None)
        cur.execute("""INSERT INTO bundles (title, sku, items, total_price, discount_type, discount_value, discount_aed,
                                            bundle_price, stock_check, created_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (title, sku, json.dumps(items), total, dtype, dval, disc, total - disc, json.dumps(stock),
                     g.get("user")))
        bid = cur.fetchone()["id"]
        conn.commit()
    except Exception as ex:
        conn.rollback()
        log(f"[BUNDLE] ERROR {ex}")
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    finally:
        conn.close()
    log(f"[BUNDLE] Added #{bid} '{title}' SKU {sku}: total {total} - {disc} = {total - disc}")
    return jsonify({"ok": True, "id": bid, "sku": sku, "title": title, "bundle_price": float(total - disc)})


# ============================================================ SECTION 6: pages
PAGE_HEAD = """<!doctype html><html><head><title>@@TITLE@@ - Bundle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e;--yellow:#f2d675}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 {{ nav_css|safe }}
 .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-top:14px}
 .card h2{font-size:20px;margin:0 0 4px}
 .sub{color:var(--muted);font-size:14px;margin:0 0 14px}
 input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
              font-size:15px;font-family:inherit}
 .btn{background:var(--yellow);color:#1c1b19;border:0;border-radius:10px;padding:11px 20px;font-weight:700;font-size:15px;cursor:pointer}
 .btn:disabled{opacity:.5;cursor:default}
 .ghost{background:none;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 14px;cursor:pointer;font-size:14px}
 .thumb{width:46px;height:46px;border-radius:8px;object-fit:cover;background:#111;flex:0 0 46px;display:flex;align-items:center;
        justify-content:center;color:var(--muted);font-size:20px}
 .muted{color:var(--muted)} .ok{color:#8fd19e} .err{color:#e0806c}
</style>"""

FORM_HTML = PAGE_HEAD.replace("@@TITLE@@", "Create bundle") + r"""
<style>
 .lines{display:flex;flex-direction:column;gap:10px}
 .line{display:flex;gap:10px;align-items:center;border:1px solid var(--line);border-radius:12px;padding:10px;position:relative}
 .line .num{width:24px;height:24px;border-radius:50%;background:var(--line);display:flex;align-items:center;justify-content:center;
            font-size:13px;flex:0 0 24px}
 .pick{flex:1;position:relative}
 .pick input{width:100%}
 .results{position:absolute;left:0;right:0;top:46px;z-index:20;background:#2d2b26;border:1px solid var(--line);border-radius:12px;
          max-height:320px;overflow:auto;box-shadow:0 12px 30px rgba(0,0,0,.5)}
 .res{display:flex;gap:10px;align-items:center;padding:8px 10px;cursor:pointer;border-bottom:1px solid var(--line)}
 .res:hover,.res.sel{background:#3a3833}
 .res .t{flex:1;min-width:0} .res .t b{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .res small{color:var(--muted)}
 .chosen{flex:1;display:flex;gap:10px;align-items:center;min-width:0}
 .chosen .t{flex:1;min-width:0} .chosen .t b{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .chosen small{color:var(--muted)}
 .qty{display:flex;align-items:center;gap:6px} .qty input{width:64px;text-align:center}
 .x{background:none;border:0;color:var(--muted);font-size:20px;cursor:pointer;padding:4px 8px}
 .x:hover{color:#e0806c}
 .row2{display:grid;grid-template-columns:1fr;gap:6px;margin-top:6px}
 label.l{font-weight:600;font-size:14px}
 .title-in{width:100%}
 .reset{background:none;border:0;color:var(--gold);cursor:pointer;font-size:13px;padding:0;text-decoration:underline}
 .seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
 .seg button{background:none;border:0;color:var(--text);padding:9px 16px;cursor:pointer;font-weight:600}
 .seg button.on{background:var(--yellow);color:#1c1b19}
 .disc{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .disc input{width:130px}
 .sum{display:grid;grid-template-columns:1fr auto;gap:6px 18px;font-size:15px;max-width:380px}
 .sum .big{font-size:22px;font-weight:700;color:var(--yellow)}
 .skubox{font-family:ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:10px;
         padding:10px 12px;word-break:break-all}
 .msg{margin-top:12px;font-size:14px}
</style></head><body><div class="page">
<div class="topbar"><h1>Create bundle</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
{% if not admin %}
<div class="card"><p class="sub">Only admins can create bundles. <a href="/bundle" style="color:var(--gold)">See the bundles</a></p></div>
{% else %}
<div class="card">
  <h2>1. Products</h2>
  <p class="sub">Pick {{ min_items }} to {{ max_items }} products (search by name, brand, SKU or barcode) and set each quantity.</p>
  <div class="lines" id="lines"></div>
  <div style="margin-top:10px"><button type="button" class="ghost" id="addLine" onclick="addLine()">+ Add product</button>
    <span class="muted" id="lineInfo" style="font-size:13px;margin-left:8px"></span></div>
</div>
<div class="card">
  <h2>2. Bundle title</h2>
  <div class="row2">
    <input class="title-in" id="title" placeholder="Pick products first" oninput="titleTouched=true; refresh()">
    <span><button type="button" class="reset" onclick="titleTouched=false; refresh()">Use the default title</button>
      <span class="muted" style="font-size:13px"> (product names joined with +)</span></span>
  </div>
</div>
<div class="card">
  <h2>3. Bundle discount</h2>
  <div class="disc">
    <div class="seg"><button type="button" id="segAmt" class="on" onclick="setType('amount')">Amount (AED)</button>
      <button type="button" id="segPct" onclick="setType('percent')">Percent (%)</button></div>
    <input type="number" id="dval" min="0" step="0.01" value="0" oninput="refresh()">
  </div>
  <div class="sum" style="margin-top:14px">
    <span class="muted">Products total (UAE price x qty)</span><span id="sTotal">AED 0.00</span>
    <span class="muted">Discount</span><span id="sDisc">- AED 0.00</span>
    <span>Bundle price</span><span class="big" id="sPrice">AED 0.00</span>
  </div>
</div>
<div class="card">
  <h2>4. Auto SKU</h2>
  <div class="skubox" id="sku">-</div>
</div>
<div class="card">
  <h2>5. Add</h2>
  <p class="sub">The bundle is added only when EVERY product has enough stock in Shopify ({{ shop or 'Shopify not set up' }}).</p>
  <button type="button" class="btn" id="addBtn" onclick="addBundle()" disabled>Add bundle</button>
  <div class="msg" id="msg"></div>
</div>
<script>
  // ---- Create bundle form (bundle.py): pick products -> title / discount / SKU follow -> Add (Shopify stock check) ----
  var MIN = {{ min_items }}, MAX = {{ max_items }}, MAXQ = {{ max_qty }}, JOIN = {{ sku_join|tojson }};
  var lines = [], titleTouched = false, dtype = "amount", timer = null;
  function el(t, c, txt) { var e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; }
  function aed(n) { return "AED " + (Math.round(n * 100) / 100).toFixed(2); }
  function thumb(src) {
    if (src) { var i = el("img", "thumb"); i.src = src; i.alt = ""; i.loading = "lazy"; return i; }
    return el("div", "thumb", "□");
  }
  function addLine() { if (lines.length >= MAX) return; lines.push({p: null, qty: 1}); draw(); }
  function removeLine(i) { lines.splice(i, 1); if (lines.length < MIN) lines.push({p: null, qty: 1}); draw(); }
  function draw() {
    var box = document.getElementById("lines"); box.innerHTML = "";
    lines.forEach(function (ln, i) {
      var row = el("div", "line"); row.appendChild(el("span", "num", String(i + 1)));
      if (ln.p) {
        var c = el("div", "chosen"); c.appendChild(thumb(ln.p.image));
        var t = el("div", "t"); t.appendChild(el("b", "", ln.p.name));
        t.appendChild(el("small", "", (ln.p.sku || "no SKU") + " · " + (ln.p.price != null ? aed(ln.p.price) : "no UAE price") +
                         " · Stock " + (ln.p.stock != null ? ln.p.stock : "-")));
        c.appendChild(t); row.appendChild(c);
        var q = el("div", "qty"); q.appendChild(el("span", "muted", "Qty"));
        var qi = el("input"); qi.type = "number"; qi.min = 1; qi.max = MAXQ; qi.value = ln.qty;
        qi.oninput = function () { ln.qty = Math.max(1, Math.min(MAXQ, parseInt(qi.value || "1", 10))); refresh(); };
        q.appendChild(qi); row.appendChild(q);
      } else {
        var pk = el("div", "pick"), inp = el("input"); inp.placeholder = "Search product " + (i + 1) + " (name, brand, SKU, barcode)";
        var res = el("div", "results"); res.hidden = true;
        inp.oninput = function () { search(inp.value, res, i); };
        inp.onkeydown = function (e) { if (e.key === "Escape") res.hidden = true; };
        pk.appendChild(inp); pk.appendChild(res); row.appendChild(pk);
        if (i === lines.length - 1 && !ln.focused) { ln.focused = true; setTimeout(function () { inp.focus(); }, 0); }
      }
      var x = el("button", "x", "×"); x.type = "button"; x.title = "Remove"; x.onclick = function () { removeLine(i); };
      row.appendChild(x); box.appendChild(row);
    });
    document.getElementById("addLine").disabled = lines.length >= MAX;
    document.getElementById("lineInfo").textContent = lines.length + " of " + MAX + " lines";
    refresh();
  }
  function search(q, res, i) {
    clearTimeout(timer);
    if (q.trim().length < 2) { res.hidden = true; return; }
    timer = setTimeout(async function () {
      try {
        var r = await fetch("/bundle/products?q=" + encodeURIComponent(q)), d = await r.json();
        res.innerHTML = ""; res.hidden = false;
        var used = lines.map(function (l) { return l.p && l.p.fi; });
        if (!d.items.length) { res.appendChild(el("div", "res", "No product found")); return; }
        d.items.forEach(function (p) {
          var it = el("div", "res"); it.appendChild(thumb(p.image));
          var t = el("div", "t"); t.appendChild(el("b", "", p.name));
          t.appendChild(el("small", "", (p.sku || "no SKU") + " · " + (p.price != null ? aed(p.price) : "no UAE price") +
                           " · Stock " + (p.stock != null ? p.stock : "-") + (p.on_site ? "" : " · not on fragrantsouq.com")));
          it.appendChild(t);
          if (used.indexOf(p.fi) >= 0) { it.style.opacity = .45; it.title = "Already in the bundle - change its quantity"; }
          else it.onclick = function () {
            lines[i].p = p; console.log("[BUNDLE] picked " + p.sku + " for line " + (i + 1));
            if (lines.length < MAX && lines.every(function (l) { return l.p; }) && lines.length < MIN) lines.push({p: null, qty: 1});
            draw();
          };
          res.appendChild(it);
        });
      } catch (e) { console.log("[BUNDLE] search failed: " + e.message); }
    }, 250);
  }
  function setType(t) {
    dtype = t;
    document.getElementById("segAmt").className = t === "amount" ? "on" : "";
    document.getElementById("segPct").className = t === "percent" ? "on" : "";
    refresh();
  }
  function refresh() {
    var chosen = lines.filter(function (l) { return l.p; });
    var total = chosen.reduce(function (s, l) { return s + (l.p.price || 0) * l.qty; }, 0);
    var v = parseFloat(document.getElementById("dval").value || "0") || 0;
    var disc = dtype === "percent" ? total * v / 100 : v;
    document.getElementById("sTotal").textContent = aed(total);
    document.getElementById("sDisc").textContent = "- " + aed(disc);
    document.getElementById("sPrice").textContent = aed(Math.max(0, total - disc));
    var ti = document.getElementById("title");
    if (!titleTouched) ti.value = chosen.map(function (l) { return l.p.name; }).join(" + ");
    document.getElementById("sku").textContent = chosen.length ? chosen.map(function (l) { return (l.p.sku || "?").toUpperCase(); }).join(JOIN) : "-";
    var bad = chosen.some(function (l) { return !l.p.sku || l.p.price == null; });
    var okDisc = v >= 0 && (dtype === "amount" ? disc < total : v <= 100 && disc < total);
    document.getElementById("addBtn").disabled = chosen.length < MIN || bad || !okDisc || !ti.value.trim();
    var m = document.getElementById("msg");
    if (!m.dataset.keep) m.textContent = chosen.length < MIN ? "Pick at least " + MIN + " products." :
        bad ? "A product has no SKU or no UAE price." : !okDisc ? "The discount must be smaller than the total." : "";
    m.className = "msg muted";
  }
  async function addBundle() {
    var btn = document.getElementById("addBtn"), m = document.getElementById("msg");
    var items = lines.filter(function (l) { return l.p; }).map(function (l) { return {fi: l.p.fi, qty: l.qty}; });
    btn.disabled = true; m.dataset.keep = "1"; m.className = "msg"; m.textContent = "Checking the stock in Shopify ...";
    try {
      var r = await fetch("/bundle/create", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({items: items, title: document.getElementById("title").value,
                              discount_type: dtype, discount_value: document.getElementById("dval").value})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      m.className = "msg ok"; m.textContent = "Bundle added: " + d.title + " (SKU " + d.sku + ", " + aed(d.bundle_price) + "). ";
      var a = el("a", "", "See all bundles"); a.href = "/bundle"; a.style.color = "var(--gold)"; m.appendChild(a);
      console.log("[BUNDLE] added #" + d.id + " " + d.sku);
    } catch (e) {
      m.className = "msg err"; m.textContent = e.message; btn.disabled = false;
      console.log("[BUNDLE] not added: " + e.message);
    }
    setTimeout(function () { delete m.dataset.keep; }, 0);
  }
  document.addEventListener("click", function (e) {           // click outside a result list -> close it
    document.querySelectorAll(".results").forEach(function (r) { if (!r.parentNode.contains(e.target)) r.hidden = true; });
  });
  for (var k = 0; k < MIN; k++) lines.push({p: null, qty: 1, focused: k > 0});
  draw();
</script>
{% endif %}
</div></body></html>
"""

LIST_HTML = PAGE_HEAD.replace("@@TITLE@@", "Bundles") + r"""
<style>
 .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .bar input{flex:1;min-width:220px}
 .bcard .top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
 .bcard h2{font-size:18px}
 .pics{display:flex;gap:6px;margin:10px 0}
 .items{margin:6px 0 0;padding:0;list-style:none;font-size:14px}
 .items li{padding:4px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:10px}
 .price{font-size:22px;font-weight:700;color:var(--yellow);text-align:right}
 .price small{display:block;font-size:13px;font-weight:400;color:var(--muted)}
 .sku{font-family:ui-monospace,Consolas,monospace;font-size:13px;color:var(--muted);word-break:break-all}
</style></head><body><div class="page">
<div class="topbar"><h1>Bundles</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
<form class="bar" method="get" style="margin-top:12px">
  <input type="text" name="q" value="{{ q }}" placeholder="Search bundles by title or SKU">
  <button class="btn" type="submit">Go</button>
  {% if admin %}<a class="ghost" href="/bundle/new" style="text-decoration:none">+ Create bundle</a>{% endif %}
</form>
<p class="muted" style="font-size:14px">{{ rows|length }} bundle{{ '' if rows|length == 1 else 's' }}{% if q %} matching "{{ q }}"{% endif %}.</p>
{% for b in rows %}
<div class="card bcard">
  <div class="top">
    <div style="flex:1;min-width:0">
      <h2>{{ b.title }}</h2>
      <div class="sku">SKU {{ b.sku }}</div>
      <div class="pics">{% for it in b['items'] %}{% if it.image %}<img class="thumb" src="{{ it.image }}" alt="" loading="lazy">{% else %}<div class="thumb">&#9633;</div>{% endif %}{% endfor %}</div>
    </div>
    <div class="price">AED {{ '%.2f' % b.bundle_price }}
      <small>was AED {{ '%.2f' % b.total_price }} · discount {% if b.discount_type == 'percent' %}{{ '%g' % b.discount_value }}% = {% endif %}AED {{ '%.2f' % b.discount_aed }}</small></div>
  </div>
  <ul class="items">
    {% for it in b['items'] %}<li><span>{{ it.qty }} x {{ it.name }} <span class="sku">({{ it.sku }})</span></span>
      <span class="muted">AED {{ '%.2f' % (it.price * it.qty) }}</span></li>{% endfor %}
  </ul>
  <p class="muted" style="font-size:13px;margin:8px 0 0">Added by {{ b.created_by or '-' }} on {{ b.created_at.strftime('%d %b %Y, %H:%M') }}</p>
</div>
{% else %}
<div class="card"><p class="sub" style="margin:0">No bundles yet.{% if admin %} <a href="/bundle/new" style="color:var(--gold)">Create the first one</a>.{% endif %}</p></div>
{% endfor %}
</div></body></html>
"""


def _nav(cur_key):
    """Menu bar + its counts (the page still opens when the counts cannot be read)."""
    import competitors_page                                  # imported here = no import loop
    counts = {}
    try:
        conn = _hooks["connect"]()
        try:
            counts = competitors_page.tab_counts(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
        finally:
            conn.close()
    except Exception as ex:
        log(f"[BUNDLE] menu counts not read: {ex}")
    return competitors_page.render_nav("bundle", cur_key, counts), competitors_page.NAV_CSS


@bundle_bp.get("/bundle/new")
def bundle_new():
    """Create bundle form."""
    import shopify_create as sc
    nav_html, nav_css = _nav("bundle-new")
    log(f"[BUNDLE] {g.get('user')} opened Create bundle form")
    return render_template_string(FORM_HTML, nav_html=nav_html, nav_css=nav_css, admin=is_admin(),
                                  min_items=MIN_ITEMS, max_items=MAX_ITEMS, max_qty=MAX_QTY, sku_join=SKU_JOIN,
                                  shop=sc.SHOP)


@bundle_bp.get("/bundle")
def bundle_list():
    """List of all bundles (newest first), search by title / SKU."""
    q = (request.args.get("q") or "").strip()
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(BUNDLES_CREATE)
        conn.commit()
        if q:
            cur.execute("SELECT * FROM bundles WHERE title ILIKE %s OR sku ILIKE %s ORDER BY id DESC LIMIT 500",
                        (f"%{q}%", f"%{q}%"))
        else:
            cur.execute("SELECT * FROM bundles ORDER BY id DESC LIMIT 500")
        rows = cur.fetchall()
    finally:
        conn.close()
    for b in rows:                                           # numbers for the template
        for k in ("total_price", "discount_value", "discount_aed", "bundle_price"):
            b[k] = float(b[k] or 0)
    nav_html, nav_css = _nav("bundle-list")
    log(f"[BUNDLE] list: {len(rows)} bundles{f' for {q!r}' if q else ''}")
    return render_template_string(LIST_HTML, nav_html=nav_html, nav_css=nav_css, rows=rows, q=q, admin=is_admin())


# ============================================================ SECTION 7: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    try:
        conn = connect()
        try:
            conn.cursor().execute(BUNDLES_CREATE)
            conn.commit()
        finally:
            conn.close()
    except Exception as ex:
        log(f"[BUNDLE] table bundles not checked at start: {ex}")
    log("[BUNDLE] Linked to app.py: menu Bundle -> Bundles (/bundle) + Create bundle form (/bundle/new), table bundles")
