"""
Airtable ↔ SQLite sync module.
Downloads all restaurant data from Airtable into local SQLite for fast reads.
Write operations go to BOTH Airtable (source of truth) AND SQLite (cache).

PROTECTION: jamais de DELETE sur users ou restaurants.
_clear_table() bloque automatiquement toute tentative sur ces tables.
"""

import sqlite3
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from . import airtable_client as at

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gestion.db")


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


_synced_rids = {}       # {restaurant_id: timestamp} — per-restaurant cooldown
_SYNC_COOLDOWN = 300    # don't re-sync within 5 minutes


def _prefetch_airtable(restaurant_id):
    """Pre-fetch all Airtable data in parallel threads. Returns dict of results."""
    tables_to_fetch = [
        ("ca_jour", lambda: at.get_all("ca_jour", restaurant_id)),
        ("depenses", lambda: at.get_all("depenses", restaurant_id)),
        ("employes", lambda: at.get_employes(restaurant_id, include_archived=True)),
        ("planning", lambda: at.get_all("planning", restaurant_id)),
        ("fiches", lambda: at.get_fiches(restaurant_id)),
        ("ingredients", lambda: at.get_all("ingredients", restaurant_id)),
        ("fournisseurs", lambda: at.get_fournisseurs(restaurant_id)),
        ("conges", lambda: at.get_conges(restaurant_id)),
        ("evenements", lambda: at.get_evenements(restaurant_id)),
        ("fermetures", lambda: at.get_fermetures(restaurant_id)),
        ("shifts", lambda: at.get_shifts(restaurant_id)),
        ("postes", lambda: at.get_postes(restaurant_id)),
        ("categories", lambda: at.get_categories(restaurant_id)),
        ("messages", lambda: at.get_messages(restaurant_id)),
        ("allergenes", lambda: at.get_allergenes(restaurant_id)),
    ]
    results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fn): name for name, fn in tables_to_fetch}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                print(f"[SYNC] Prefetch error ({name}): {e}")
                results[name] = []
    return results


def sync_restaurant(restaurant_id):
    """Download all data for a restaurant from Airtable into SQLite."""
    if not restaurant_id:
        return
    print(f"[SYNC] Syncing restaurant {restaurant_id} from Airtable...")
    t0 = time.time()

    try:
        # Phase 1: parallel prefetch all Airtable data
        data = _prefetch_airtable(restaurant_id)
        t1 = time.time()
        print(f"[SYNC] Prefetch done in {t1-t0:.1f}s")

        # Phase 2: write to SQLite (sequential, single connection)
        db = _db()
        _sync_ca_jour_from(db, restaurant_id, data["ca_jour"])
        _sync_depenses_from(db, restaurant_id, data["depenses"])
        # Snapshot absences locales (Repos/CP/AM/...) avec leur (prenom,nom) AVANT
        # de wiper les employes — sinon on perd le lien employe_id→nom et les
        # absences orphelinent à chaque sync.
        _absences_snapshot = []
        try:
            _absences_snapshot = db.execute(
                "SELECT e.prenom, e.nom, p.date, p.heure_debut, p.heure_fin, p.poste, "
                "p.shift_id, p.shift_nom, p.shift_couleur "
                "FROM planning p JOIN employes e ON p.employe_id=e.id "
                "WHERE p.restaurant_id=? AND p.heure_debut=p.heure_fin AND length(p.heure_debut)<=4",
                (restaurant_id,)
            ).fetchall()
        except Exception as _e:
            print(f"[SYNC] absences snapshot error: {_e}")
        _sync_employes_from(db, restaurant_id, data["employes"])
        db.commit()  # Commit employes before planning (needs employee IDs)
        _sync_planning_from(db, restaurant_id, data["planning"], data["employes"], absences_snapshot=_absences_snapshot)
        _sync_fiches_from(db, restaurant_id, data["fiches"], data["ingredients"])
        _sync_fournisseurs_from(db, restaurant_id, data["fournisseurs"])
        _sync_conges_from(db, restaurant_id, data["conges"], data["employes"])
        _sync_evenements_from(db, restaurant_id, data["evenements"])
        _sync_fermetures_from(db, restaurant_id, data["fermetures"])
        _sync_shifts_from(db, restaurant_id, data["shifts"])
        _sync_postes_from(db, restaurant_id, data["postes"])
        _sync_categories_from(db, restaurant_id, data["categories"])
        _sync_messages_from(db, restaurant_id, data["messages"])
        _sync_allergenes_from(db, restaurant_id, data["allergenes"])
        db.commit()
        db.close()
        _synced_rids[restaurant_id] = time.time()
    except Exception as e:
        print(f"[SYNC] Error: {e}")
        import traceback
        traceback.print_exc()
        _synced_rids[restaurant_id] = time.time()

    print(f"[SYNC] Done in {time.time()-t0:.1f}s")


def needs_sync(restaurant_id):
    """Check if we need to sync this specific restaurant."""
    if not restaurant_id:
        return False
    # Already synced in this process and cooldown hasn't expired
    if restaurant_id in _synced_rids:
        if time.time() - _synced_rids[restaurant_id] < _SYNC_COOLDOWN:
            return False
        return True
    # First check in this process: verify SQLite has substantial data
    if has_local_data(restaurant_id):
        _synced_rids[restaurant_id] = time.time()
        return False
    return True


def has_local_data(restaurant_id):
    """Check if SQLite has substantial data for this restaurant."""
    db = _db()
    try:
        count = db.execute("SELECT COUNT(*) FROM ca_jour WHERE restaurant_id=?", (restaurant_id,)).fetchone()[0]
        emp_count = db.execute("SELECT COUNT(*) FROM employes WHERE restaurant_id=?", (restaurant_id,)).fetchone()[0]
        db.close()
        return count > 5 and emp_count > 0
    except Exception:
        db.close()
        return False


_sync_lock = {}  # per-restaurant lock to avoid concurrent syncs


def sync_restaurant_async(restaurant_id):
    """Run sync in a background thread. Marks as synced immediately to avoid re-trigger."""
    if restaurant_id in _sync_lock:
        return  # already syncing
    _synced_rids[restaurant_id] = time.time()  # prevent re-trigger during sync

    def _run():
        _sync_lock[restaurant_id] = True
        try:
            sync_restaurant(restaurant_id)
        finally:
            _sync_lock.pop(restaurant_id, None)
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ── Mapping helpers ─────────────────────────────────────────────────────

