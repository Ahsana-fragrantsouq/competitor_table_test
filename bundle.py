"""
Bundle menu (2026-11-12, user: "add option 'Bundle' after Item Master (drop down menu), in that next option is
'Create bundle form'. Content can add later")
==============================================================================================================
Menu bar: ... Item Master ▾ | Bundle ▾  -> "Create bundle form" (/bundle/new)
The page is EMPTY for now (only the title + a note) - the form will be added later.

Where things are (change them here):
  SECTION 1  menu        -> BUNDLE_OPTIONS (key, link, text) - used by competitors_page.render_nav
  SECTION 2  page        -> BUNDLE_HTML
  SECTION 3  route       -> GET /bundle/new
  SECTION 4  hooks       -> init(): database connection from app.py (only for the menu counts)
"""

from flask import Blueprint, render_template_string, g

bundle_bp = Blueprint("bundle", __name__)


def log(*a):
    """Print to the Render log (lines start with [BUNDLE])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: menu
# Bundle dropdown: (key = cur_key of that page, link, text). New pages = one more line here.
BUNDLE_OPTIONS = [
    ("bundle-new", "/bundle/new", "Create bundle form"),
]


# ============================================================ SECTION 2: page
BUNDLE_HTML = """<!doctype html><html><head><title>Create bundle - Bundle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 {{ nav_css|safe }}
 .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-top:14px}
 .card h2{font-size:24px;margin:0 0 4px}
 .sub{color:var(--muted);font-size:14px;margin:0}
</style></head><body><div class="page">
<div class="topbar"><h1>Bundle</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
<div class="card">
  <h2>Create bundle form</h2>
  <p class="sub">Coming soon - the bundle form will be added here.</p>
</div>
</div></body></html>
"""


# ============================================================ SECTION 3: route
_hooks = {}


@bundle_bp.get("/bundle/new")
def bundle_new():
    """Create bundle form page (empty for now)."""
    import competitors_page                                  # menu bar (imported here = no import loop)
    import psycopg2.extras
    counts = {}
    try:
        conn = _hooks["connect"]()
        try:
            counts = competitors_page.tab_counts(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
        finally:
            conn.close()
    except Exception as ex:                                  # the page still opens without the menu counts
        log(f"[BUNDLE] menu counts not read: {ex}")
    log(f"[BUNDLE] {g.get('user')} opened Create bundle form")
    return render_template_string(BUNDLE_HTML, nav_html=competitors_page.render_nav("bundle", "bundle-new", counts),
                                  nav_css=competitors_page.NAV_CSS)


# ============================================================ SECTION 4: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log("[BUNDLE] Linked to app.py: menu Bundle -> Create bundle form (/bundle/new, empty for now)")
