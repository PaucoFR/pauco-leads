# -*- coding: utf-8 -*-
"""
Routes pour les allergènes + QR code.
"""

from datetime import date

from flask import render_template, request, jsonify, make_response, send_file
from flask_login import login_required, current_user

from modules.config import app
from modules.db import get_db
from modules.helpers import _rid
from modules import airtable_client as at


_ALLERGENE_COLS = [
    "gluten", "crustaces", "oeufs", "poisson", "arachides", "soja", "lait",
    "fruits_a_coque", "celeri", "moutarde", "sesame", "sulfites", "lupin", "mollusques",
]
_ALLERGENE_AT_FIELDS = [
    "Gluten", "Crustaces", "Oeufs", "Poisson", "Arachides", "Soja", "Lait",
    "Fruits_a_coque", "Celeri", "Moutarde", "Sesame", "Sulfites", "Lupin", "Mollusques",
]


@app.route("/gestion/allergenes")
def allergenes_page():
    db = get_db()
    rid = current_user.restaurant_id if current_user.is_authenticated else ""

    # ── Source de vérité = Airtable Fiches Food (table "fiches" type=Food)
    # Les plats sont auto-importés depuis Airtable. Le local SQLite est utilisé en fallback
    # pour les plats hors-carte ajoutés manuellement via "+ Ajouter un plat".
    fiche_cats = {}  # nom_plat (lower) -> categorie
    fiches_source = []  # liste de (nom, categorie) — venant d'Airtable en priorité
    if rid:
        try:
            # Bypass cache : on veut voir IMMÉDIATEMENT les fiches qui viennent
            # d'être créées depuis l'onglet Fiches Food.
            at.invalidate_cache("fiches", rid)
            airtable_fiches = at.get_fiches(rid, "Food")
            for r in airtable_fiches:
                if (r.get("Statut") or "actif") != "actif":
                    continue
                nom = (r.get("Nom") or "").strip()
                cat = (r.get("Categorie") or "").strip() or "Plats"
                if not nom:
                    continue
                fiche_cats[nom.lower()] = cat
                fiches_source.append((nom, cat))
        except Exception as e:
            print(f"[ALLERGENES] Airtable fiches fetch error: {e}")
    # Fallback : compléter depuis le local SQLite si une fiche n'est pas dans Airtable
    for row in db.execute("SELECT nom, categorie FROM fiches_techniques WHERE restaurant_id=? AND statut='actif'", (_rid(),)).fetchall():
        key = row["nom"].strip().lower()
        if key not in fiche_cats:
            fiche_cats[key] = row["categorie"]
            fiches_source.append((row["nom"].strip(), row["categorie"]))

    # ── Auto-import new plats (case-insensitive dedup) ──
    existing_rows = db.execute("SELECT id, nom_plat, categorie FROM allergenes WHERE restaurant_id=?", (_rid(),)).fetchall()
    existing_lower = {}  # lower_name -> row dict
    for r in existing_rows:
        key = r["nom_plat"].strip().lower()
        if key in existing_lower:
            # Duplicate found — keep the one with more allergens checked
            old = existing_lower[key]
            alg_cols = ['gluten','crustaces','oeufs','poisson','arachides','soja','lait',
                        'fruits_a_coque','celeri','moutarde','sesame','sulfites','lupin','mollusques']
            old_full = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), old["id"],)).fetchone()
            cur_full = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), r["id"],)).fetchone()
            old_count = sum(1 for c in alg_cols if old_full[c])
            cur_count = sum(1 for c in alg_cols if cur_full[c])
            if cur_count > old_count:
                # Current has more allergens — delete old, keep current
                db.execute("DELETE FROM allergenes WHERE id=?", (old["id"],))
                existing_lower[key] = dict(r)
            else:
                # Old has more (or equal) — delete current duplicate
                db.execute("DELETE FROM allergenes WHERE id=?", (r["id"],))
        else:
            existing_lower[key] = dict(r)

    new_plats = []
    for nom_f, cat_f in fiches_source:
        key = nom_f.strip().lower()
        if key not in existing_lower:
            new_plats.append((nom_f.strip(), cat_f))
            existing_lower[key] = True  # mark as seen

    for nom, cat in new_plats:
        cur = db.execute("INSERT INTO allergenes (nom_plat, categorie) VALUES (?, ?)", (nom, cat))
        if rid:
            try:
                # Évite les doublons : si un record Airtable existe déjà pour (rid, nom), on le réutilise
                nom_esc = nom.replace("'", "\\'")
                existing_at = at.find_first_nocache("allergenes",
                    f"AND({{Restaurant_ID}}='{rid}',LOWER({{Nom_plat}})='{nom_esc.lower()}')")
                if existing_at:
                    db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (existing_at["id"], cur.lastrowid))
                else:
                    rec = at.create_allergene(rid, nom, cat)
                    db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (rec.get("id", ""), cur.lastrowid))
            except Exception as e:
                print(f"[ALLERGENES] Auto-import Airtable error for '{nom}': {e}")

    # ── Update categories from fiches_techniques (source of truth) ──
    for r in db.execute("SELECT id, nom_plat, categorie FROM allergenes WHERE restaurant_id=?", (_rid(),)).fetchall():
        fiche_cat = fiche_cats.get(r["nom_plat"].strip().lower())
        if fiche_cat and fiche_cat != r["categorie"]:
            db.execute("UPDATE allergenes SET categorie=? WHERE id=?", (fiche_cat, r["id"]))

    db.commit()

    # Backfill missing airtable_id for rows that failed initial create
    if rid:
        orphans = db.execute("SELECT id, nom_plat, categorie FROM allergenes WHERE restaurant_id=? AND airtable_id='' OR airtable_id IS NULL", (_rid(),)).fetchall()
        for orph in orphans:
            try:
                nom_esc = (orph["nom_plat"] or "").replace("'", "\\'")
                existing_at = at.find_first_nocache("allergenes",
                    f"AND({{Restaurant_ID}}='{rid}',LOWER({{Nom_plat}})='{nom_esc.lower()}')")
                if existing_at:
                    db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (existing_at["id"], orph["id"]))
                else:
                    rec = at.create_allergene(rid, orph["nom_plat"], orph["categorie"])
                    db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (rec.get("id", ""), orph["id"]))
            except Exception as e:
                print(f"[ALLERGENES] Backfill error for '{orph['nom_plat']}': {e}")
        if orphans:
            db.commit()

    _EXCLUDED_CATS = {"Classiques", "Creations", "Créations", "Sans Alcool", "Boisson", "Cocktail", "Bar"}
    # Normalize category names (accent variants → canonical)
    _CAT_NORMALIZE = {"Entrées": "Entrees", "Entrée": "Entrees", "entrées": "Entrees", "entrees": "Entrees",
                      "Plat": "Plats", "plats": "Plats", "Dessert": "Desserts", "desserts": "Desserts"}
    # Fixed order: Entrees → Plats → Desserts → rest
    _CAT_FIXED_ORDER = {"Entrees": 0, "Entrées": 0, "Plats": 1, "Desserts": 2}
    cat_order = {r["nom"]: r["ordre"] for r in db.execute("SELECT nom, ordre FROM categories_fiches WHERE restaurant_id=? AND type='food' ORDER BY ordre, id", (_rid(),)).fetchall()}
    rows = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? ORDER BY nom_plat", (_rid(),)).fetchall()
    plats = []
    for r in rows:
        p = dict(r)
        # Normalize category
        p["categorie"] = _CAT_NORMALIZE.get(p["categorie"], p["categorie"])
        if p["categorie"] in _EXCLUDED_CATS:
            continue
        plats.append(p)
    plats.sort(key=lambda p: (_CAT_FIXED_ORDER.get(p["categorie"], cat_order.get(p["categorie"], 999)), p["nom_plat"].lower()))
    return render_template("base.html", page="allergenes", allergenes=plats)


