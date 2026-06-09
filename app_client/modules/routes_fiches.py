# -*- coding: utf-8 -*-
"""
Routes pour les fiches (catégories, fiches hub, bar, food, cocktails).
"""

import json
from datetime import datetime

from flask import render_template, request, jsonify
from flask_login import login_required, current_user

from modules.config import app
from modules.db import get_db
from modules.helpers import _rid
from modules import airtable_client as at
from modules import airtable_sync as sync


_DEFAULT_CATEGORIES = {
    "food": ['Entrées', 'Plats', 'Desserts'],
    "cocktail": ['Classiques', 'Créations', 'Sans Alcool'],
    "bar": ['Vins', 'Bières', 'Softs & Jus', 'Gins', 'Whisky & Bourbon', 'Rhums', 'Vodkas', 'Autres spiritueux'],
}


def _get_categories(db, ftype):
    """Get categories for a fiche type, ordered by 'ordre' field.
    Seeds default categories for the current restaurant if none exist."""
    rid = _rid()
    rows = db.execute("SELECT * FROM categories_fiches WHERE restaurant_id=? AND type=? ORDER BY ordre, id", (rid, ftype,)).fetchall()
    if not rows and ftype in _DEFAULT_CATEGORIES:
        for i, cat in enumerate(_DEFAULT_CATEGORIES[ftype]):
            db.execute("INSERT OR IGNORE INTO categories_fiches (restaurant_id, type, nom, ordre) VALUES (?, ?, ?, ?)", (rid, ftype, cat, i))
        db.commit()
        rows = db.execute("SELECT * FROM categories_fiches WHERE restaurant_id=? AND type=? ORDER BY ordre, id", (rid, ftype,)).fetchall()
    return [r["nom"] for r in rows]


def _sync_categories_to_airtable(db, ftype):
    """Sync all category names and orders to Airtable (delete + recreate)."""
    rid = _rid()
    if not rid or getattr(current_user, "demo_mode", False):
        return
    try:
        # Delete existing in Airtable for this type
        existing = at.get_all("categories_fiches", restaurant_id=rid,
                              formula=f"{{Type}}='{ftype}'")
        if existing:
            at.batch_delete("categories_fiches", [r["id"] for r in existing])
        # Recreate with current order
        rows = db.execute("SELECT nom, ordre FROM categories_fiches WHERE restaurant_id=? AND type=? ORDER BY ordre, id",
                          (rid, ftype)).fetchall()
        if rows:
            at.batch_create("categories_fiches",
                [{"Restaurant_ID": rid, "Type": ftype, "Nom": r["nom"], "Ordre": r["ordre"]} for r in rows])
        at.invalidate_cache("categories_fiches")
    except Exception as e:
        print(f"[SYNC] categories_fiches sync error: {e}")