def _airtable_id_map():
    """Returns a persistent mapping of Airtable IDs to SQLite IDs."""
    # We store Airtable record IDs in a mapping table
    pass


# PROTECTION: jamais de DELETE sur ces tables (SQLite)
_PROTECTED_TABLES = frozenset({"restaurants", "password_resets", "utilisateurs"})


def _clear_table(db, table, rid):
    if table in _PROTECTED_TABLES:
        raise RuntimeError(f"[GUARD] BLOCKED: _clear_table on protected table '{table}'")
    db.execute(f"DELETE FROM {table} WHERE restaurant_id = ?", (rid,))


# ── Individual table syncs ──────────────────────────────────────────────

def _sync_ca_jour(db, rid):
    _sync_ca_jour_from(db, rid, at.get_all("ca_jour", rid))

def _sync_ca_jour_from(db, rid, rows):
    _clear_table(db, "ca_jour", rid)
    for r in rows:
        date_val = r.get("Date", "")
        if not date_val:
            continue
        ca_total = r.get("CA_Total", 0) or 0
        ca_resto = r.get("CA_Restaurant", 0) or 0
        ca_bar = r.get("CA_Bar", 0) or 0
        midi = r.get("Couverts_Midi", 0) or 0
        soir = r.get("Couverts_Soir", 0) or 0
        tickets_bar = r.get("Tickets_Bar", 0) or 0
        tm = r.get("Ticket_Moyen", 0) or 0
        tm_bar = r.get("Ticket_Moyen_Bar", 0) or 0
        comm = r.get("Commentaire", "") or ""
        detail = r.get("CA_Detail", "") or ""
        db.execute("""INSERT OR IGNORE INTO ca_jour (restaurant_id,date,ca,ca_restaurant,ca_bar,couverts_midi,couverts_soir,
                      tickets_bar,ticket_moyen,ticket_moyen_bar,commentaire,ca_detail)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, date_val, ca_total, ca_resto, ca_bar, midi, soir, tickets_bar, tm, tm_bar, comm, detail))
    print(f"[SYNC] ca_jour: {len(rows)} records")


def _sync_depenses(db, rid):
    _sync_depenses_from(db, rid, at.get_all("depenses", rid))

def _sync_depenses_from(db, rid, rows):
    _clear_table(db, "depenses_fixes", rid)
    _clear_table(db, "depenses_variables", rid)
    n_fix = 0
    n_var = 0
    for r in rows:
        dep_type = r.get("Type", "Variable")
        cat = r.get("Catégorie", r.get("Categorie", ""))
        desc = r.get("Description", "") or ""
        montant = r.get("Montant", 0) or 0
        mois = r.get("Mois", "") or ""

        if dep_type == "Fixe":
            recurrente = 1 if r.get("Recurrente") else 0
            desac = r.get("Desactivee_mois", "") or ""
            db.execute("""INSERT INTO depenses_fixes (restaurant_id,categorie,description,montant,mois,recurrente,desactivee_mois)
                          VALUES (?,?,?,?,?,?,?)""", (rid, cat, desc, montant, mois, recurrente, desac))
            n_fix += 1
        else:
            date_val = r.get("Date", "") or ""
            # Toujours dériver mois depuis date pour cohérence
            mois_val = date_val[:7] if date_val and len(date_val) >= 7 else mois
            db.execute("""INSERT INTO depenses_variables (restaurant_id,date,categorie,description,montant,type,mois)
                          VALUES (?,?,?,?,?,'variable',?)""", (rid, date_val, cat, desc, montant, mois_val))
            n_var += 1
    print(f"[SYNC] depenses: {n_fix} fixes, {n_var} variables")


def _sync_employes(db, rid):
    _sync_employes_from(db, rid, at.get_employes(rid, include_archived=True))

def _sync_employes_from(db, rid, rows):
    _clear_table(db, "employes", rid)
    for i, r in enumerate(rows):
        prenom = r.get("Prénom", r.get("Prenom", ""))
        nom = r.get("Nom", "")
        poste = r.get("Poste", "Salle")
        contrat = r.get("Type_contrat", "CDI")
        date_deb = r.get("Date_entrée", r.get("Date_entree", ""))
        date_fin = r.get("Date_fin", "") or ""
        salaire = r.get("Salaire_brut", 0) or 0
        heures = r.get("Heures_semaine", 35) or 35
        phone = r.get("Téléphone", r.get("Telephone", "")) or ""
        actif = r.get("Actif", True)
        statut = "Actif" if actif else "Inactif"
        ordre = r.get("Ordre", 0) or 0
        date_naissance = r.get("Date_naissance", "") or ""
        email = r.get("Email", "") or ""
        adresse = r.get("Adresse", "") or ""
        numero_secu = r.get("Numero_secu", "") or ""
        iban = r.get("IBAN", "") or ""
        cp_acquis = r.get("CP_acquis", 0) or 0
        solde_initial = r.get("Solde_Initial", 0) or 0
        date_debut_compteur = r.get("Date_Debut_Compteur", "") or ""

        db.execute("""INSERT INTO employes (restaurant_id,prenom,nom,poste,type_contrat,date_debut,date_fin,
                      salaire_brut,heures_semaine,phone,statut,ordre,date_naissance,
                      email,adresse,numero_secu,iban,cp_acquis,solde_initial,date_debut_compteur)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, prenom, nom, poste, contrat, date_deb, date_fin, salaire, heures, phone, statut, ordre, date_naissance,
                    email, adresse, numero_secu, iban, cp_acquis, solde_initial, date_debut_compteur))
    print(f"[SYNC] employes: {len(rows)} records")


def _sync_planning(db, rid):
    _sync_planning_from(db, rid, at.get_all("planning", rid), at.get_employes(rid, include_archived=True))