@app.route("/gestion/allergenes/add", methods=["POST"])
def allergenes_add():
    db = get_db()
    data = request.get_json()
    nom = data.get("nom_plat", "").strip()
    cat = data.get("categorie", "Plat")
    if not nom:
        return jsonify({"ok": False, "error": "Nom requis"}), 400
    rid = _rid()
    cur = db.execute("INSERT INTO allergenes (restaurant_id, nom_plat, categorie) VALUES (?, ?, ?)", (rid, nom, cat))
    db.commit()
    plat_id = cur.lastrowid
    at_id = ""
    if rid:
        try:
            rec = at.create_allergene(rid, nom, cat)
            at_id = rec.get("id", "")
            db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (at_id, plat_id))
            db.commit()
        except Exception:
            pass
    row = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), plat_id,)).fetchone()
    return jsonify({"ok": True, "plat": dict(row) if row else None})


@app.route("/gestion/allergenes/toggle", methods=["POST"])
def allergenes_toggle():
    db = get_db()
    data = request.get_json()
    plat_id = int(data.get("id", 0))
    field = data.get("field", "")
    if field not in _ALLERGENE_COLS:
        return jsonify({"ok": False}), 400
    row = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), plat_id,)).fetchone()
    if not row:
        return jsonify({"ok": False}), 404
    new_val = 0 if row[field] else 1
    db.execute(f"UPDATE allergenes SET {field}=? WHERE id=?", (new_val, plat_id))
    db.commit()
    # Sync to Airtable
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    at_id = row["airtable_id"] or ""
    try:
        if not at_id and rid:
            # Record was never created in Airtable — create it now with current state
            updated = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), plat_id,)).fetchone()
            rec = at.create_allergene(rid, updated["nom_plat"], updated["categorie"])
            at_id = rec.get("id", "")
            db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (at_id, plat_id))
            db.commit()
            # Push all current checkbox values
            fields = {}
            for i, col in enumerate(_ALLERGENE_COLS):
                fields[_ALLERGENE_AT_FIELDS[i]] = bool(updated[col])
            if updated["autres"]:
                fields["Autres"] = updated["autres"]
            at.update_allergene(at_id, fields)
        elif at_id:
            at_field = _ALLERGENE_AT_FIELDS[_ALLERGENE_COLS.index(field)]
            at.update_allergene(at_id, {at_field: bool(new_val)})
    except Exception as e:
        print(f"[ALLERGENES] Airtable sync error: {e}")
    return jsonify({"ok": True, "value": new_val})


