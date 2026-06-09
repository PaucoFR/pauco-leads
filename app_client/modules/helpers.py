# -*- coding: utf-8 -*-
"""Fonctions utilitaires métier — helpers RH, dates, stats mensuelles, seuil de rentabilité."""

from datetime import date
from flask_login import current_user

MOIS_NOMS = ["Janvier", "Fevrier", "Mars", "Avril", "Mai", "Juin",
             "Juillet", "Aout", "Septembre", "Octobre", "Novembre", "Decembre"]


def _rid():
    """Return current user's restaurant_id for multi-tenant filtering."""
    try:
        if current_user.is_authenticated:
            return current_user.restaurant_id or ""
    except Exception:
        pass
    return ""


def _solde_cp(db, emp_id, cp_acquis=0, date_debut=""):
    """Calcule le solde CP : acquis - pris. Si cp_acquis=0, estime depuis date_debut."""
    if not cp_acquis and date_debut:
        try:
            from datetime import datetime
            start = datetime.strptime(date_debut[:10], "%Y-%m-%d")
            months = (date.today().year - start.year) * 12 + date.today().month - start.month
            cp_acquis = round(min(months * 2.5, 30), 1)  # Cap 30 jours (1 an + report)
        except Exception:
            cp_acquis = 25  # Fallback
    # Count CP taken: from conges table + planning table (heure_debut='CP')
    cp_conges = db.execute("""SELECT COUNT(*) as c FROM conges
                              WHERE restaurant_id=? AND employe_id=? AND type LIKE '%pay%'""", (_rid(), emp_id,)).fetchone()["c"]
    cp_planning = db.execute("""SELECT COUNT(*) as c FROM planning
                                WHERE restaurant_id=? AND employe_id=? AND heure_debut='CP'""", (_rid(), emp_id,)).fetchone()["c"]
    cp_pris = max(cp_conges, cp_planning)  # Avoid double-counting
    return round(cp_acquis - cp_pris, 1)


def _compteur_total(db, emp_id, heures_contrat, date_debut, solde_initial=0, date_debut_compteur=""):
    """Solde heures total depuis date creation Pauco : heures planifiees - heures contractuelles + solde initial."""
    ref = date_debut_compteur or date_debut or "2026-01-01"
    today = date.today()
    rows = db.execute("""SELECT heure_debut, heure_fin, repas, duree_repas FROM planning
                         WHERE restaurant_id=? AND employe_id=? AND date>=? AND date<=?""",
                      (_rid(), emp_id, ref, today.isoformat())).fetchall()
    heures = 0
    for r in rows:
        try:
            hd = int(r["heure_debut"].split(":")[0]) + int(r["heure_debut"].split(":")[1]) / 60
            hf = int(r["heure_fin"].split(":")[0]) + int(r["heure_fin"].split(":")[1]) / 60
            base = max(hf - hd, 0)
            if r["repas"]:
                base = max(base - (int(r["duree_repas"] or 0) / 60.0), 0)
            heures += base
        except Exception:
            pass
    try:
        d_ref = date.fromisoformat(ref[:10])
        nb_sem = max((today - d_ref).days / 7, 0.1)
    except Exception:
        nb_sem = 1
    contrat = heures_contrat * nb_sem
    return round(heures - contrat + (solde_initial or 0), 1)


def _mois_label(mois_str):
    """'2026-03' -> 'Mars 2026'"""
    try:
        y, m = mois_str.split("-")
        return f"{MOIS_NOMS[int(m)-1]} {y}"
    except Exception:
        return mois_str


def _mois_nom(mois_num):
    """3 -> 'Mars'"""
    return MOIS_NOMS[mois_num - 1]


def _mois_courant():
    return date.today().strftime("%Y-%m")


def _get_mois_list():
    """Retourne les 12 derniers mois sous forme de list."""
    today = date.today()
    result = []
    for i in range(12):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        val = f"{y:04d}-{m:02d}"
        result.append({"value": val, "label": _mois_label(val)})
    return result