def _sync_planning_from(db, rid, rows, emp_at, absences_snapshot=None):
    _clear_table(db, "planning", rid)
    # Restaurer les absences locales (Repos/CP/AM/...) qui ne survivent pas à
    # Airtable (champ Shift_début refuse les codes courts). On les remappe par
    # (prenom, nom) car les IDs SQLite des employés ont changé après le sync.
    if absences_snapshot:
        name_to_id = {(e["prenom"], e["nom"]): e["id"]
                      for e in db.execute("SELECT id, prenom, nom FROM employes WHERE restaurant_id=?", (rid,)).fetchall()}
        for a in absences_snapshot:
            new_eid = name_to_id.get((a["prenom"], a["nom"]))
            if not new_eid:
                continue
            db.execute(
                "INSERT INTO planning (restaurant_id,employe_id,date,heure_debut,heure_fin,poste,shift_id,shift_nom,shift_couleur) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, new_eid, a["date"], a["heure_debut"], a["heure_fin"],
                 a["poste"], a["shift_id"] or 0, a["shift_nom"] or "", a["shift_couleur"] or "")
            )

    # Map Airtable employee IDs to SQLite IDs (by prenom+nom)
    emp_sqlite = db.execute("SELECT id, prenom, nom FROM employes ORDER BY id").fetchall()

    at_to_sqlite = {}
    sqlite_by_key = {}
    for e in emp_sqlite:
        sqlite_by_key[(e["prenom"], e["nom"])] = e["id"]
    for e in emp_at:
        prenom = e.get("Prénom", e.get("Prenom", ""))
        nom = e.get("Nom", "")
        at_id = e["id"]
        if (prenom, nom) in sqlite_by_key:
            at_to_sqlite[at_id] = sqlite_by_key[(prenom, nom)]

    n = 0
    for r in rows:
        emp_at_id = r.get("Employé_ID", r.get("Employe_ID", ""))
        sqlite_emp_id = at_to_sqlite.get(emp_at_id)
        if not sqlite_emp_id:
            continue
        date_val = r.get("Date", "")
        h_deb = r.get("Shift_début", r.get("Shift_debut", ""))
        h_fin = r.get("Shift_fin", "")
        poste = r.get("Poste", "Salle")
        shift_id = r.get("Shift_ID", "") or ""
        shift_nom = r.get("Shift_nom", "") or ""
        shift_couleur = r.get("Shift_couleur", "") or ""
        repas = 1 if r.get("Repas") else 0
        duree_repas = int(r.get("Duree Repas") or 0)

        db.execute("""INSERT INTO planning (restaurant_id,employe_id,date,heure_debut,heure_fin,poste,shift_id,shift_nom,shift_couleur,repas,duree_repas)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, sqlite_emp_id, date_val, h_deb, h_fin, poste,
                    0 if not shift_id else shift_id, shift_nom, shift_couleur, repas, duree_repas))
        n += 1
    print(f"[SYNC] planning: {n} entries")


def _sync_fiches(db, rid):
    _sync_fiches_from(db, rid, at.get_fiches(rid), at.get_all("ingredients", rid))

def _sync_fiches_from(db, rid, rows, all_ingredients):
    _clear_table(db, "fiches_techniques", rid)
    _clear_table(db, "fiche_ingredients", rid)
    _clear_table(db, "fiches_cocktails", rid)
    _clear_table(db, "cocktail_ingredients", rid)
    _clear_table(db, "boissons_bar", rid)

    # Index ingredients by Fiche_ID for O(1) lookup (eliminates N+1 API calls)
    ingr_by_fiche = {}
    for ing in all_ingredients:
        fid = ing.get("Fiche_ID", "")
        if fid:
            ingr_by_fiche.setdefault(fid, []).append(ing)

    n_food = n_cocktail = n_bar = 0

    for r in rows:
        ftype = r.get("Type", "Food")
        nom = r.get("Nom", "")
        cat = r.get("Categorie", "")
        pvht = r.get("Prix_vente_ht", 0) or 0
        pvttc = r.get("Prix_vente_ttc", 0) or 0
        cout = r.get("Cout_ht", 0) or 0
        coeff = r.get("Coefficient", 0) or 0
        ratio = r.get("Ratio_mp", 0) or 0
        marge = r.get("Marge_ht", 0) or 0
        statut = r.get("Statut", "actif") or "actif"
        at_id = r["id"]
        ingr = ingr_by_fiche.get(at_id, [])
        # New: parse Ingredients JSON field if present (authoritative)
        ing_json_raw = r.get("Ingredients", "") or ""
        ing_json = []
        if ing_json_raw:
            try:
                import json as _json
                ing_json = _json.loads(ing_json_raw) or []
            except Exception:
                ing_json = []

        if ftype == "Food":
            portions = r.get("Nb_portions", 0) or 0
            temps = r.get("Temps_prépa", r.get("Temps_prepa", 0)) or 0
            cur = db.execute("""INSERT INTO fiches_techniques (restaurant_id,nom,categorie,prix_vente_ht,prix_vente_ttc,
                                cout_ht,coefficient,ratio_mp,marge_ht,statut,nb_portions,temps_preparation)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (rid, nom, cat, pvht, pvttc, cout, coeff, ratio, marge, statut, portions, temps))
            fiche_sqlite_id = cur.lastrowid
            if ing_json:
                for ing in ing_json:
                    qty = float(ing.get("quantite", 0) or 0)
                    unite = ing.get("unite", "g")
                    gr = qty if unite in ("g", "kg") else 0
                    db.execute("""INSERT INTO fiche_ingredients (restaurant_id,fiche_id,produit,prix_kilo,grammes,cout_portion,unite,quantite,prix_litre)
                                  VALUES (?,?,?,?,?,?,?,?,?)""",
                               (rid, fiche_sqlite_id, ing.get("produit", ""),
                                float(ing.get("prix_kilo", 0) or 0), gr,
                                float(ing.get("cout_portion", 0) or 0),
                                unite, qty, float(ing.get("prix_litre", 0) or 0)))
            else:
                for ing in ingr:
                    db.execute("""INSERT INTO fiche_ingredients (restaurant_id,fiche_id,produit,prix_kilo,grammes,cout_portion)
                                  VALUES (?,?,?,?,?,?)""",
                               (rid, fiche_sqlite_id, ing.get("Produit", ""),
                                ing.get("Prix_kilo", 0) or 0, ing.get("Grammes", 0) or 0,
                                ing.get("Cout_portion", 0) or 0))
            n_food += 1

        elif ftype == "Cocktail":
            vol = r.get("Volume_cl", 0) or 0
            cur = db.execute("""INSERT INTO fiches_cocktails (restaurant_id,nom,categorie,volume_cl,prix_vente_ht,prix_vente_ttc,
                                cout_ht,coefficient,ratio_mp,marge_ht,statut)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                             (rid, nom, cat, vol, pvht, pvttc, cout, coeff, ratio, marge, statut))
            cocktail_sqlite_id = cur.lastrowid
            if ing_json:
                for ing in ing_json:
                    db.execute("""INSERT INTO cocktail_ingredients (restaurant_id,cocktail_id,produit,cout_ht_unitaire,
                                  qte_bouteille_cl,cout_ht_cl,qte_utilisee_cl,cout_ht_verre)
                                  VALUES (?,?,?,?,?,?,?,?)""",
                               (rid, cocktail_sqlite_id, ing.get("produit", ""),
                                float(ing.get("cout_ht_unitaire", 0) or 0),
                                float(ing.get("qte_bouteille_cl", 0) or 0),
                                float(ing.get("cout_ht_cl", 0) or 0),
                                float(ing.get("qte_utilisee_cl", 0) or 0),
                                float(ing.get("cout_ht_verre", 0) or 0)))
            else:
                for ing in ingr:
                    db.execute("""INSERT INTO cocktail_ingredients (restaurant_id,cocktail_id,produit,cout_ht_unitaire,
                                  qte_bouteille_cl,cout_ht_cl,qte_utilisee_cl,cout_ht_verre)
                                  VALUES (?,?,?,?,?,?,?,?)""",
                               (rid, cocktail_sqlite_id, ing.get("Produit", ""),
                                ing.get("Cout_ht_unitaire", 0) or 0, ing.get("Qte_bouteille_cl", 0) or 0,
                                ing.get("Cout_ht_cl", 0) or 0, ing.get("Qte_utilisee_cl", 0) or 0,
                                ing.get("Cout_ht_verre", 0) or 0))
            n_cocktail += 1

        elif ftype == "Boisson":
            cl = r.get("Contenance_cl", 0) or 0
            pa = r.get("Prix_achat_ht", 0) or 0
            coutcl = r.get("Cout_ht_cl", 0) or 0
            pc = r.get("Perte_casse", 0) or 0
            pd = r.get("Perte_degustation", 0) or 0
            pe = r.get("Perte_evaporation", 0) or 0
            ca = r.get("Cout_ajuste", 0) or 0
            db.execute("""INSERT INTO boissons_bar (restaurant_id,nom,categorie,contenance_cl,prix_achat_ht,prix_vente_ttc,
                          prix_vente_ht,cout_ht_cl,perte_casse,perte_degustation,perte_evaporation,
                          cout_ajuste,coefficient,ratio_mp,marge_ht,statut)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (rid, nom, cat, cl, pa, pvttc, pvht, coutcl, pc, pd, pe, ca, coeff, ratio, marge, statut))
            n_bar += 1

    print(f"[SYNC] fiches: {n_food} food, {n_cocktail} cocktails, {n_bar} bar")


