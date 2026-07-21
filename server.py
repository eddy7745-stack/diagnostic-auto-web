#!/usr/bin/env python3
"""Serveur diagnostic automobile — python3 server.py"""

import json
import re
import sqlite3
import time
import os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB_PATH = os.path.join(os.path.dirname(__file__), "codes.db")
PORT = int(os.environ.get("PORT", 8080))

OWNER_EMAIL   = "eddy7745@gmail.com"
MAX_DEVICES   = 2
IA_MONTHLY_CAP = 300  # garde-fou anti-abus par appareil (n'affecte pas un usage normal, même abonné)

def init_purchases_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            payment_ref TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS purchases_email ON purchases(email)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            device_id TEXT NOT NULL,
            activated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(email, device_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS activations_email ON activations(email)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ia_usage (
            device_id TEXT NOT NULL,
            month TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (device_id, month)
        )
    """)
    conn.commit()
    conn.close()


def ia_check_and_increment(device_id):
    """Retourne True si l'appareil a encore du quota ce mois-ci, et incrémente."""
    month = time.strftime("%Y-%m")
    conn = get_db()
    row = conn.execute(
        "SELECT count FROM ia_usage WHERE device_id = ? AND month = ?",
        (device_id, month)
    ).fetchone()
    count = row["count"] if row else 0
    if count >= IA_MONTHLY_CAP:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO ia_usage (device_id, month, count) VALUES (?, ?, 1) "
        "ON CONFLICT(device_id, month) DO UPDATE SET count = count + 1",
        (device_id, month)
    )
    conn.commit()
    conn.close()
    return True


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence request logs

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, mime):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Répond comme GET (statut + headers) mais sans corps — requis par
        # certains crawlers (AdMob app-ads.txt, Googlebot) qui sondent en HEAD.
        class _NullWriter:
            def write(self, *_): pass
            def flush(self): pass

        real_end_headers = self.end_headers

        def end_headers_then_mute():
            real_end_headers()
            self.wfile = _NullWriter()

        self.end_headers = end_headers_then_mute
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_file(os.path.join(os.path.dirname(__file__), "index.html"), "text/html; charset=utf-8")
            return

        if path == "/search":
            code  = qs.get("code",  [""])[0].strip().upper()
            brand = qs.get("brand", ["ALL"])[0].strip().upper()
            lang  = qs.get("lang",  ["fr"])[0].strip().lower()
            limit = min(int(qs.get("limit", [50])[0]), 200)

            if not code:
                self.send_json({"results": [], "total": 0})
                return

            # Title column: french / english with fallbacks
            if lang == "fr":
                title_col = "CASE WHEN title_fr IS NOT NULL AND title_fr != '' AND title_fr != title THEN title_fr ELSE title END"
            else:
                title_col = "CASE WHEN title_en IS NOT NULL AND title_en != '' THEN title_en ELSE title END"

            conn = get_db()

            use_brand_filter = brand != "ALL"

            # Exact match
            if use_brand_filter:
                rows = conn.execute(
                    f"SELECT *, {title_col} AS display_title FROM codes WHERE code = ? AND (brand = ? OR brand = 'ALL') ORDER BY brand = 'ALL' ASC LIMIT ?",
                    (code, brand, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT *, {title_col} AS display_title FROM codes WHERE code = ? ORDER BY brand = 'ALL' ASC LIMIT ?",
                    (code, limit)
                ).fetchall()

            # Prefix match
            if not rows:
                if use_brand_filter:
                    rows = conn.execute(
                        f"SELECT *, {title_col} AS display_title FROM codes WHERE code LIKE ? AND (brand = ? OR brand = 'ALL') ORDER BY code ASC LIMIT ?",
                        (code + "%", brand, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT *, {title_col} AS display_title FROM codes WHERE code LIKE ? ORDER BY code ASC LIMIT ?",
                        (code + "%", limit)
                    ).fetchall()

            # Full-text search in title
            if not rows:
                if use_brand_filter:
                    rows = conn.execute(
                        f"SELECT *, {title_col} AS display_title FROM codes WHERE {title_col} LIKE ? AND (brand = ? OR brand = 'ALL') ORDER BY code ASC LIMIT ?",
                        ("%" + code + "%", brand, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT *, {title_col} AS display_title FROM codes WHERE {title_col} LIKE ? ORDER BY code ASC LIMIT ?",
                        ("%" + code + "%", limit)
                    ).fetchall()

            conn.close()

            def col(r, name, default=""):
                return r[name] if name in r.keys() else default

            results = []
            for r in rows:
                if lang == "fr":
                    causes_out = json.loads(col(r, "causes", "[]"))
                    steps_out  = json.loads(col(r, "steps",  "[]"))
                else:
                    causes_raw = col(r, "causes_en", "[]")
                    steps_raw  = col(r, "steps_en",  "[]")
                    causes_out = json.loads(causes_raw) if causes_raw else json.loads(col(r, "causes", "[]"))
                    steps_out  = json.loads(steps_raw)  if steps_raw  else json.loads(col(r, "steps",  "[]"))

                results.append({
                    "code":     r["code"],
                    "brand":    r["brand"],
                    "severity": r["severity"],
                    "title":    r["display_title"],
                    "title_fr": col(r, "title_fr"),
                    "title_en": col(r, "title_en"),
                    "causes":   causes_out,
                    "steps":    steps_out,
                })

            self.send_json({"results": results, "total": len(results)})
            return

        # ── Pages SEO par code défaut : /code/P0300 ─────────────────────────
        if path.startswith("/code/"):
            code = path[6:].strip().upper().replace(".HTML", "")
            if not re.fullmatch(r"[PCBU][0-3][0-9A-F]{3}", code):
                self.send_response(404); self.end_headers(); return
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM codes WHERE code = ? ORDER BY brand = 'ALL' DESC LIMIT 6",
                (code,)).fetchall()
            conn.close()
            if not rows:
                self.send_response(404); self.end_headers(); return
            r = rows[0]
            title  = (r["title_fr"] or r["title"]) if "title_fr" in r.keys() else r["title"]
            causes = json.loads(r["causes"] or "[]")
            steps  = json.loads(r["steps"] or "[]")
            sev    = {"low": "Faible", "medium": "Moyenne", "high": "Élevée", "critical": "Critique"}.get(r["severity"], "Moyenne")
            sev_color = {"low": "#22c55e", "medium": "#f97316", "high": "#ef4444", "critical": "#ef4444"}.get(r["severity"], "#f97316")
            variants = [v for v in rows[1:] if v["brand"] != "ALL"]
            esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            causes_html = "".join(f"<li>{esc(c)}</li>" for c in causes) or "<li>Consultez l'application pour le détail.</li>"
            steps_html  = "".join(f"<li>{esc(st)}</li>" for st in steps) or "<li>Scannez votre véhicule avec l'application.</li>"
            variants_html = ""
            if variants:
                items = "".join(
                    f"<li><strong>{esc(v['brand'])}</strong> : {esc((v['title_fr'] or v['title']) if 'title_fr' in v.keys() else v['title'])}</li>"
                    for v in variants)
                variants_html = f"<h2>Signification spécifique par constructeur</h2><ul>{items}</ul>"
            meta_desc = esc(f"Code défaut {code} : {title}. Causes possibles, gravité et étapes de réparation. Diagnostiquez gratuitement avec l'application Diagnostic Auto.")[:250]
            body = f"""<!DOCTYPE html>
<html lang="fr"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code défaut {code} : {esc(title)} — signification et réparation</title>
<meta name="description" content="{meta_desc}">
<link rel="canonical" href="https://diagnostic-auto-web.onrender.com/code/{code}">
<script type="application/ld+json">{json.dumps({
    "@context": "https://schema.org", "@type": "FAQPage",
    "mainEntity": [{
        "@type": "Question", "name": f"Que signifie le code défaut {code} ?",
        "acceptedAnswer": {"@type": "Answer", "text": f"{code} : {title}. Gravité : {sev}."}
    }, {
        "@type": "Question", "name": f"Quelles sont les causes du code {code} ?",
        "acceptedAnswer": {"@type": "Answer", "text": " ; ".join(causes) or "Voir l'application Diagnostic Auto."}
    }]}, ensure_ascii=False)}</script>
<style>
body{{margin:0;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e2e8f0;line-height:1.6}}
.wrap{{max-width:760px;margin:0 auto;padding:24px 20px 60px}}
a{{color:#60a5fa}} h1{{font-size:1.6rem;line-height:1.3}} h2{{color:#60a5fa;font-size:1.15rem;margin-top:28px}}
.code{{font-family:Menlo,monospace;color:#60a5fa}}
.sev{{display:inline-block;background:{sev_color}22;color:{sev_color};border-radius:8px;padding:3px 12px;font-weight:700;font-size:.9rem}}
ul{{padding-left:22px}} li{{margin-bottom:6px}}
.cta{{background:#1a1d27;border:1px solid #2d3452;border-radius:14px;padding:22px;margin-top:34px;text-align:center}}
.play{{display:inline-block;background:#3b82f6;color:#fff;font-weight:700;border-radius:10px;padding:13px 26px;text-decoration:none;margin-top:10px}}
.top{{color:#94a3b8;font-size:.9rem}}
</style></head><body><div class="wrap">
<p class="top"><a href="/">← Diagnostic Auto</a></p>
<h1>Code défaut <span class="code">{code}</span><br>{esc(title)}</h1>
<p>Gravité : <span class="sev">{sev}</span></p>
<h2>Causes possibles</h2><ul>{causes_html}</ul>
<h2>Étapes de diagnostic</h2><ol>{steps_html}</ol>
{variants_html}
<div class="cta">
  <strong>🔧 Diagnostiquez votre voiture vous-même</strong>
  <p style="color:#94a3b8;font-size:.95rem">Scannez vos codes défauts en Bluetooth (ELM327), obtenez des conseils IA personnalisés et suivez vos véhicules — gratuitement.</p>
  <a class="play" href="https://play.google.com/store/apps/details?id=com.diagnosticauto.app">📲 Télécharger sur Google Play</a>
</div>
</div></body></html>"""
            body = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/sitemap-codes.xml":
            body = SITEMAP_CODES_CACHE
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/download/apk":
            apk_path = os.path.join(os.path.dirname(__file__), "DiagnosticAuto.apk")
            if not os.path.exists(apk_path):
                self.send_response(404)
                self.end_headers()
                return
            with open(apk_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Disposition", "attachment; filename=DiagnosticAuto.apk")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/google32a84dd2657db19d.html":
            body = b"google-site-verification: google32a84dd2657db19d.html"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/app-ads.txt":
            body = b"google.com, pub-3003334283248427, DIRECT, f08c47fec0942fa0\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/robots.txt":
            body = (
                "User-agent: *\n"
                "Allow: /\n"
                "Disallow: /stats\n\n"
                "Sitemap: https://diagnostic-auto-web.onrender.com/sitemap.xml\n"
                "Sitemap: https://diagnostic-auto-web.onrender.com/sitemap-codes.xml\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/sitemap.xml":
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
                '        xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
                '  <url>\n'
                '    <loc>https://diagnostic-auto-web.onrender.com/</loc>\n'
                '    <changefreq>monthly</changefreq>\n'
                '    <priority>1.0</priority>\n'
                '    <xhtml:link rel="alternate" hreflang="fr" href="https://diagnostic-auto-web.onrender.com/"/>\n'
                '    <xhtml:link rel="alternate" hreflang="en" href="https://diagnostic-auto-web.onrender.com/?lang=en"/>\n'
                '  </url>\n'
                '  <url>\n'
                '    <loc>https://diagnostic-auto-web.onrender.com/privacy</loc>\n'
                '    <changefreq>yearly</changefreq>\n'
                '    <priority>0.3</priority>\n'
                '  </url>\n'
                + ''.join(
                    f'  <url>\n'
                    f'    <loc>https://diagnostic-auto-web.onrender.com/?code={c}</loc>\n'
                    f'    <changefreq>yearly</changefreq>\n'
                    f'    <priority>0.8</priority>\n'
                    f'  </url>\n'
                    for c in [
                        'P0300','P0171','P0420','P0401','P0101','P0130',
                        'P0505','P0715','P1340','B1000','U0100','P0016',
                        'P0340','P0442','P0456','P0128','P0335','P0102',
                        'P0113','P0304','P0302','P0303','P0172','P0301',
                        'P0191','P0200','P0400','P0410','P0430','P0440',
                    ]
                )
                + '</urlset>\n'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/privacy", "/privacy.html"):
            self.send_file(os.path.join(os.path.dirname(__file__), "privacy.html"), "text/html; charset=utf-8")
            return

        if path == "/reset-devices":
            email     = qs.get("email",     [""])[0].strip().lower()
            device_id = qs.get("device_id", [""])[0].strip()
            if not email or not device_id:
                self.send_json({"ok": False, "reason": "missing_params"})
                return
            conn = get_db()
            # Vérifier que l'email a bien un achat
            purchase = conn.execute(
                "SELECT id FROM purchases WHERE email = ?", (email,)
            ).fetchone()
            if not purchase:
                conn.close()
                self.send_json({"ok": False, "reason": "not_purchased"})
                return
            # Supprimer toutes les activations existantes
            conn.execute("DELETE FROM activations WHERE email = ?", (email,))
            # Enregistrer le nouvel appareil
            conn.execute(
                "INSERT OR IGNORE INTO activations (email, device_id) VALUES (?,?)",
                (email, device_id)
            )
            conn.commit()
            conn.close()
            self.send_json({"ok": True})
            return

        if path == "/obd-location":
            brand = qs.get("brand", [""])[0].strip().upper()
            lang  = qs.get("lang",  ["fr"])[0].strip().lower()
            if not brand:
                self.send_json({"results": []})
                return
            conn = get_db()
            # Match flexible : CITROEN, CITROËN, CITROEN/DS...
            rows = conn.execute(
                "SELECT model, location_fr, location_en, notes FROM obd_locations WHERE UPPER(brand) LIKE ? ORDER BY model",
                (f"%{brand}%",)
            ).fetchall()
            # Si pas de résultat exact, chercher par mot-clé
            if not rows:
                rows = conn.execute(
                    "SELECT model, location_fr, location_en, notes FROM obd_locations WHERE UPPER(brand) LIKE ? ORDER BY model",
                    (f"%{brand.split('/')[0].strip()}%",)
                ).fetchall()
            conn.close()
            loc_key = "location_fr" if lang == "fr" else "location_en"
            results = [
                {
                    "model": r[0],
                    "location": r[1] if lang == "fr" else r[2],
                    "notes": r[3]
                }
                for r in rows
            ]
            self.send_json({"results": results})
            return

        if path == "/check-purchase":
            email     = qs.get("email",     [""])[0].strip().lower()
            device_id = qs.get("device_id", [""])[0].strip()
            if not email:
                self.send_json({"premium": False, "reason": "missing_email"})
                return

            conn = get_db()
            # Vérifier que l'email a un achat
            purchase = conn.execute(
                "SELECT id FROM purchases WHERE email = ?", (email,)
            ).fetchone()

            if not purchase:
                conn.close()
                self.send_json({"premium": False, "reason": "not_purchased"})
                return

            # Propriétaire : aucune limite
            if email == OWNER_EMAIL:
                if device_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO activations (email, device_id) VALUES (?,?)",
                        (email, device_id)
                    )
                    conn.commit()
                conn.close()
                self.send_json({"premium": True})
                return

            # Vérifier si cet appareil est déjà enregistré
            if device_id:
                already = conn.execute(
                    "SELECT id FROM activations WHERE email=? AND device_id=?",
                    (email, device_id)
                ).fetchone()
                if already:
                    conn.close()
                    self.send_json({"premium": True})
                    return

            # Compter les appareils enregistrés
            count = conn.execute(
                "SELECT COUNT(*) FROM activations WHERE email=?", (email,)
            ).fetchone()[0]

            if count >= MAX_DEVICES:
                conn.close()
                self.send_json({"premium": False, "reason": "device_limit"})
                return

            # Enregistrer ce nouvel appareil
            if device_id:
                conn.execute(
                    "INSERT OR IGNORE INTO activations (email, device_id) VALUES (?,?)",
                    (email, device_id)
                )
                conn.commit()
            conn.close()
            self.send_json({"premium": True})
            return

        if path == "/stats":
            conn = get_db()
            total = conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
            by_brand = conn.execute(
                "SELECT brand, COUNT(*) as n FROM codes GROUP BY brand ORDER BY n DESC"
            ).fetchall()
            conn.close()
            self.send_json({
                "total": total,
                "by_brand": [{"brand": r["brand"], "count": r["n"]} for r in by_brand]
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/diagnostic-ia":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except Exception:
                self.send_json({"error": "invalid_json"}, 400)
                return

            code     = data.get("code", "").strip().upper()
            title    = data.get("title", "")
            causes   = data.get("causes", [])
            steps    = data.get("steps", [])
            brand    = data.get("brand", "")
            model    = data.get("model", "")
            year     = data.get("year", "")
            mileage  = data.get("mileage", "")
            symptoms = data.get("symptoms", "")
            lang     = data.get("lang", "fr")

            if not code:
                self.send_json({"error": "missing_code"}, 400)
                return

            device_id = data.get("device_id", "").strip()
            if not device_id:
                self.send_json({"error": "missing_device_id"}, 400)
                return
            if not ia_check_and_increment(device_id):
                self.send_json({"error": "rate_limited"}, 429)
                return

            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                self.send_json({"error": "api_key_not_configured"}, 500)
                return

            vehicle_info = ""
            if brand or model or year or mileage:
                parts = [x for x in [brand, model, year, (f"{mileage} km" if mileage else "")] if x]
                vehicle_info = "Véhicule : " + " — ".join(parts) + "\n" if lang == "fr" else "Vehicle: " + " — ".join(parts) + "\n"

            symptoms_info = (f"Symptômes décrits : {symptoms}\n" if symptoms else "") if lang == "fr" else (f"Described symptoms: {symptoms}\n" if symptoms else "")

            if lang == "fr":
                prompt = f"""Tu es un expert en mécanique automobile avec 20 ans d'expérience. Analyse ce code défaut OBD et donne un conseil professionnel, clair et pratique.

Code défaut : {code}
Description : {title}
{vehicle_info}{symptoms_info}
Causes connues : {', '.join(causes) if causes else 'Non spécifiées'}
Étapes de diagnostic : {', '.join(steps) if steps else 'Non spécifiées'}

Réponds en français avec :
1. **Diagnostic** : explication simple de ce qui se passe
2. **Urgence** : 🔴 Critique / 🟠 Modéré / 🟢 Mineur — et pourquoi
3. **À faire** : les 2-3 actions concrètes à entreprendre dans l'ordre
4. **Coût estimé** : fourchette de prix réaliste pour la réparation en France
5. **Conseil** : astuce pro ou mise en garde importante

Sois direct, pratique et professionnel. Maximum 250 mots."""
            else:
                prompt = f"""You are an automotive expert with 20 years of experience. Analyze this OBD fault code and give professional, clear, practical advice.

Fault code: {code}
Description: {title}
{vehicle_info}{symptoms_info}
Known causes: {', '.join(causes) if causes else 'Not specified'}
Diagnostic steps: {', '.join(steps) if steps else 'Not specified'}

Reply in English with:
1. **Diagnosis**: simple explanation of what's happening
2. **Urgency**: 🔴 Critical / 🟠 Moderate / 🟢 Minor — and why
3. **Action**: the 2-3 concrete steps to take in order
4. **Estimated cost**: realistic price range for the repair
5. **Pro tip**: expert advice or important warning

Be direct, practical and professional. Maximum 250 words."""

            try:
                import urllib.request
                payload = json.dumps({
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}]
                }).encode()
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=payload,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                advice = result["content"][0]["text"]
                self.send_json({"advice": advice, "code": code})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        self.send_response(404)
        self.end_headers()


def build_sitemap_cache():
    conn = get_db()
    codes_list = [row[0] for row in conn.execute("SELECT DISTINCT code FROM codes ORDER BY code")
                  if re.fullmatch(r"[PCBU][0-3][0-9A-F]{3}", row[0])]
    conn.close()
    urls = "".join(
        f"<url><loc>https://diagnostic-auto-web.onrender.com/code/{c}</loc><changefreq>yearly</changefreq><priority>0.6</priority></url>"
        for c in codes_list)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>').encode()

SITEMAP_CODES_CACHE = b""  # initialisé au démarrage

def self_ping():
    import urllib.request
    import threading
    def ping():
        while True:
            try:
                urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=5)
            except Exception:
                pass
            time.sleep(600)
    t = threading.Thread(target=ping, daemon=True)
    t.start()

if __name__ == "__main__":
    print(f"Diagnostic Auto — port {PORT}")
    init_purchases_db()
    SITEMAP_CODES_CACHE = build_sitemap_cache()
    print(f"Sitemap codes chargé : {len(SITEMAP_CODES_CACHE)} octets")
    self_ping()
    # Propriétaire de l'application — accès premium permanent
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO purchases (email, payment_ref) VALUES (?, ?)",
            ("eddy7745@gmail.com", "owner")
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