def _stats_mois(db, mois):
    """Stats agglomerees pour un mois donne."""
    rows = db.execute("SELECT * FROM ca_jour WHERE restaurant_id=? AND date LIKE ? ORDER BY date", (_rid(), mois + "%",)).fetchall()
    if not rows:
        return {"ca_total": 0, "ca_restaurant": 0, "ca_bar": 0, "nb_jours": 0,
                "couverts_total": 0, "ticket_moyen": 0, "ticket_moyen_bar": 0,
                "ca_moyen": 0, "first_date": None, "last_date": None, "ratio_bar": 0}
    ca_total = sum(r["ca"] for r in rows)
    ca_resto = sum((r["ca_restaurant"] if r["ca_restaurant"] else r["ca"]) for r in rows)
    ca_bar = sum((r["ca_bar"] if r["ca_bar"] else 0) for r in rows)
    nb_jours = len(rows)
    couverts_total = sum(r["couverts_midi"] + r["couverts_soir"] for r in rows)
    tickets_bar_total = sum((r["tickets_bar"] if r["tickets_bar"] else 0) for r in rows)
    tm_resto = round(ca_resto / couverts_total, 2) if couverts_total > 0 else 0
    tm_bar = round(ca_bar / tickets_bar_total, 2) if tickets_bar_total > 0 else 0
    ratio_bar = round(ca_bar / ca_total * 100, 1) if ca_total > 0 else 0
    return {
        "ca_total": round(ca_total, 2),
        "ca_restaurant": round(ca_resto, 2),
        "ca_bar": round(ca_bar, 2),
        "nb_jours": nb_jours,
        "couverts_total": couverts_total,
        "ticket_moyen": tm_resto,
        "ticket_moyen_bar": tm_bar,
        "ca_moyen": round(ca_total / nb_jours, 2) if nb_jours > 0 else 0,
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "ratio_bar": ratio_bar,
    }


def _depenses_mois(db, mois):
    """Total depenses par grande categorie pour un mois."""
    fixes_row = db.execute("SELECT SUM(montant) as t FROM depenses_fixes WHERE restaurant_id=? AND mois = ?", (_rid(), mois,)).fetchone()
    total_fixes = fixes_row["t"] or 0
    # Add recurring fixes from previous months
    recurrentes = db.execute("SELECT * FROM depenses_fixes WHERE restaurant_id=? AND recurrente = 1 AND mois < ?", (_rid(), mois,)).fetchall()
    direct_keys = set()
    for r in db.execute("SELECT categorie, description FROM depenses_fixes WHERE restaurant_id=? AND mois = ?", (_rid(), mois,)).fetchall():
        direct_keys.add((r["categorie"], r["description"]))
    for r in recurrentes:
        if (r["categorie"], r["description"]) not in direct_keys:
            desact = (r["desactivee_mois"] or "").split(",") if r["desactivee_mois"] else []
            if mois not in desact:
                total_fixes += r["montant"]
    vars_row = db.execute("SELECT SUM(montant) as t FROM depenses_variables WHERE restaurant_id=? AND mois = ?", (_rid(), mois,)).fetchone()
    total_vars = vars_row["t"] or 0

    # Achats & Matières : tout sauf personnel (inclut ex-charges opérationnelles)
    food_cats = "('Food','Matières premières','Emballages','Autre achats','Hygiène & Entretien','Entretien','Énergie','Réparations','Reparation','Autre charges opé','Fournitures','Marketing','Livraison')"
    bev_cats = "('Boissons')"
    personnel_cats = "('Salaires','Extras & Intérim','Extras / Interim','Formation','Autre personnel','Charges sociales')"

    def _sum_cats(cats):
        r = db.execute(f"SELECT SUM(montant) as t FROM depenses_variables WHERE restaurant_id=? AND mois = ? AND categorie IN {cats}", (_rid(), mois,)).fetchone()
        return round(r["t"] or 0, 2)

    food = _sum_cats(food_cats)
    beverage = _sum_cats(bev_cats)
    personnel = _sum_cats(personnel_cats)

    return {
        "total_fixes": round(total_fixes, 2),
        "total_variables": round(total_vars, 2),
        "total": round(total_fixes + total_vars, 2),
        "food": food,
        "beverage": beverage,
        "personnel": personnel,
        "charges_ope": 0,
    }