def _sync_fournisseurs(db, rid):
    _sync_fournisseurs_from(db, rid, at.get_fournisseurs(rid))

def _sync_fournisseurs_from(db, rid, rows):
    _clear_table(db, "fournisseurs", rid)
    for r in rows:
        db.execute("""INSERT INTO fournisseurs (restaurant_id,nom,type,telephone,email,nom_commercial,
                      telephone_commercial,email_commercial,site_web,adresse,notes)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, r.get("Nom", ""), r.get("Type", "Autre"),
                    r.get("Telephone", "") or "", r.get("Email", "") or "",
                    r.get("Nom_commercial", "") or "", r.get("Telephone_commercial", "") or "",
                    r.get("Email_commercial", "") or "", r.get("Site_web", "") or "",
                    r.get("Adresse", "") or "", r.get("Notes", "") or ""))
    print(f"[SYNC] fournisseurs: {len(rows)} records")


def _sync_conges(db, rid):
    _sync_conges_from(db, rid, at.get_conges(rid), at.get_employes(rid, include_archived=True))

def _sync_conges_from(db, rid, rows, emp_at):
    _clear_table(db, "conges", rid)

    # Map employee Airtable IDs to SQLite IDs
    emp_sqlite = db.execute("SELECT id, prenom, nom FROM employes ORDER BY id").fetchall()
    at_to_sqlite = {}
    sqlite_by_key = {(e["prenom"], e["nom"]): e["id"] for e in emp_sqlite}
    for e in emp_at:
        prenom = e.get("Prénom", e.get("Prenom", ""))
        nom = e.get("Nom", "")
        if (prenom, nom) in sqlite_by_key:
            at_to_sqlite[e["id"]] = sqlite_by_key[(prenom, nom)]

    for r in rows:
        emp_at_id = r.get("Employe_ID", "")
        sqlite_id = at_to_sqlite.get(emp_at_id)
        if not sqlite_id:
            continue
        db.execute("""INSERT INTO conges (restaurant_id,employe_id,type,date_debut,date_fin,commentaire)
                      VALUES (?,?,?,?,?,?)""",
                   (rid, sqlite_id, r.get("Type", ""), r.get("Date_debut", ""),
                    r.get("Date_fin", ""), r.get("Commentaire", "") or ""))
    print(f"[SYNC] conges: {len(rows)} records")


def _sync_evenements(db, rid):
    _sync_evenements_from(db, rid, at.get_evenements(rid))

def _sync_evenements_from(db, rid, rows):
    _clear_table(db, "evenements", rid)
    for r in rows:
        db.execute("""INSERT INTO evenements (restaurant_id,titre,date,description,couleur,note)
                      VALUES (?,?,?,?,?,?)""",
                   (rid, r.get("Titre", ""), r.get("Date", ""),
                    r.get("Description", "") or "",
                    r.get("Couleur", "#2D6A4A") or "#2D6A4A",
                    r.get("Note", "") or ""))
    print(f"[SYNC] evenements: {len(rows)} records")


def _sync_fermetures(db, rid):
    _sync_fermetures_from(db, rid, at.get_fermetures(rid))

def _sync_fermetures_from(db, rid, rows):
    _clear_table(db, "fermetures", rid)
    for r in rows:
        db.execute("""INSERT INTO fermetures (restaurant_id,date,recurrence,jour_semaine)
                      VALUES (?,?,?,?)""",
                   (rid, r.get("Date", "") or "", r.get("Recurrence", 0) or 0,
                    r.get("Jour_semaine", -1) if r.get("Jour_semaine") is not None else -1))
    print(f"[SYNC] fermetures: {len(rows)} records")


def _sync_shifts(db, rid):
    _sync_shifts_from(db, rid, at.get_shifts(rid))

def _sync_shifts_from(db, rid, rows):
    _clear_table(db, "shifts", rid)
    for r in rows:
        db.execute("""INSERT INTO shifts (restaurant_id,nom,pole,heure_debut,heure_fin,couleur)
                      VALUES (?,?,?,?,?,?)""",
                   (rid, r.get("Nom", ""), r.get("Pole", "Salle"),
                    r.get("Heure_debut", "") or "", r.get("Heure_fin", "") or "",
                    r.get("Couleur", "#2D6A4A") or "#2D6A4A"))
    print(f"[SYNC] shifts: {len(rows)} records")


def _sync_postes(db, rid):
    _sync_postes_from(db, rid, at.get_postes(rid))

def _sync_postes_from(db, rid, rows):
    _clear_table(db, "postes", rid)
    for r in rows:
        try:
            db.execute("""INSERT INTO postes (restaurant_id,nom,pole,is_default) VALUES (?,?,?,?)""",
                       (rid, r.get("Nom", ""), r.get("Pole", "Salle"), r.get("Is_default", 0) or 0))
        except Exception:
            pass
    print(f"[SYNC] postes: {len(rows)} records")


def _sync_categories(db, rid):
    _sync_categories_from(db, rid, at.get_categories(rid))

def _sync_categories_from(db, rid, rows):
    _clear_table(db, "categories_fiches", rid)
    for r in rows:
        try:
            db.execute("""INSERT INTO categories_fiches (restaurant_id,type,nom,ordre) VALUES (?,?,?,?)""",
                       (rid, r.get("Type", "food"), r.get("Nom", ""), r.get("Ordre", 0) or 0))
        except Exception:
            pass
    print(f"[SYNC] categories: {len(rows)} records")


def _sync_messages(db, rid):
    _sync_messages_from(db, rid, at.get_messages(rid))

def _sync_messages_from(db, rid, rows):
    _clear_table(db, "messages", rid)
    for r in rows:
        db.execute("""INSERT INTO messages (restaurant_id,expediteur,destinataires,objet,message,date,lu)
                      VALUES (?,?,?,?,?,?,?)""",
                   (rid, r.get("From_user", "Gerant"), r.get("To_user", "tous"),
                    r.get("Subject", ""), r.get("Body", ""),
                    r.get("Created_at", "") or "", 1 if r.get("Lu") else 0))
    print(f"[SYNC] messages: {len(rows)} records")


def _sync_allergenes(db, rid):
    _sync_allergenes_from(db, rid, at.get_allergenes(rid))

def _sync_allergenes_from(db, rid, rows):
    _clear_table(db, "allergenes", rid)
    for r in rows:
        db.execute("""INSERT INTO allergenes (restaurant_id,nom_plat,categorie,gluten,crustaces,oeufs,poisson,
                      arachides,soja,lait,fruits_a_coque,celeri,moutarde,sesame,sulfites,lupin,
                      mollusques,autres,airtable_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, r.get("Nom_plat", ""), r.get("Categorie", "Plat"),
                    1 if r.get("Gluten") else 0, 1 if r.get("Crustaces") else 0,
                    1 if r.get("Oeufs") else 0, 1 if r.get("Poisson") else 0,
                    1 if r.get("Arachides") else 0, 1 if r.get("Soja") else 0,
                    1 if r.get("Lait") else 0, 1 if r.get("Fruits_a_coque") else 0,
                    1 if r.get("Celeri") else 0, 1 if r.get("Moutarde") else 0,
                    1 if r.get("Sesame") else 0, 1 if r.get("Sulfites") else 0,
                    1 if r.get("Lupin") else 0, 1 if r.get("Mollusques") else 0,
                    r.get("Autres", "") or "", r["id"]))
    print(f"[SYNC] allergenes: {len(rows)} records")