def _set_fiche_statut_airtable(rid, fiche_type, nom, statut):
    """Update Statut field on the matching Airtable fiche record."""
    if not rid or not nom or getattr(current_user, "demo_mode", False):
        return
    try:
        nom_esc = nom.replace("'", "\\'")
        rec = at.find_first("fiches",
            f"AND({{Restaurant_ID}}='{rid}',{{Type}}='{fiche_type}',{{Nom}}='{nom_esc}')")
        print(f"[FICHE-STATUT] lookup type={fiche_type} nom={nom!r} → {rec['id'] if rec else 'NONE'}")
        if rec:
            at.update("fiches", rec["id"], {"Statut": statut})
            at.invalidate_cache("fiches")
            print(f"[FICHE-STATUT] Airtable OK {rec['id']} → {statut}")
    except Exception as e:
        print(f"[FICHE-STATUT] error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


def _delete_fiche_from_airtable(rid, fiche_type, nom):
    """Delete a fiche from Airtable by (Restaurant_ID, Type, Nom)."""
    if not rid or not nom or getattr(current_user, "demo_mode", False):
        return
    try:
        nom_esc = nom.replace("'", "\\'")
        rec = at.find_first("fiches",
            f"AND({{Restaurant_ID}}='{rid}',{{Type}}='{fiche_type}',{{Nom}}='{nom_esc}')")
        print(f"[FICHE-DEL] lookup type={fiche_type} nom={nom!r} rid={rid} → {rec['id'] if rec else 'NONE'}")
        if rec:
            at.delete("fiches", rec["id"])
            at.invalidate_cache("fiches")
            print(f"[FICHE-DEL] Airtable OK deleted {rec['id']}")
    except Exception as e:
        print(f"[FICHE-DEL] error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


# ── Categories ──────────────────────────────────────────────────────────

@app.route("/categories/manage", methods=["POST"])
def categories_manage():
    db = get_db()
    data = request.get_json()
    action = data.get("action", "")
    ftype = data.get("type", "food")
    if action == "add":
        nom = data.get("nom", "").strip()
        if nom:
            sync.write_categorie(db, ftype, nom)
    elif action == "rename":
        old = data.get("old", "")
        new = data.get("new", "").strip()
        if old and new:
            db.execute("UPDATE categories_fiches SET nom=? WHERE restaurant_id=? AND type=? AND nom=?", (new, _rid(), ftype, old))
            db.commit()
            _sync_categories_to_airtable(db, ftype)
    elif action == "delete":
        nom = data.get("nom", "")
        if nom:
            sync.delete_categorie(db, ftype, nom)
    elif action == "reorder":
        nom = data.get("nom", "")
        direction = data.get("direction", "up")
        rows = db.execute("SELECT id, nom, ordre FROM categories_fiches WHERE restaurant_id=? AND type=? ORDER BY ordre, id", (_rid(), ftype,)).fetchall()
        # Normalize: ensure unique sequential ordre values
        for idx, r in enumerate(rows):
            if r["ordre"] != idx:
                db.execute("UPDATE categories_fiches SET ordre=? WHERE id=?", (idx, r["id"]))
        rows = db.execute("SELECT id, nom, ordre FROM categories_fiches WHERE restaurant_id=? AND type=? ORDER BY ordre, id", (_rid(), ftype,)).fetchall()
        for i, r in enumerate(rows):
            if r["nom"] == nom:
                if direction == "up" and i > 0:
                    db.execute("UPDATE categories_fiches SET ordre=? WHERE id=?", (rows[i-1]["ordre"], r["id"]))
                    db.execute("UPDATE categories_fiches SET ordre=? WHERE id=?", (r["ordre"], rows[i-1]["id"]))
                elif direction == "down" and i < len(rows) - 1:
                    db.execute("UPDATE categories_fiches SET ordre=? WHERE id=?", (rows[i+1]["ordre"], r["id"]))
                    db.execute("UPDATE categories_fiches SET ordre=? WHERE id=?", (r["ordre"], rows[i+1]["id"]))
                db.commit()
                break
        _sync_categories_to_airtable(db, ftype)
    cats = _get_categories(db, ftype)
    return jsonify({"ok": True, "categories": cats})


@app.route("/categories/list")
def categories_list():
    db = get_db()
    ftype = request.args.get("type", "food")
    return jsonify({"categories": _get_categories(db, ftype)})


# ── Fiches Hub ─────────────────────────────────────────────────────────

@app.route("/fiches")
def fiches_hub():
    db = get_db()
    # Count from each table
    nb_food = db.execute("SELECT COUNT(*) as c FROM fiches_techniques WHERE restaurant_id=? AND statut='actif'", (_rid(),)).fetchone()["c"]
    nb_cocktails = db.execute("SELECT COUNT(*) as c FROM fiches_cocktails WHERE restaurant_id=? AND statut='actif'", (_rid(),)).fetchone()["c"]
    nb_bar = db.execute("SELECT COUNT(*) as c FROM boissons_bar WHERE restaurant_id=? AND statut='actif'", (_rid(),)).fetchone()["c"]
    # Build unified list for search
    all_fiches = []
    for r in db.execute("SELECT id, nom, cout_ht FROM fiches_techniques WHERE restaurant_id=? AND statut='actif' ORDER BY nom", (_rid(),)).fetchall():
        all_fiches.append({"id": r["id"], "nom": r["nom"], "type": "food", "cout_total": r["cout_ht"]})
    for r in db.execute("SELECT id, nom, cout_ht FROM fiches_cocktails WHERE restaurant_id=? AND statut='actif' ORDER BY nom", (_rid(),)).fetchall():
        all_fiches.append({"id": r["id"], "nom": r["nom"], "type": "cocktail", "cout_total": r["cout_ht"]})
    for r in db.execute("SELECT id, nom, prix_achat_ht FROM boissons_bar WHERE restaurant_id=? AND statut='actif' ORDER BY nom", (_rid(),)).fetchall():
        all_fiches.append({"id": r["id"], "nom": r["nom"], "type": "bar", "cout_total": r["prix_achat_ht"]})
    # Recent: last 4 across all tables by id desc
    recents = sorted(all_fiches, key=lambda x: -x["id"])[:4]
    return render_template("base.html", page="fiches_hub",
                           nb_food=nb_food, nb_cocktails=nb_cocktails, nb_bar=nb_bar,
                           recents=recents, all_fiches=all_fiches)


# ── Boissons Bar ───────────────────────────────────────────────────────

@app.route("/fiches/boissons-bar")
def fiches_bar():
    db = get_db()
    all_b = db.execute("SELECT * FROM boissons_bar WHERE restaurant_id=? ORDER BY categorie, nom", (_rid(),)).fetchall()
    use_demo = False
    produits = [dict(b) for b in all_b if b["statut"] == "actif"]
    archives_bar = [dict(b) for b in all_b if b["statut"] == "archive"]
    bar_cats_db = _get_categories(db, "bar")
    # Also include categories actually used by products (in case Airtable categories are incomplete)
    product_cats = sorted(set(p.get("categorie", "") for p in produits if p.get("categorie")))
    all_cats = list(bar_cats_db)
    for c in product_cats:
        if c not in all_cats:
            all_cats.append(c)
    return render_template("base.html", page="fiches_bar", bar_produits=produits, archives_bar=archives_bar, use_demo_bar=use_demo, bar_cats=all_cats)


@app.route("/fiches/boissons-bar/save", methods=["POST"])
def bar_save():
    db = get_db()
    data = request.get_json()
    nom = data.get("nom", "")
    cat = data.get("categorie", "Autres spiritueux")
    contenance = float(data.get("contenance_cl", 0))
    prix_achat = float(data.get("prix_achat_ht", 0))
    pvttc = float(data.get("prix_vente_ttc", 0))
    pvht = round(pvttc / 1.10, 2) if pvttc > 0 else 0
    cout_cl = round(prix_achat / contenance, 2) if contenance > 0 else 0
    pc = float(data.get("perte_casse", 0))
    pd = float(data.get("perte_degustation", 0))
    pe = float(data.get("perte_evaporation", 0))
    total_pertes = (pc + pd + pe) / 100
    cout_ajuste = round(prix_achat * (1 + total_pertes), 2)
    coeff = round(pvht / cout_ajuste, 1) if cout_ajuste > 0 else 0
    ratio = round(cout_ajuste / pvht * 100, 1) if pvht > 0 else 0
    marge = round(pvht - cout_ajuste, 2)

    rid = _rid()
    bid = data.get("id")
    print(f"[BAR-SAVE] rid={rid} nom={nom!r} cat={cat!r} cont={contenance} pa={prix_achat} pvttc={pvttc}")
    if bid:
        db.execute("""UPDATE boissons_bar SET nom=?,categorie=?,contenance_cl=?,prix_achat_ht=?,prix_vente_ttc=?,prix_vente_ht=?,
                      cout_ht_cl=?,perte_casse=?,perte_degustation=?,perte_evaporation=?,cout_ajuste=?,coefficient=?,ratio_mp=?,marge_ht=? WHERE restaurant_id=? AND id=?""",
                   (nom, cat, contenance, prix_achat, pvttc, pvht, cout_cl, pc, pd, pe, cout_ajuste, coeff, ratio, marge, rid, bid))
    else:
        cur = db.execute("""INSERT INTO boissons_bar (restaurant_id,nom,categorie,contenance_cl,prix_achat_ht,prix_vente_ttc,prix_vente_ht,
                      cout_ht_cl,perte_casse,perte_degustation,perte_evaporation,cout_ajuste,coefficient,ratio_mp,marge_ht)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, nom, cat, contenance, prix_achat, pvttc, pvht, cout_cl, pc, pd, pe, cout_ajuste, coeff, ratio, marge))
        bid = cur.lastrowid
    db.commit()
    print(f"[BAR-SAVE] sqlite OK id={bid}")

    if rid and not getattr(current_user, "demo_mode", False):
        try:
            print(f"[BAR-SAVE] -> Airtable upsert_fiche Boisson Nom={nom!r}")
            created = at.upsert_fiche(rid, "Boisson", nom, categorie=cat,
                                      prix_vente_ht=pvht, prix_vente_ttc=pvttc,
                                      cout_ht=cout_ajuste, coefficient=coeff,
                                      ratio_mp=ratio, marge_ht=marge,
                                      contenance_cl=contenance, prix_achat_ht=prix_achat,
                                      cout_ht_cl=cout_cl, perte_casse=pc,
                                      perte_degustation=pd, perte_evaporation=pe,
                                      cout_ajuste=cout_ajuste)
            print(f"[BAR-SAVE] Airtable OK id={created.get('id') if created else None}")
        except Exception as e:
            print(f"[BAR-SAVE] Airtable ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    return jsonify({"ok": True, "id": bid})


@app.route("/fiches/boissons-bar/delete/<int:id>", methods=["POST"])
def bar_delete(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM boissons_bar WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("DELETE FROM boissons_bar WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _delete_fiche_from_airtable(rid, "Boisson", row["nom"])
    return jsonify({"ok": True})


@app.route("/fiches/boissons-bar/archive/<int:id>", methods=["POST"])
def bar_archive(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM boissons_bar WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("UPDATE boissons_bar SET statut='archive' WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _set_fiche_statut_airtable(rid, "Boisson", row["nom"], "archive")
    return jsonify({"ok": True})


@app.route("/fiches/boissons-bar/restore/<int:id>", methods=["POST"])
def bar_restore(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM boissons_bar WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("UPDATE boissons_bar SET statut='actif' WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _set_fiche_statut_airtable(rid, "Boisson", row["nom"], "actif")
    return jsonify({"ok": True})


# ── Fiches Techniques (Food) ───────────────────────────────────────────

@app.route("/gestion/fiches-techniques")
def fiches_techniques():
    db = get_db()
    all_fiches = db.execute("SELECT * FROM fiches_techniques WHERE restaurant_id=? ORDER BY categorie, nom", (_rid(),)).fetchall()
    use_demo = False
    fiches_data = []
    archives_data = []
    if all_fiches:
        for f in all_fiches:
            ingr = db.execute("SELECT * FROM fiche_ingredients WHERE restaurant_id=? AND fiche_id=? ORDER BY id", (_rid(), f["id"],)).fetchall()
            entry = {**dict(f), "ingredients": [dict(i) for i in ingr]}
            if f["statut"] == "archive":
                archives_data.append(entry)
            else:
                fiches_data.append(entry)
    food_cats = _get_categories(db, "food")
    return render_template("base.html", page="fiches_techniques",
        fiches=fiches_data, archives=archives_data, use_demo=use_demo, food_cats=food_cats)


@app.route("/gestion/fiches-techniques/save", methods=["POST"])
def fiche_save():
    db = get_db()
    data = request.get_json()
    nom = data.get("nom", "")
    categorie = data.get("categorie", "Plats")
    prix_ttc = float(data.get("prix_vente_ttc", 0))
    prix_ht = float(data.get("prix_vente_ht", 0))
    # Si TTC fourni mais pas HT, deriver HT
    if prix_ttc > 0 and prix_ht == 0:
        prix_ht = round(prix_ttc / 1.10, 2)
    # Si HT fourni mais pas TTC, deriver TTC
    if prix_ht > 0 and prix_ttc == 0:
        prix_ttc = round(prix_ht * 1.10, 2)
    ingredients = data.get("ingredients", [])
    # Calculer cout_portion selon unité
    for i in ingredients:
        unite = i.get("unite", "g")
        qty = float(i.get("quantite", 0) or i.get("grammes", 0))
        pk = float(i.get("prix_kilo", 0))
        pl = float(i.get("prix_litre", 0))
        if unite == "g":
            i["cout_portion"] = round(pk * qty / 1000, 2)
        elif unite == "kg":
            i["cout_portion"] = round(pk * qty, 2)
        elif unite == "cl":
            i["cout_portion"] = round(pl * qty / 100, 2)
        elif unite == "L":
            i["cout_portion"] = round(pl * qty, 2)
        else:
            i["cout_portion"] = round(pk * qty / 1000, 2)
        i["quantite"] = qty
        i["unite"] = unite
    cout_ht = round(sum(i["cout_portion"] for i in ingredients), 2)
    coefficient = round(prix_ht / cout_ht, 1) if cout_ht > 0 else 0
    ratio_mp = round(cout_ht / prix_ht * 100, 1) if prix_ht > 0 else 0
    marge_ht = round(prix_ht - cout_ht, 2)

    nb_portions = int(data.get("nb_portions", 0) or 0)
    temps_preparation = int(data.get("temps_preparation", 0) or 0)

    rid = _rid()
    print(f"[FICHE-FOOD-SAVE] rid={rid} nom={nom!r} cat={categorie!r} pvht={prix_ht} pvttc={prix_ttc} cout={cout_ht} ingredients={len(ingredients)}")
    fiche_id = data.get("id")
    if fiche_id:
        old = db.execute("SELECT * FROM fiches_techniques WHERE restaurant_id=? AND id=?", (rid, fiche_id,)).fetchone()
        if old:
            now = datetime.now().strftime("%d/%m/%Y %H:%M")
            changes = [("nom", old["nom"], nom), ("prix_vente_ttc", str(old["prix_vente_ttc"]), str(prix_ttc)),
                       ("prix_vente_ht", str(old["prix_vente_ht"]), str(prix_ht)), ("cout_ht", str(old["cout_ht"]), str(cout_ht))]
            for champ, av, nv in changes:
                if av != nv:
                    db.execute("INSERT INTO historique_fiches (restaurant_id,fiche_id,fiche_type,champ,ancienne_valeur,nouvelle_valeur,date_modification) VALUES (?,?,?,?,?,?,?)",
                               (rid, fiche_id, "food", champ, av, nv, now))
        db.execute("UPDATE fiches_techniques SET nom=?,categorie=?,prix_vente_ht=?,prix_vente_ttc=?,cout_ht=?,coefficient=?,ratio_mp=?,marge_ht=?,nb_portions=?,temps_preparation=? WHERE restaurant_id=? AND id=?",
                   (nom, categorie, prix_ht, prix_ttc, cout_ht, coefficient, ratio_mp, marge_ht, nb_portions, temps_preparation, rid, fiche_id))
        db.execute("DELETE FROM fiche_ingredients WHERE fiche_id=?", (fiche_id,))
    else:
        cur = db.execute("INSERT INTO fiches_techniques (restaurant_id,nom,categorie,prix_vente_ht,prix_vente_ttc,cout_ht,coefficient,ratio_mp,marge_ht,nb_portions,temps_preparation) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, nom, categorie, prix_ht, prix_ttc, cout_ht, coefficient, ratio_mp, marge_ht, nb_portions, temps_preparation))
        fiche_id = cur.lastrowid

    for i in ingredients:
        unite = i.get("unite", "g")
        qty = float(i.get("quantite", 0))
        pk = float(i.get("prix_kilo", 0))
        pl = float(i.get("prix_litre", 0))
        cp = float(i.get("cout_portion", 0))
        gr = qty if unite in ("g", "kg") else 0
        db.execute("INSERT INTO fiche_ingredients (restaurant_id,fiche_id,produit,prix_kilo,grammes,cout_portion,unite,quantite,prix_litre) VALUES (?,?,?,?,?,?,?,?,?)",
                   (rid, fiche_id, i.get("produit", ""), pk, gr, cp, unite, qty, pl))
    db.commit()
    print(f"[FICHE-FOOD-SAVE] sqlite OK id={fiche_id}")

    # Push Airtable
    if rid and not getattr(current_user, "demo_mode", False):
        try:
            import json as _json
            ing_payload = [{
                "produit": i.get("produit", ""),
                "quantite": float(i.get("quantite", 0)),
                "unite": i.get("unite", "g"),
                "prix_kilo": float(i.get("prix_kilo", 0)),
                "prix_litre": float(i.get("prix_litre", 0)),
                "cout_portion": float(i.get("cout_portion", 0)),
            } for i in ingredients]
            ingj = _json.dumps(ing_payload, ensure_ascii=False)
            print(f"[FICHE-FOOD-SAVE] -> Airtable upsert_fiche Nom={nom!r} ingredients={len(ing_payload)}")
            created = at.upsert_fiche(rid, "Food", nom, categorie=categorie,
                                      prix_vente_ht=prix_ht, prix_vente_ttc=prix_ttc,
                                      cout_ht=cout_ht, coefficient=coefficient,
                                      ratio_mp=ratio_mp, marge_ht=marge_ht,
                                      nb_portions=nb_portions, temps_prepa=temps_preparation,
                                      ingredients_json=ingj)
            print(f"[FICHE-FOOD-SAVE] Airtable OK id={created.get('id') if created else None}")
        except Exception as e:
            print(f"[FICHE-FOOD-SAVE] Airtable ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    return jsonify({"ok": True, "id": fiche_id})


@app.route("/gestion/fiches-techniques/delete/<int:id>", methods=["POST"])
def fiche_delete(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM fiches_techniques WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("DELETE FROM fiche_ingredients WHERE fiche_id=?", (id,))
    db.execute("DELETE FROM fiches_techniques WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _delete_fiche_from_airtable(rid, "Food", row["nom"])
    return jsonify({"ok": True})


@app.route("/gestion/fiches-techniques/archive/<int:id>", methods=["POST"])
def fiche_archive(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM fiches_techniques WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("UPDATE fiches_techniques SET statut='archive' WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _set_fiche_statut_airtable(rid, "Food", row["nom"], "archive")
    return jsonify({"ok": True})


@app.route("/gestion/fiches-techniques/restore/<int:id>", methods=["POST"])
def fiche_restore(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM fiches_techniques WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("UPDATE fiches_techniques SET statut='actif' WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _set_fiche_statut_airtable(rid, "Food", row["nom"], "actif")
    return jsonify({"ok": True})


# ── Fiches Cocktails ───────────────────────────────────────────────────

@app.route("/gestion/fiches-cocktails")
def fiches_cocktails():
    db = get_db()
    all_c = db.execute("SELECT * FROM fiches_cocktails WHERE restaurant_id=? ORDER BY categorie, nom", (_rid(),)).fetchall()
    use_demo = False
    cocktails = []
    archives = []
    if all_c:
        for c in all_c:
            ingr = db.execute("SELECT * FROM cocktail_ingredients WHERE restaurant_id=? AND cocktail_id=? ORDER BY id", (_rid(), c["id"],)).fetchall()
            entry = {**dict(c), "ingredients": [dict(i) for i in ingr]}
            if c["statut"] == "archive":
                archives.append(entry)
            else:
                cocktails.append(entry)
    cocktail_cats = _get_categories(db, "cocktail")
    return render_template("base.html", page="fiches_cocktails",
        cocktails=cocktails, archives_cocktails=archives, use_demo_cocktails=use_demo, cocktail_cats=cocktail_cats)


@app.route("/gestion/fiches-cocktails/save", methods=["POST"])
def cocktail_save():
    db = get_db()
    data = request.get_json()
    nom = data.get("nom", "")
    categorie = data.get("categorie", "Creations")
    volume_cl = float(data.get("volume_cl", 0))
    prix_ttc = float(data.get("prix_vente_ttc", 0))
    prix_ht = float(data.get("prix_vente_ht", 0))
    if prix_ttc > 0 and prix_ht == 0:
        prix_ht = round(prix_ttc / 1.10, 2)
    if prix_ht > 0 and prix_ttc == 0:
        prix_ttc = round(prix_ht * 1.10, 2)
    ingredients = data.get("ingredients", [])
    for i in ingredients:
        cu = float(i.get("cout_ht_unitaire", 0))
        qb = float(i.get("qte_bouteille_cl", 0))
        i["cout_ht_cl"] = round(cu / qb, 2) if qb > 0 else 0
        qu = float(i.get("qte_utilisee_cl", 0))
        i["cout_ht_verre"] = round(i["cout_ht_cl"] * qu, 2) if i["cout_ht_cl"] > 0 else float(i.get("cout_ht_verre", 0))
    cout_ht = round(sum(float(i.get("cout_ht_verre", 0)) for i in ingredients), 2)
    coefficient = round(prix_ht / cout_ht, 1) if cout_ht > 0 else 0
    ratio_mp = round(cout_ht / prix_ht * 100, 1) if prix_ht > 0 else 0
    marge_ht = round(prix_ht - cout_ht, 2)

    rid = _rid()
    print(f"[CK-SAVE] rid={rid} nom={nom!r} cat={categorie!r} vol={volume_cl} pvht={prix_ht} pvttc={prix_ttc} cout={cout_ht} ingredients={len(ingredients)}")
    cid = data.get("id")
    if cid:
        db.execute("UPDATE fiches_cocktails SET nom=?,categorie=?,volume_cl=?,prix_vente_ht=?,prix_vente_ttc=?,cout_ht=?,coefficient=?,ratio_mp=?,marge_ht=? WHERE restaurant_id=? AND id=?",
                   (nom, categorie, volume_cl, prix_ht, prix_ttc, cout_ht, coefficient, ratio_mp, marge_ht, rid, cid))
        db.execute("DELETE FROM cocktail_ingredients WHERE cocktail_id=?", (cid,))
    else:
        cur = db.execute("INSERT INTO fiches_cocktails (restaurant_id,nom,categorie,volume_cl,prix_vente_ht,prix_vente_ttc,cout_ht,coefficient,ratio_mp,marge_ht) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (rid, nom, categorie, volume_cl, prix_ht, prix_ttc, cout_ht, coefficient, ratio_mp, marge_ht))
        cid = cur.lastrowid
    for i in ingredients:
        db.execute("INSERT INTO cocktail_ingredients (restaurant_id,cocktail_id,produit,cout_ht_unitaire,qte_bouteille_cl,cout_ht_cl,qte_utilisee_cl,cout_ht_verre) VALUES (?,?,?,?,?,?,?,?)",
                   (rid, cid, i.get("produit", ""), float(i.get("cout_ht_unitaire", 0)), float(i.get("qte_bouteille_cl", 0)),
                    float(i.get("cout_ht_cl", 0)), float(i.get("qte_utilisee_cl", 0)), float(i.get("cout_ht_verre", 0))))
    db.commit()
    print(f"[CK-SAVE] sqlite OK id={cid}")

    # Push Airtable (création uniquement — update fait via re-création possible plus tard)
    if rid and not getattr(current_user, "demo_mode", False):
        try:
            import json as _json
            ing_payload = [{
                "produit": i.get("produit", ""),
                "cout_ht_unitaire": float(i.get("cout_ht_unitaire", 0)),
                "qte_bouteille_cl": float(i.get("qte_bouteille_cl", 0)),
                "cout_ht_cl": float(i.get("cout_ht_cl", 0)),
                "qte_utilisee_cl": float(i.get("qte_utilisee_cl", 0)),
                "cout_ht_verre": float(i.get("cout_ht_verre", 0)),
            } for i in ingredients]
            ingj = _json.dumps(ing_payload, ensure_ascii=False)
            print(f"[CK-SAVE] -> Airtable upsert_fiche Cocktail Nom={nom!r} ingredients={len(ing_payload)}")
            created = at.upsert_fiche(rid, "Cocktail", nom, categorie=categorie,
                                      prix_vente_ht=prix_ht, prix_vente_ttc=prix_ttc,
                                      cout_ht=cout_ht, coefficient=coefficient,
                                      ratio_mp=ratio_mp, marge_ht=marge_ht,
                                      volume_cl=volume_cl, ingredients_json=ingj)
            print(f"[CK-SAVE] Airtable OK id={created.get('id') if created else None}")
        except Exception as e:
            print(f"[CK-SAVE] Airtable ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    return jsonify({"ok": True, "id": cid})


@app.route("/gestion/fiches-cocktails/delete/<int:id>", methods=["POST"])
def cocktail_delete(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM fiches_cocktails WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("DELETE FROM cocktail_ingredients WHERE cocktail_id=?", (id,))
    db.execute("DELETE FROM fiches_cocktails WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _delete_fiche_from_airtable(rid, "Cocktail", row["nom"])
    return jsonify({"ok": True})


@app.route("/gestion/fiches-cocktails/archive/<int:id>", methods=["POST"])
def cocktail_archive(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM fiches_cocktails WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("UPDATE fiches_cocktails SET statut='archive' WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _set_fiche_statut_airtable(rid, "Cocktail", row["nom"], "archive")
    return jsonify({"ok": True})


@app.route("/gestion/fiches-cocktails/restore/<int:id>", methods=["POST"])
def cocktail_restore(id):
    db = get_db()
    rid = _rid()
    row = db.execute("SELECT nom FROM fiches_cocktails WHERE restaurant_id=? AND id=?", (rid, id)).fetchone()
    db.execute("UPDATE fiches_cocktails SET statut='actif' WHERE restaurant_id=? AND id=?", (rid, id))
    db.commit()
    if row:
        _set_fiche_statut_airtable(rid, "Cocktail", row["nom"], "actif")
    return jsonify({"ok": True})