def _seuil_rentabilite(db, mois):
    """Calcule le seuil de rentabilite et la progression."""
    dep = _depenses_mois(db, mois)
    stats = _stats_mois(db, mois)
    ca = stats["ca_total"]

    # Marge brute = CA - depenses variables (matieres)
    # Taux de marge brute = (CA - variables) / CA
    marge_brute_pct = (ca - dep["total_variables"]) / ca if ca > 0 else 0.65

    # Seuil = charges fixes / taux de marge brute
    if marge_brute_pct > 0:
        seuil = round(dep["total_fixes"] / marge_brute_pct, 2)
    else:
        seuil = round(dep["total_fixes"] / 0.65, 2)  # fallback 65% marge

    if seuil <= 0:
        return {"seuil": 0, "ca": ca, "pct": 0, "manque": 0, "atteint": False, "date_atteint": None}

    pct = round(ca / seuil * 100, 1) if seuil > 0 else 0
    atteint = ca >= seuil
    date_atteint = None

    if atteint and stats["first_date"]:
        # Trouver la date ou le seuil a ete atteint
        cumul = 0
        rows = db.execute("SELECT date, ca FROM ca_jour WHERE restaurant_id=? AND date LIKE ? ORDER BY date", (_rid(), mois + "%",)).fetchall()
        for r in rows:
            cumul += r["ca"]
            if cumul >= seuil:
                d = r["date"]
                try:
                    date_atteint = f"{d[8:10]}/{d[5:7]}"
                except Exception:
                    date_atteint = d
                break

    manque = round(max(seuil - ca, 0), 2)
    return {"seuil": seuil, "ca": ca, "pct": min(pct, 100), "manque": manque, "atteint": atteint, "date_atteint": date_atteint}


def _meilleur_mois_historique(db, mois):
    """Trouve le meilleur mois identique (meme mois, annee differente) dans l'historique."""
    try:
        mois_num = int(mois.split("-")[1])
    except (ValueError, IndexError):
        return None

    # Chercher tous les mois identiques dans l'historique
    rows = db.execute("""
        SELECT substr(date, 1, 7) as m, SUM(ca) as total_ca
        FROM ca_jour
        WHERE substr(date, 6, 2) = ?
        GROUP BY m
        ORDER BY total_ca DESC
    """, (f"{mois_num:02d}",)).fetchall()

    if not rows:
        return None

    meilleur = rows[0]
    meilleur_annee = meilleur["m"][:4]
    meilleur_ca = round(meilleur["total_ca"], 2)

    # Stats du mois selectionne
    stats = _stats_mois(db, mois)
    ca_actuel = stats["ca_total"]

    if meilleur["m"] == mois:
        # C'est le mois courant lui-meme → chercher le 2e meilleur
        if len(rows) > 1:
            meilleur = rows[1]
            meilleur_annee = meilleur["m"][:4]
            meilleur_ca = round(meilleur["total_ca"], 2)
        else:
            return None

    if meilleur_ca <= 0:
        return None

    pct = round(ca_actuel / meilleur_ca * 100, 1) if meilleur_ca > 0 else 0
    mois_nom = _mois_nom(mois_num)

    return {
        "mois_nom": mois_nom,
        "annee_record": meilleur_annee,
        "ca_record": meilleur_ca,
        "ca_actuel": ca_actuel,
        "pct": pct,
    }