@app.route("/gestion/allergenes/autres", methods=["POST"])
def allergenes_autres():
    db = get_db()
    data = request.get_json()
    plat_id = int(data.get("id", 0))
    autres = data.get("autres", "")
    db.execute("UPDATE allergenes SET autres=? WHERE id=?", (autres, plat_id))
    db.commit()
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    row = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), plat_id,)).fetchone()
    if not row:
        return jsonify({"ok": True})
    at_id = row["airtable_id"] or ""
    try:
        if not at_id and rid:
            rec = at.create_allergene(rid, row["nom_plat"], row["categorie"])
            at_id = rec.get("id", "")
            db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (at_id, plat_id))
            db.commit()
        if at_id:
            at.update_allergene(at_id, {"Autres": autres})
    except Exception as e:
        print(f"[ALLERGENES] Airtable sync error: {e}")
    return jsonify({"ok": True})


@app.route("/gestion/allergenes/delete", methods=["POST"])
def allergenes_delete():
    db = get_db()
    data = request.get_json()
    plat_id = int(data.get("id", 0))
    row = db.execute("SELECT airtable_id FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), plat_id,)).fetchone()
    db.execute("DELETE FROM allergenes WHERE id=?", (plat_id,))
    db.commit()
    if row and row["airtable_id"]:
        try:
            at.delete_allergene(row["airtable_id"])
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/public/allergenes/<restaurant_id>")
def public_allergenes(restaurant_id):
    """Public allergen page — no login required, mobile-friendly."""
    resto = at.get_restaurant(restaurant_id) or {}
    resto_name = resto.get("Nom", restaurant_id)
    # Load allergens from Airtable directly
    alg_data = at.get_allergenes(restaurant_id)
    _EXCLUDED = {"Classiques", "Creations", "Créations", "Sans Alcool", "Boisson", "Cocktail", "Bar"}
    plats = [a for a in alg_data if a.get("Categorie", "") not in _EXCLUDED]
    alg_fields = at.ALLERGENE_FIELDS
    alg_colors = {
        "Gluten": "#D97706", "Crustaces": "#DC2626", "Oeufs": "#F59E0B", "Poisson": "#2563EB",
        "Arachides": "#7C3AED", "Soja": "#059669", "Lait": "#6366F1", "Fruits_a_coque": "#92400E",
        "Celeri": "#0D9488", "Moutarde": "#CA8A04", "Sesame": "#EA580C", "Sulfites": "#9333EA",
        "Lupin": "#0284C7", "Mollusques": "#BE185D",
    }
    alg_display = {
        "Gluten": "Gluten", "Crustaces": "Crustacés", "Oeufs": "Œufs", "Poisson": "Poisson",
        "Arachides": "Arachides", "Soja": "Soja", "Lait": "Lait", "Fruits_a_coque": "Fruits à coque",
        "Celeri": "Céleri", "Moutarde": "Moutarde", "Sesame": "Sésame", "Sulfites": "Sulfites",
        "Lupin": "Lupin", "Mollusques": "Mollusques",
    }
    today_str = date.today().strftime("%d/%m/%Y")

    # Build plat list with present allergens
    plats_out = []
    for p in plats:
        present = []
        for f in alg_fields:
            if p.get(f):
                present.append({"name": alg_display.get(f, f), "color": alg_colors.get(f, "#6B7280")})
        plats_out.append({"nom": p.get("Nom_plat", ""), "cat": p.get("Categorie", ""), "allergens": present})

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Allergènes — {resto_name}</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'DM Sans',sans-serif;background:#F7F4EF;color:#17120D;min-height:100vh}}
.hdr{{background:#0F1F14;padding:20px 16px;text-align:center}}
.hdr h1{{color:#fff;font-size:18px;font-weight:700;margin-bottom:2px}}
.hdr p{{color:rgba(255,255,255,.6);font-size:12px}}
.wrap{{max-width:480px;margin:0 auto;padding:16px}}
.cat{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6A6059;margin:16px 0 8px;padding-bottom:4px;border-bottom:1px solid #E4DDD3}}
.card{{background:#fff;border:1px solid #E4DDD3;border-radius:10px;padding:14px 16px;margin-bottom:8px}}
.card h3{{font-size:15px;font-weight:600;margin-bottom:8px}}
.badges{{display:flex;flex-wrap:wrap;gap:6px}}
.badge{{font-size:11px;font-weight:600;padding:3px 10px;border-radius:6px;color:#fff}}
.empty{{font-size:12px;color:#B0A89E;font-style:italic}}
.ftr{{text-align:center;padding:20px 16px;font-size:11px;color:#B0A89E}}
</style></head><body>
<div class="hdr"><h1>{resto_name}</h1><p>Allergènes</p></div>
<div class="wrap">"""

    cur_cat = ""
    for p in plats_out:
        if p["cat"] != cur_cat:
            cur_cat = p["cat"]
            html += f'<div class="cat">{cur_cat}</div>'
        html += f'<div class="card"><h3>{p["nom"]}</h3><div class="badges">'
        if p["allergens"]:
            for a in p["allergens"]:
                html += f'<span class="badge" style="background:{a["color"]}">{a["name"]}</span>'
        else:
            html += '<span class="empty">Aucun allergène déclaré</span>'
        html += '</div></div>'

    html += f"""</div>
<div class="ftr">Informations mises à jour le {today_str}<br>Généré par Pauco · paucoandco.com</div>
</body></html>"""
    return html


@app.route("/gestion/allergenes/qrcode")
@login_required
def allergenes_qrcode():
    """Generate QR code pointing to the public allergen page."""
    rid = current_user.restaurant_id
    base_url = request.host_url.rstrip("/").replace("http://", "https://")
    public_url = f"{base_url}/public/allergenes/{rid}"

    try:
        import qrcode
        from io import BytesIO
        import base64
        qr = qrcode.make(public_url, box_size=10, border=2)
        qr_buf = BytesIO()
        qr.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        qr_b64 = base64.b64encode(qr_buf.getvalue()).decode()
    except Exception as e:
        return jsonify({"ok": False, "error": f"QR generation failed: {e}"}), 500

    try:
        at.update_restaurant(rid, **{"QR_Allergenes_URL": public_url})
    except Exception:
        pass

    return jsonify({"ok": True, "pdf_url": public_url, "qr_png_b64": qr_b64})


@app.route("/gestion/allergenes/qrcode/download")
@login_required
def allergenes_qrcode_download():
    """Download QR code as PNG."""
    rid = current_user.restaurant_id
    resto = at.get_restaurant(rid) or {}
    pdf_url = resto.get("QR_Allergenes_URL", "") or ""
    if not pdf_url:
        return "QR code non généré — cliquez d'abord sur Générer", 404
    try:
        import qrcode
        from io import BytesIO
        qr = qrcode.make(pdf_url, box_size=12, border=2)
        buf = BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png",
                         download_name=f"qr_allergenes_{rid}.png",
                         as_attachment=True)
    except Exception as e:
        return f"Erreur: {e}", 500