# ── Demo preload ────────────────────────────────────────────────────────

_DEMO_RID = "bistrot_du_port"

def preload_demo():
    """Warm cache + SQLite for the demo restaurant at startup (background thread)."""
    def _run():
        try:
            print(f"[SYNC] Preloading demo restaurant '{_DEMO_RID}'...")
            sync_restaurant(_DEMO_RID)
            print(f"[SYNC] Demo preload complete")
        except Exception as e:
            print(f"[SYNC] Demo preload error: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ══════════════════════════════════════════════════════════════════════════
#  DUAL-WRITE HELPERS — call these instead of raw SQLite for mutations
# ══════════════════════════════════════════════════════════════════════════

def _get_rid():
    """Get current restaurant ID from Flask context."""
    try:
        from flask_login import current_user
        if current_user.is_authenticated:
            return current_user.restaurant_id
    except Exception:
        pass
    return ""


def _is_demo():
    """Check if current user is in demo mode (no Airtable writes)."""
    try:
        from flask_login import current_user
        if current_user.is_authenticated:
            return getattr(current_user, "demo_mode", False)
    except Exception:
        pass
    return False


# --- CA Jour ---

def write_ca_jour(db, date_str, ca, ca_resto, ca_bar, midi, soir, tm, comm=""):
    """Write CA to both SQLite and Airtable."""
    rid = _get_rid()
    db.execute("""INSERT OR REPLACE INTO ca_jour (restaurant_id,date,ca,ca_restaurant,ca_bar,couverts_midi,couverts_soir,
                  tickets_bar,ticket_moyen,ticket_moyen_bar,commentaire) VALUES (?,?,?,?,?,?,?,0,?,0,?)""",
               (rid, date_str, ca, ca_resto, ca_bar, midi, soir, tm, comm))
    db.commit()
    if rid and not _is_demo():
        try:
            at.upsert_ca_jour(rid, date_str, ca, ca_resto, ca_bar, midi, soir, tm, comm)
        except Exception as e:
            print(f"[SYNC] Airtable write error (ca_jour): {e}")


# --- Depenses ---

def write_depense_fixe(db, cat, desc, montant, mois, recurrente=0):
    rid = _get_rid()
    db.execute("INSERT INTO depenses_fixes (restaurant_id,categorie,description,montant,mois,recurrente) VALUES (?,?,?,?,?,?)",
               (rid, cat, desc, montant, mois, recurrente))
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_depense_fixe(rid, cat, desc, montant, mois, bool(recurrente))
        except Exception as e:
            print(f"[SYNC] Airtable write error (depense_fixe): {e}")


def write_depense_variable(db, date_str, cat, desc, montant, mois):
    rid = _get_rid()
    # Toujours dériver mois depuis date pour cohérence entre requêtes date et mois
    mois_val = date_str[:7] if date_str and len(date_str) >= 7 else mois
    db.execute("INSERT INTO depenses_variables (restaurant_id,date,categorie,description,montant,type,mois) VALUES (?,?,?,?,?,'variable',?)",
               (rid, date_str, cat, desc, montant, mois_val))
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_depense_variable(rid, date_str, cat, desc, montant, mois)
        except Exception as e:
            print(f"[SYNC] Airtable write error (depense_var): {e}")


# --- Employes ---

def _infer_pole(poste_name):
    """Infer pole from poste name using common keywords.
    Order matters: Salle checked first (Chef de Rang, Commis de Salle)."""
    p = poste_name.lower()
    # Salle — check FIRST (more specific: "rang", "salle", "maitre")
    salle_kw = ["rang", "salle", "serveur", "serveuse", "runner",
                "maitre", "ma\u00eetre", "h\u00f4te", "hotesse", "petit d\u00e9jeuner"]
    if any(k in p for k in salle_kw):
        return "Salle"
    # Bar
    bar_kw = ["barman", "barmaid", "bar", "sommelier", "mixolog"]
    if any(k in p for k in bar_kw):
        return "Bar"
    # Cuisine
    cuisine_kw = ["chef", "cuisinier", "cuisiniere", "commis", "patissier",
                  "p\u00e2tissier", "plongeur", "cuisine", "boulanger", "traiteur",
                  "second", "sous-chef", "apprenti"]
    if any(k in p for k in cuisine_kw):
        return "Cuisine"
    return "Salle"


def write_employe(db, prenom, nom, poste, contrat, deb, fin, salaire, heures, phone, date_naissance=""):
    rid = _get_rid()
    db.execute("""INSERT INTO employes (restaurant_id,prenom,nom,poste,type_contrat,date_debut,date_fin,
                  salaire_brut,heures_semaine,phone,date_naissance,statut) VALUES (?,?,?,?,?,?,?,?,?,?,?,'Actif')""",
               (rid, prenom, nom, poste, contrat, deb, fin, salaire, heures, phone, date_naissance or ""))
    db.commit()
    if rid and not _is_demo():
        try:
            # Determine pole: first from local postes table, then from Airtable, then heuristic
            pole = None
            row = db.execute("SELECT pole FROM postes WHERE nom=?", (poste,)).fetchone()
            if row:
                pole = row["pole"]
            else:
                postes = at.get_postes(rid)
                for p in postes:
                    if p.get("Nom") == poste:
                        pole = p.get("Pole", None)
                        break
            if not pole:
                pole = _infer_pole(poste)
            # Auto-create poste if it doesn't exist locally (so planning grid works)
            if not row:
                try:
                    db.execute("INSERT OR IGNORE INTO postes (restaurant_id, nom, pole) VALUES (?, ?, ?)", (rid, poste, pole))
                    db.commit()
                except Exception:
                    pass
            at.create_employe(rid, prenom, nom, poste, contrat, deb, salaire, heures, phone, pole, date_fin=fin or None, date_naissance=date_naissance or None)
        except Exception as e:
            print(f"[SYNC] Airtable write error (employe): {e}")


# --- Planning ---

def write_planning(db, emp_id, date_str, h_deb, h_fin, poste, shift_id=0, shift_nom="", shift_couleur="", repas=False, duree_repas=0):
    rid = _get_rid()
    db.execute("""INSERT INTO planning (restaurant_id,employe_id,date,heure_debut,heure_fin,poste,shift_id,shift_nom,shift_couleur,repas,duree_repas)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (rid, emp_id, date_str, h_deb, h_fin, poste, shift_id, shift_nom, shift_couleur, 1 if repas else 0, int(duree_repas or 0)))
    db.commit()
    print(f"[PLANNING-WRITE] sqlite OK rid={rid} emp_id={emp_id} date={date_str} h_deb={h_deb!r} h_fin={h_fin!r} repas={repas} duree={duree_repas}")
    # Les champs Shift_début/Shift_fin Airtable sont singleLineText : ils acceptent
    # les codes courts type "R"/"CP" → on push tout vers Airtable, absences incluses.
    if rid and not _is_demo():
        try:
            emp_row = db.execute("SELECT prenom, nom FROM employes WHERE id=?", (emp_id,)).fetchone()
            if emp_row:
                emp_at_list = at.get_employes(rid, include_archived=True)
                for e in emp_at_list:
                    if (e.get("Prénom", e.get("Prenom", "")) == emp_row["prenom"]
                            and e.get("Nom", "") == emp_row["nom"]):
                        print(f"[PLANNING-WRITE] -> Airtable create Employé_ID={e['id']} Date={date_str} Shift_début={h_deb!r} Shift_fin={h_fin!r} Poste={poste!r}")
                        created = at.create_planning_entry(rid, e["id"], date_str, h_deb, h_fin, poste or "",
                                                 str(shift_id or ""), shift_nom or "", shift_couleur or "",
                                                 repas=bool(repas), duree_repas=int(duree_repas or 0))
                        print(f"[PLANNING-WRITE] Airtable OK id={created.get('id') if created else None}")
                        break
                else:
                    print(f"[PLANNING-WRITE] Aucun employé Airtable trouvé pour {emp_row['prenom']} {emp_row['nom']}")
        except Exception as ex:
            print(f"[SYNC] Airtable write error (planning): {ex}")
            import traceback
            traceback.print_exc()


# --- Fiches ---

def write_fiche_food(db, nom, cat, pvht, pvttc, cout, coeff, ratio, marge, portions=0, temps=0, fiche_id=None, ingredients=None):
    rid = _get_rid()
    if fiche_id:
        db.execute("""UPDATE fiches_techniques SET nom=?,categorie=?,prix_vente_ht=?,prix_vente_ttc=?,
                      cout_ht=?,coefficient=?,ratio_mp=?,marge_ht=?,nb_portions=?,temps_preparation=? WHERE id=?""",
                   (nom, cat, pvht, pvttc, cout, coeff, ratio, marge, portions, temps, fiche_id))
    else:
        cur = db.execute("""INSERT INTO fiches_techniques (restaurant_id,nom,categorie,prix_vente_ht,prix_vente_ttc,
                            cout_ht,coefficient,ratio_mp,marge_ht,nb_portions,temps_preparation) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (rid, nom, cat, pvht, pvttc, cout, coeff, ratio, marge, portions, temps))
        fiche_id = cur.lastrowid
    # Ingredients
    if ingredients is not None:
        db.execute("DELETE FROM fiche_ingredients WHERE fiche_id=?", (fiche_id,))
        for i in ingredients:
            db.execute("INSERT INTO fiche_ingredients (restaurant_id,fiche_id,produit,prix_kilo,grammes,cout_portion) VALUES (?,?,?,?,?,?)",
                       (rid, fiche_id, i.get("produit", ""), float(i.get("prix_kilo", 0)),
                        float(i.get("grammes", 0)), float(i.get("cout_portion", 0))))
    db.commit()

    if rid and not _is_demo():
        try:
            at.create_fiche(rid, "Food", nom, cat, pvht, pvttc, cout, coeff, ratio, marge,
                            nb_portions=portions, temps_prepa=temps)
        except Exception as e:
            print(f"[SYNC] Airtable write error (fiche_food): {e}")
    return fiche_id


# --- Evenements ---

def write_evenement(db, titre, date_str, couleur="#2D6A4A", note=""):
    rid = _get_rid()
    db.execute("INSERT INTO evenements (restaurant_id,titre,date,couleur,note) VALUES (?,?,?,?,?)",
               (rid, titre, date_str, couleur, note))
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_evenement(rid, titre, date_str, couleur, note)
        except Exception as e:
            print(f"[SYNC] Airtable write error (evenement): {e}")


# --- Fermetures ---

def write_fermeture(db, recurrence, jour_semaine=-1, date_str=""):
    rid = _get_rid()
    if recurrence:
        db.execute("INSERT INTO fermetures (restaurant_id,recurrence,jour_semaine) VALUES (?,1,?)", (rid, jour_semaine,))
    else:
        db.execute("INSERT INTO fermetures (restaurant_id,date,recurrence,jour_semaine) VALUES (?,?,0,-1)", (rid, date_str,))
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_fermeture(rid, recurrence=bool(recurrence), jour_semaine=jour_semaine, date_str=date_str)
        except Exception as e:
            print(f"[SYNC] Airtable write error (fermeture): {e}")


# --- Messages ---

def write_message(db, expediteur, destinataires, objet, message, date_str):
    rid = _get_rid()
    db.execute("INSERT INTO messages (restaurant_id,expediteur,destinataires,objet,message,date,lu) VALUES (?,?,?,?,?,?,0)",
               (rid, expediteur, destinataires, objet, message, date_str))
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_message(rid, expediteur, destinataires, objet, message, date_str)
        except Exception as e:
            print(f"[SYNC] Airtable write error (message): {e}")


# --- Fournisseurs ---

def write_fournisseur(db, nom, ftype="Autre", **kwargs):
    rid = _get_rid()
    try:
        db.execute("""INSERT INTO fournisseurs (restaurant_id,nom,type,telephone,email,nom_commercial,
                      telephone_commercial,email_commercial,site_web,adresse,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (rid, nom, ftype, kwargs.get("telephone", ""), kwargs.get("email", ""),
                    kwargs.get("nom_commercial", ""), kwargs.get("telephone_commercial", ""),
                    kwargs.get("email_commercial", ""), kwargs.get("site_web", ""),
                    kwargs.get("adresse", ""), kwargs.get("notes", "")))
    except Exception:
        pass
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_fournisseur(rid, nom, ftype, **kwargs)
        except Exception as e:
            print(f"[SYNC] Airtable write error (fournisseur): {e}")


# --- Shifts ---

def write_shift(db, nom, pole, h_deb, h_fin, couleur):
    rid = _get_rid()
    db.execute("INSERT INTO shifts (restaurant_id,nom,pole,heure_debut,heure_fin,couleur) VALUES (?,?,?,?,?,?)",
               (rid, nom, pole, h_deb, h_fin, couleur))
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_shift(rid, nom, pole, h_deb, h_fin, couleur)
        except Exception as e:
            print(f"[SYNC] Airtable write error (shift): {e}")


def update_shift(db, shift_id):
    """Re-sync un shift modifié vers Airtable (delete + recreate)."""
    row = db.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not row:
        return
    rid = _get_rid()
    if rid and not _is_demo():
        try:
            nom_old = row["nom"]
            _find_and_delete_airtable("shifts",
                f"AND({{Restaurant_ID}}='{rid}',{{Nom}}='{nom_old}')")
        except Exception:
            pass
        try:
            at.create_shift(rid, row["nom"], row["pole"],
                            row["heure_debut"], row["heure_fin"], row["couleur"])
        except Exception as e:
            print(f"[SYNC] Airtable update error (shift): {e}")


# --- Postes ---

def write_poste(db, nom, pole):
    rid = _get_rid()
    # Vérifie d'abord si le poste existe déjà pour ce restaurant (idempotence)
    existing = db.execute("SELECT id FROM postes WHERE restaurant_id=? AND nom=? AND pole=?", (rid, nom, pole)).fetchone()
    if not existing:
        try:
            db.execute("INSERT INTO postes (restaurant_id,nom,pole) VALUES (?,?,?)", (rid, nom, pole))
        except Exception as e:
            print(f"[SYNC] write_poste local INSERT error rid={rid} nom={nom!r} pole={pole!r}: {e}")
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_poste(rid, nom, pole)
        except Exception as e:
            print(f"[SYNC] Airtable write error (poste): {e}")


# --- Categories ---

def write_categorie(db, type_cat, nom):
    rid = _get_rid()
    try:
        db.execute("INSERT INTO categories_fiches (restaurant_id,type,nom) VALUES (?,?,?)", (rid, type_cat, nom))
    except Exception:
        pass
    db.commit()
    if rid and not _is_demo():
        try:
            at.create_categorie(rid, type_cat, nom)
        except Exception as e:
            print(f"[SYNC] Airtable write error (categorie): {e}")


# --- Conges ---

def write_conge(db, emp_id, type_c, d1, d2, comm=""):
    rid = _get_rid()
    db.execute("INSERT INTO conges (restaurant_id,employe_id,type,date_debut,date_fin,commentaire) VALUES (?,?,?,?,?,?)",
               (rid, emp_id, type_c, d1, d2, comm))
    db.commit()
    if rid and not _is_demo():
        try:
            emp_row = db.execute("SELECT prenom FROM employes WHERE id=?", (emp_id,)).fetchone()
            if emp_row:
                emp_at_list = at.get_employes(rid, include_archived=True)
                for e in emp_at_list:
                    if e.get("Prénom", e.get("Prenom", "")) == emp_row["prenom"]:
                        at.create_conge(rid, e["id"], type_c, d1, d2, comm)
                        break
        except Exception as e:
            print(f"[SYNC] Airtable write error (conge): {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  DELETE helpers — supprime en local ET dans Airtable
# ═══════════════════════════════════════════════════════════════════════════

def _find_and_delete_airtable(table, formula):
    """Find a record by formula in Airtable and delete it. Returns True on success."""
    try:
        rec = at.find_first_nocache(table, formula)
        if rec:
            at.delete(table, rec["id"])
            return True
    except Exception as e:
        print(f"[SYNC] Airtable delete error ({table}): {e}")
    return False


def delete_poste(db, poste_id):
    """Supprime un poste en local et dans Airtable."""
    row = db.execute("SELECT nom, pole FROM postes WHERE id=?", (poste_id,)).fetchone()
    db.execute("DELETE FROM postes WHERE id=?", (poste_id,))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and row:
        nom = row["nom"]
        _find_and_delete_airtable("postes",
            f"AND({{Restaurant_ID}}='{rid}',{{Nom}}='{nom}')")


def delete_shift(db, shift_id):
    """Supprime un shift en local et dans Airtable."""
    row = db.execute("SELECT nom, pole FROM shifts WHERE id=?", (shift_id,)).fetchone()
    db.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and row:
        nom = row["nom"]
        _find_and_delete_airtable("shifts",
            f"AND({{Restaurant_ID}}='{rid}',{{Nom}}='{nom}')")


def delete_evenement(db, evt_id):
    """Supprime un evenement en local et dans Airtable."""
    row = db.execute("SELECT titre, date FROM evenements WHERE id=?", (evt_id,)).fetchone()
    db.execute("DELETE FROM evenements WHERE id=?", (evt_id,))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and row:
        titre = row["titre"].replace("'", "\\'")
        d = row["date"]
        # DATESTR() : Airtable renvoie les dates en ISO datetime, comparaison string-équivalente impossible sans
        _find_and_delete_airtable("evenements",
            f"AND({{Restaurant_ID}}='{rid}',{{Titre}}='{titre}',DATESTR({{Date}})='{d}')")


def delete_fermeture(db, ferm_id):
    """Supprime une fermeture en local et dans Airtable."""
    row = db.execute("SELECT date, recurrence, jour_semaine FROM fermetures WHERE id=?", (ferm_id,)).fetchone()
    db.execute("DELETE FROM fermetures WHERE id=?", (ferm_id,))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and row:
        if row["recurrence"]:
            _find_and_delete_airtable("fermetures",
                f"AND({{Restaurant_ID}}='{rid}',{{Recurrence}}=1,{{Jour_semaine}}={row['jour_semaine']})")
        elif row["date"]:
            _find_and_delete_airtable("fermetures",
                f"AND({{Restaurant_ID}}='{rid}',DATESTR({{Date}})='{row['date']}')")


def delete_conge(db, conge_id):
    """Supprime un conge en local et dans Airtable."""
    row = db.execute("SELECT employe_id, date_debut, date_fin FROM conges WHERE id=?", (conge_id,)).fetchone()
    db.execute("DELETE FROM conges WHERE id=?", (conge_id,))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and row:
        d1, d2 = row["date_debut"], row["date_fin"]
        _find_and_delete_airtable("conges",
            f"AND({{Restaurant_ID}}='{rid}',{{Date_debut}}='{d1}',{{Date_fin}}='{d2}')")


def update_fournisseur(db, fournisseur_id, nom, ftype="Autre", **kwargs):
    """Met a jour un fournisseur en local et dans Airtable."""
    # Recuperer l'ancien nom pour retrouver le record Airtable
    old = db.execute("SELECT nom FROM fournisseurs WHERE id=?", (fournisseur_id,)).fetchone()
    db.execute("""UPDATE fournisseurs SET nom=?, type=?, telephone=?, email=?, nom_commercial=?,
                  telephone_commercial=?, email_commercial=?, site_web=?, adresse=?, notes=?
                  WHERE id=?""",
               (nom, ftype, kwargs.get("telephone", ""), kwargs.get("email", ""),
                kwargs.get("nom_commercial", ""), kwargs.get("telephone_commercial", ""),
                kwargs.get("email_commercial", ""), kwargs.get("site_web", ""),
                kwargs.get("adresse", ""), kwargs.get("notes", ""), fournisseur_id))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and old:
        try:
            old_nom = old["nom"].replace("'", "\\'")
            rec = at.find_first("fournisseurs",
                f"AND({{Restaurant_ID}}='{rid}',{{Nom}}='{old_nom}')")
            if rec:
                at.update_fournisseur(rec["id"],
                    nom=nom, type=ftype, **kwargs)
        except Exception as e:
            print(f"[SYNC] Airtable update error (fournisseur): {e}")


def delete_fournisseur(db, fournisseur_id):
    """Supprime un fournisseur en local et dans Airtable."""
    row = db.execute("SELECT nom FROM fournisseurs WHERE id=?", (fournisseur_id,)).fetchone()
    db.execute("DELETE FROM fournisseurs WHERE id=?", (fournisseur_id,))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and row:
        nom = row["nom"].replace("'", "\\'")
        _find_and_delete_airtable("fournisseurs",
            f"AND({{Restaurant_ID}}='{rid}',{{Nom}}='{nom}')")


def delete_categorie(db, type_cat, nom):
    """Supprime une categorie en local et dans Airtable."""
    db.execute("DELETE FROM categories_fiches WHERE type=? AND nom=?", (type_cat, nom))
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo():
        _find_and_delete_airtable("categories_fiches",
            f"AND({{Restaurant_ID}}='{rid}',{{Type}}='{type_cat}',{{Nom}}='{nom}')")


def delete_depense(db, table_local, depense_id):
    """Supprime une depense (fixe ou variable) en local et dans Airtable."""
    if table_local == "fixe":
        row = db.execute("SELECT categorie, description, mois FROM depenses_fixes WHERE id=?", (depense_id,)).fetchone()
        db.execute("DELETE FROM depenses_fixes WHERE id=?", (depense_id,))
    elif table_local == "variable":
        row = db.execute("SELECT categorie, description, mois FROM depenses_variables WHERE id=?", (depense_id,)).fetchone()
        db.execute("DELETE FROM depenses_variables WHERE id=?", (depense_id,))
    else:
        row = None
    db.commit()
    rid = _get_rid()
    if rid and not _is_demo() and row:
        desc = row["description"].replace("'", "\\'")
        mois = row["mois"]
        _find_and_delete_airtable("depenses",
            f"AND({{Restaurant_ID}}='{rid}',{{Description}}='{desc}',{{Mois}}='{mois}')")
