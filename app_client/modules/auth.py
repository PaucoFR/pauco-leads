"""Auth module — login, user management, password reset.
Users and restaurants in Airtable. SQLite only for password_resets and Flask sessions.

PROTECTION: jamais de DELETE sur users ou restaurants.
Suppression = soft delete (Actif=0).
"""

import os
import time
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import redirect, url_for, session, request
from flask_login import LoginManager, UserMixin, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from . import airtable_client as at

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = ""

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gestion.db")


def _get_auth_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


ALL_PERMISSIONS = [
    "dashboard", "saisie_ca", "depenses", "fiches", "ratios",
    "planning", "equipe",
    "avis_overview", "avis_liste", "avis_stats",
    "fiches_techniques", "allergenes", "fiches_bar",
    "calendrier", "messagerie", "ressources", "reglages",
    "hygiene",
    # Hygiène HACCP — sous-modules
    "haccp_etiquettes", "haccp_releves", "haccp_receptions",
    "haccp_viandes_origine", "haccp_viandes_tracabilite", "haccp_checklists",
    # Marketing
    "marketing_perf_ads", "marketing_rapports_ads", "marketing_stats_reseaux", "marketing_shooting",
    # Recrutement
    "recrut_offres", "recrut_candidatures", "recrut_vivier",
]

# Fallback presets when Roles table has no data yet
_FALLBACK_PRESETS = {
    "Gerant": set(ALL_PERMISSIONS),
    "Manager": {"dashboard", "saisie_ca", "depenses", "ratios", "planning", "equipe",
                "avis_overview", "avis_liste", "avis_stats", "calendrier", "messagerie", "ressources"},
    "Chef": {"fiches_techniques", "allergenes", "fiches_bar", "fiches", "depenses", "ratios", "hygiene",
             "haccp_etiquettes", "haccp_releves", "haccp_receptions",
             "haccp_viandes_origine", "haccp_viandes_tracabilite", "haccp_checklists"},
    "Staff": {"planning"},
}

# Cache for roles per restaurant
_roles_cache = {}
_ROLES_CACHE_TTL = 120


def _get_role_permissions(restaurant_id, role_name):
    """Get permissions for a role from Airtable Roles table."""
    if role_name == "Gerant":
        return set(ALL_PERMISSIONS)

    # Check cache
    cache_key = f"{restaurant_id}:{role_name}"
    cached = _roles_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < _ROLES_CACHE_TTL:
        return cached["perms"]

    # Try Airtable
    try:
        roles = at.get_roles(restaurant_id)
        for r in roles:
            rname = r.get("Nom", "")
            perms_str = r.get("Permissions", "") or ""
            p = {x.strip() for x in perms_str.split(",") if x.strip()} if perms_str else set()
            _roles_cache[f"{restaurant_id}:{rname}"] = {"perms": p, "ts": time.time()}
        cached = _roles_cache.get(cache_key)
        if cached:
            return cached["perms"]
    except Exception:
        pass

    # Fallback to built-in presets
    return set(_FALLBACK_PRESETS.get(role_name, set()))


def invalidate_roles_cache(restaurant_id=None):
    """Clear cached role permissions."""
    if restaurant_id:
        keys = [k for k in _roles_cache if k.startswith(f"{restaurant_id}:")]
        for k in keys:
            del _roles_cache[k]
    else:
        _roles_cache.clear()


class User(UserMixin):
    def __init__(self, id, email, restaurant_id="", restaurant_name="", first_login=False, demo_mode=False, prenom="", nom="", role="Gerant", permissions=None):
        self.id = id
        self.email = email
        self.restaurant_id = restaurant_id
        self.restaurant_name = restaurant_name
        self.first_login = first_login
        self.demo_mode = demo_mode
        self.prenom = prenom
        self.nom = nom
        self.role = role
        self.permissions = permissions if permissions is not None else set(_FALLBACK_PRESETS.get(role, set()))


def _safe_formula_value(val):
    """Escape single quotes for Airtable formula injection prevention."""
    return str(val).replace("'", "\\'").replace("\\", "")


def _check_demo_mode(restaurant_id):
    """Check if a restaurant has demo_mode enabled in Airtable settings."""
    if not restaurant_id:
        return False
    try:
        settings = at.get_all("settings", restaurant_id=restaurant_id)
        for s in settings:
            if s.get("demo_mode"):
                return True
    except Exception:
        pass
    return False


_user_meta_cache = {}  # user_id -> {resto_nom, demo, ts}
_USER_META_TTL = 300   # cache for 5 minutes


DEMO_RESTAURANT_ID = "bistrot_du_port"


def user_type_for_restaurant(restaurant_id):
    """demo if bistrot_du_port, client otherwise."""
    return "demo" if restaurant_id == DEMO_RESTAURANT_ID else "client"


def invalidate_user_cache(user_id=None):
    """Flush in-memory user meta cache + Airtable cache for utilisateurs.
    Called after any admin modification on a user."""
    if user_id:
        _user_meta_cache.pop(user_id, None)
    else:
        _user_meta_cache.clear()
    at.invalidate_cache("utilisateurs")


@login_manager.user_loader
def load_user(user_id):
    """Load user from Airtable by record ID. Called on every request by Flask-Login."""
    # Demo session — no Airtable lookup needed
    if user_id == "demo_session":
        return User("demo_session", "demo@paucoandco.com",
                     restaurant_id="bistrot_du_port",
                     restaurant_name="Le Bistrot du Port",
                     first_login=False, demo_mode=True)

    # Use cached meta if fresh (avoids Airtable calls on every request)
    cached = _user_meta_cache.get(user_id)
    if cached and time.time() - cached["ts"] < _USER_META_TTL:
        return User(user_id, cached["email"], cached["rid"], cached["resto_nom"],
                     cached["first_login"], cached["demo"],
                     prenom=cached.get("prenom", ""), nom=cached.get("nom", ""),
                     role=cached.get("role", "Gerant"),
                     permissions=cached.get("perms", _get_role_permissions(cached.get("rid", ""), cached.get("role", "Gerant"))))

    try:
        rec = at.get_one("utilisateurs", user_id)
    except Exception:
        return None
    if not rec or not rec.get("Actif", 1):
        return None

    resto_id = rec.get("Restaurant_ID", "") or ""
    resto_nom = ""
    demo = False
    if resto_id:
        try:
            resto = at.get_restaurant(resto_id)
            if resto:
                resto_nom = resto.get("Nom", "")
        except Exception:
            pass
        demo = _check_demo_mode(resto_id)

    prenom = rec.get("Prenom", "") or ""
    nom = rec.get("Nom", "") or ""
    role = rec.get("Role", "") or "Gerant"
    perms = _get_role_permissions(resto_id, role)
    _user_meta_cache[user_id] = {
        "email": rec.get("Email", ""), "rid": resto_id,
        "resto_nom": resto_nom, "demo": demo,
        "first_login": bool(rec.get("First_login", 0)),
        "prenom": prenom, "nom": nom, "role": role,
        "perms": perms,
        "ts": time.time()
    }
    return User(user_id, rec.get("Email", ""), resto_id, resto_nom,
                bool(rec.get("First_login", 0)), demo,
                prenom=prenom, nom=nom, role=role, permissions=perms)


def init_auth_tables():
    """Create password_resets table in SQLite. Seed users & demo restaurant in Airtable."""
    # SQLite: only password_resets
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );
    """)
    db.commit()
    db.close()

    # ── Ensure demo restaurant exists in Airtable ──
    try:
        existing = at.get_all("restaurants", formula="{Restaurant_ID}='bistrot_du_port'")
        if not existing:
            at.create_restaurant(
                nom="Le Bistrot du Port",
                adresse="12 Quai de la Mediterranee",
                ville="Marseille",
                lat=43.2965, lng=5.3698,
                gerant_nom="Fabre", gerant_prenom="Jean-Michel",
                telephone="06 12 34 56 78", email="contact@bistrotduport.fr",
                restaurant_id="bistrot_du_port"
            )
            print("[AUTH] Restaurant demo cree (bistrot_du_port)")
            _seed_demo_data("bistrot_du_port")
    except Exception as e:
        print(f"[AUTH] Airtable check skipped: {e}")

    # ── Ensure all known user accounts exist in Airtable (idempotent) ──
    now = datetime.now().isoformat()
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_pw:
        print("[AUTH] WARNING: ADMIN_PASSWORD env var not set — skipping account seeding")
        return

    # Accounts loaded from env — format: email:password:prenom:nom (comma-separated)
    _accounts_raw = os.environ.get("SEED_ACCOUNTS", "")
    _ensure_accounts = [
        ("paul@paucoandco.com", admin_pw, "bistrot_du_port", 0, "Paul", ""),
    ]
    for entry in _accounts_raw.split(","):
        parts = entry.strip().split(":")
        if len(parts) >= 2:
            email, pw = parts[0], parts[1]
            prenom = parts[2] if len(parts) > 2 else ""
            nom = parts[3] if len(parts) > 3 else ""
            _ensure_accounts.append((email, pw, "bistrot_du_port", 0, prenom, nom))
    created = 0
    for email, pw, rid, fl, prenom, nom in _ensure_accounts:
        try:
            existing = at.find_first("utilisateurs", f"{{Email}}='{_safe_formula_value(email)}'")
            if not existing:
                user_type = user_type_for_restaurant(rid)
                at.create("utilisateurs", {
                    "Email": email,
                    "Password_hash": generate_password_hash(pw),
                    "Prenom": prenom,
                    "Nom": nom,
                    "Restaurant_ID": rid,
                    "Type": user_type,
                    "First_login": fl,
                    "Actif": 1,
                    "Created_at": now,
                })
                created += 1
        except Exception:
            pass
    if created:
        print(f"[AUTH] {created} comptes crees dans Airtable")


def _seed_demo_data(resto_id):
    """Seed ultra-realistic demo data — Le Bistrot du Port, Marseille.
    45 couverts salle + 20 terrasse, mardi-dimanche, TM 38 dejeuner / 52 diner.
    All data goes to Airtable.
    """
    import random, calendar
    from datetime import date as _d, timedelta
    random.seed(42)  # Reproducible

    # === POSTES ===
    for nom, pole in [("Chef de cuisine","Cuisine"),("Second de cuisine","Cuisine"),("Commis de cuisine","Cuisine"),
                      ("Chef de partie","Cuisine"),("Cuisinier","Cuisine"),("Plongeur","Cuisine"),
                      ("Maitre d'hotel","Salle"),("Serveur","Salle"),("Serveuse","Salle"),
                      ("Chef de rang","Salle"),("Hotesse d'accueil","Salle"),("Runner","Salle"),
                      ("Chef barman","Bar"),("Barman","Bar"),("Barmaid","Bar"),
                      ("Extra cuisine","Cuisine"),("Extra salle","Salle")]:
        try: at.create_poste(resto_id, nom, pole)
        except Exception as e: print(f"[SEED] Poste {nom}: {e}")

    # === 20 EMPLOYES ===
    emps_data = [
        # Cuisine (7)
        ("Pierre","Moreau","Chef de cuisine","CDI","2019-04-01","",2800,39,"06 12 45 78 23","Cuisine"),
        ("Sophie","Blanc","Second de cuisine","CDI","2021-09-01","",2200,39,"06 23 56 89 34","Cuisine"),
        ("Karim","Benali","Commis de cuisine","CDD","2023-10-01","2026-09-30",1800,35,"06 34 67 90 45","Cuisine"),
        ("Antoine","Roux","Cuisinier","CDI","2022-05-01","",2100,39,"06 14 25 36 47","Cuisine"),
        ("Fatima","Ouali","Commis de cuisine","CDD","2025-03-01","2026-02-28",1750,35,"06 25 36 47 58","Cuisine"),
        ("Remi","Garnier","Chef de partie","CDI","2021-09-15","",2300,39,"06 36 47 58 69","Cuisine"),
        ("Ines","Marchetti","Plongeur","CDI","2024-06-01","",1700,35,"06 47 58 69 70","Cuisine"),
        # Salle (7)
        ("Marie","Dupont","Maitre d'hotel","CDI","2018-06-15","",2400,39,"06 45 78 01 56","Salle"),
        ("Lucas","Martin","Serveur","CDI","2022-03-01","",1900,39,"06 56 89 12 67","Salle"),
        ("Emma","Bernard","Chef de rang","CDI","2023-01-10","",2000,35,"06 67 90 23 78","Salle"),
        ("Camille","Rousseau","Serveuse","CDI","2022-10-01","",1900,35,"06 15 26 37 48","Salle"),
        ("Baptiste","Girard","Serveur","CDI","2023-04-01","",1850,39,"06 26 37 48 59","Salle"),
        ("Nadia","Benali","Hotesse d'accueil","CDI","2020-03-01","",1800,30,"06 37 48 59 60","Salle"),
        ("Julien","Petit","Runner","CDI","2024-09-01","",1750,35,"06 48 59 60 71","Salle"),
        # Bar (4)
        ("Thomas","Leroy","Chef barman","CDI","2020-11-01","",2200,39,"06 78 01 34 89","Bar"),
        ("Julie","Faure","Barman","CDI","2024-02-01","",1900,35,"06 89 12 45 90","Bar"),
        ("Chloe","Mercier","Barmaid","CDI","2021-06-01","",2000,39,"06 59 60 71 82","Bar"),
        ("Maxime","Durand","Barman","Apprentissage","2025-09-01","2026-08-31",1200,35,"06 60 71 82 93","Bar"),
        # Extras (2)
        ("Luca","Ferrari","Extra cuisine","CDI","2023-11-01","",1900,35,"06 71 82 93 04","Cuisine"),
        ("Sara","Benoit","Extra salle","CDD","2025-06-01","2026-05-31",1650,30,"06 82 93 04 15","Salle"),
    ]
    emp_records = {}  # prenom -> airtable record id
    for p,n,poste,contrat,deb,fin,sal,h,phone,pole in emps_data:
        rec = at.create_employe(resto_id, p, n, poste, contrat, deb, sal, h, phone, pole, date_fin=fin or None)
        emp_records[p] = rec["id"]

    # === FOURNISSEURS ===
    fourns = [("Metro Cash & Carry","Food"),("Pomona","Food"),("PassionFroid","Food"),
              ("Le Comptoir de la Mer","Food"),("Nicolas Vins Marseille","Boissons"),
              ("Thiriet","Food"),("Marche Saint-Charles","Food"),("Blanchisserie Provence","Hygiene"),
              ("Boulangerie Maison Noel","Food"),("La Ferme du Vallon","Food")]
    for fn,ft in fourns:
        try: at.create_fournisseur(resto_id, fn, ft)
        except Exception as e: print(f"[SEED] Fournisseur {fn}: {e}")

    # === RECETTES ===
    notes_pool = {
        (1,4): "Reprise apres fetes — equipe au complet",
        (1,6): "Premier samedi — bonne reprise, 48 couverts soir",
        (1,10): "Pluie + mistral — terrasse fermee, service calme",
        (1,14): "Galette des Rois — menu special — 52 couverts soir",
        (1,18): "Dimanche calme, 28 couverts midi seulement",
        (1,20): "Pluie toute la journee — 22 couverts midi",
        (1,25): "Groupe cabinet comptable — 18 couverts midi",
        (2,7): "Mistral fort — terrasse fermee, 30 couverts",
        (2,14): "Saint-Valentin — complet midi et soir — menu 75EUR — 8 extras",
        (2,15): "Lendemain St-Valentin — encore du monde, 55 couverts soir",
        (2,21): "Groupe La Poste Marseille — 28 couverts midi",
        (2,22): "Beau soleil — terrasse ouverte, bonne journee",
        (3,1): "Terrasse ouverte premier weekend mars — complet terrasse",
        (3,7): "Brunch test dimanche — 42 couverts — tres positif, a refaire",
        (3,8): "Match OM — terrasse pleine pour l'apero, +800EUR bar",
        (3,14): "Complet midi et soir — refus de 12 couverts",
        (3,15): "Samedi exceptionnel — 68 couverts soir + terrasse",
        (3,21): "Soiree anniversaire 15 personnes — menu 55EUR",
        (3,22): "Dimanche ensoleille — terrasse pleine",
        (3,28): "Groupe Asso Sportive — 35 couverts midi reserves",
    }
    notes_2025 = {
        (10,31): "Halloween — deco speciale — 58 couverts soir",
        (11,1): "Toussaint — service midi uniquement",
        (11,20): "Beaujolais Nouveau — soiree speciale — complet",
        (12,24): "Reveillon Noel — menu 85EUR — complet",
        (12,31): "Saint-Sylvestre — menu 120EUR — complet",
    }

    # Store CA data for expense calculations
    ca_by_month = {}

    # Oct/Nov/Dec 2025
    ca_records_batch = []
    for mois in [10,11,12]:
        nb_days = calendar.monthrange(2025,mois)[1]
        m_str = f"2025-{mois:02d}"
        month_total = 0
        for d in range(1, nb_days+1):
            dt = _d(2025,mois,d)
            if dt.weekday() == 0: continue
            if mois==12 and d==25: continue
            is_we = dt.weekday() >= 4
            if mois == 10:
                ca_resto = random.randint(5500,8200) if is_we else random.randint(3200,5000)
                ca_bar = random.randint(1200,2000) if is_we else random.randint(500,900)
            elif mois == 11:
                ca_resto = random.randint(4500,6800) if is_we else random.randint(2800,4200)
                ca_bar = random.randint(900,1500) if is_we else random.randint(400,700)
            else:
                ca_resto = random.randint(6500,9500) if is_we else random.randint(4000,6200)
                ca_bar = random.randint(1400,2200) if is_we else random.randint(600,1100)
            if mois==11 and d==20: ca_resto=8500; ca_bar=2200
            if mois==12 and d==24: ca_resto=11000; ca_bar=3000
            if mois==12 and d==31: ca_resto=13000; ca_bar=3500
            ca = ca_resto + ca_bar
            month_total += ca
            midi = random.randint(30,50) if not is_we else random.randint(38,58)
            soir = random.randint(25,45) if not is_we else random.randint(40,65)
            if mois==12 and d in (24,31): soir=65; midi=0
            couv = midi + soir
            tm = round(ca_resto/couv,2) if couv>0 else 0
            note = notes_2025.get((mois,d),"")
            ca_records_batch.append({
                "Restaurant_ID": resto_id,
                "Date": dt.isoformat(),
                "CA_Total": float(ca),
                "CA_Restaurant": float(ca_resto),
                "CA_Bar": float(ca_bar),
                "Couverts_Midi": midi,
                "Couverts_Soir": soir,
                "Ticket_Moyen": tm,
                "Commentaire": note
            })
        ca_by_month[m_str] = month_total

    # Jan/Feb/Mar 2026
    for mois in [1,2,3]:
        nb_days = calendar.monthrange(2026,mois)[1]
        m_str = f"2026-{mois:02d}"
        month_total = 0
        for d in range(1, nb_days+1):
            dt = _d(2026,mois,d)
            if dt.weekday() == 0: continue
            is_we = dt.weekday() >= 4
            if mois == 1:
                ca_resto = random.randint(4500,6800) if is_we else random.randint(2800,4200)
                ca_bar = random.randint(900,1500) if is_we else random.randint(400,700)
            elif mois == 2:
                ca_resto = random.randint(5000,7500) if is_we else random.randint(3200,5000)
                ca_bar = random.randint(1000,1600) if is_we else random.randint(500,800)
            else:
                ca_resto = random.randint(5500,8000) if is_we else random.randint(3500,5500)
                ca_bar = random.randint(1100,1800) if is_we else random.randint(500,900)
            if mois==2 and d==14: ca_resto=9500; ca_bar=2500
            ca = ca_resto + ca_bar
            month_total += ca
            midi = random.randint(28,48) if not is_we else random.randint(35,55)
            soir = random.randint(22,42) if not is_we else random.randint(38,65)
            couv = midi + soir
            tm = round(ca_resto/couv,2) if couv>0 else 0
            note = notes_pool.get((mois,d),"")
            ca_records_batch.append({
                "Restaurant_ID": resto_id,
                "Date": dt.isoformat(),
                "CA_Total": float(ca),
                "CA_Restaurant": float(ca_resto),
                "CA_Bar": float(ca_bar),
                "Couverts_Midi": midi,
                "Couverts_Soir": soir,
                "Ticket_Moyen": tm,
                "Commentaire": note
            })
        ca_by_month[m_str] = month_total

    # Batch create CA records
    at.batch_create("ca_jour", ca_records_batch)
    print(f"[SEED] {len(ca_records_batch)} CA records created")

    # === DEPENSES FIXES ===
    fixes = [
        ("Loyer","SCI Vieux-Port Immobilier",4200),("Energie","EDF",920),
        ("Energie","Engie",650),("Assurances","Axa Pro Multirisque",520),
        ("Abonnements","Orange Pro + Telephone",220),("Abonnements","Logiciels caisse + Pauco",320),
        ("Abonnements","Verisure + camera",160),("Autre charges fixes","Cabinet Ferrandi",580),
        ("Autre charges fixes","Expert social paie",420),("Autre charges fixes","Taxe fonciere (mensualise)",650),
        ("Autre charges fixes","Redevance terrasse mairie",480),("Autre charges fixes","Mutuelle entreprise",380),
    ]
    dep_batch = []
    for cat,desc,m in fixes:
        dep_batch.append({
            "Restaurant_ID": resto_id,
            "Catégorie": cat,
            "Description": desc,
            "Montant": float(m),
            "Mois": "2025-10",
            "Type": "Fixe",
            "Recurrente": True,
            "Desactivee_mois": ""
        })
    at.batch_create("depenses", dep_batch)

    # === DEPENSES VARIABLES ===
    food_split = [
        ("Metro Cash & Carry", 0.25, 3), ("PassionFroid", 0.22, 3),
        ("Pomona", 0.14, 2), ("Le Comptoir de la Mer", 0.13, 2),
        ("Marche Saint-Charles", 0.10, 8), ("Thiriet", 0.06, 1),
        ("Boulangerie Maison Noel", 0.05, 4), ("La Ferme du Vallon", 0.05, 2),
    ]
    ops_split = [
        ("Blanchisserie Provence", 0.45, 2),
        ("Produits entretien", 0.25, 1),
        ("Fournitures diverses", 0.30, 1),
    ]

    for m_str in ["2025-10","2025-11","2025-12","2026-01","2026-02","2026-03"]:
        ca_mois = ca_by_month.get(m_str, 70000)
        var_batch = []

        # Food = 29% CA
        total_food = ca_mois * 0.29
        for fourn, pct, nb in food_split:
            base = total_food * pct / nb
            for i in range(nb):
                variation = random.uniform(0.85, 1.15)
                montant = round(base * variation, 2)
                jour = random.randint(2, 28)
                var_batch.append({
                    "Restaurant_ID": resto_id,
                    "Date": f"{m_str}-{jour:02d}",
                    "Catégorie": "Food",
                    "Description": fourn,
                    "Montant": montant,
                    "Mois": m_str,
                    "Type": "Variable"
                })

        # Boissons = 21% CA
        total_bev = ca_mois * 0.21
        base_bev = total_bev / 4
        for i in range(4):
            variation = random.uniform(0.85, 1.15)
            jour = random.randint(2, 28)
            var_batch.append({
                "Restaurant_ID": resto_id,
                "Date": f"{m_str}-{jour:02d}",
                "Catégorie": "Boissons",
                "Description": "Nicolas Vins Marseille",
                "Montant": round(base_bev * variation, 2),
                "Mois": m_str,
                "Type": "Variable"
            })

        # Personnel
        for desc, montant in [("Salaires equipe", 39100), ("URSSAF + caisses", 16400)]:
            jour = random.randint(2, 28)
            var_batch.append({
                "Restaurant_ID": resto_id,
                "Date": f"{m_str}-{jour:02d}",
                "Catégorie": "Salaires" if "Salaire" in desc else "Charges sociales",
                "Description": desc,
                "Montant": float(montant),
                "Mois": m_str,
                "Type": "Variable"
            })
        jour = random.randint(2, 28)
        var_batch.append({
            "Restaurant_ID": resto_id,
            "Date": f"{m_str}-{jour:02d}",
            "Catégorie": "Extras & Interim",
            "Description": "Extras weekend",
            "Montant": round(random.uniform(450, 650), 2),
            "Mois": m_str,
            "Type": "Variable"
        })

        # Ops = 3% CA
        total_ops = ca_mois * 0.03
        for fourn, pct, nb in ops_split:
            base = total_ops * pct / nb
            for i in range(nb):
                variation = random.uniform(0.85, 1.15)
                jour = random.randint(2, 28)
                var_batch.append({
                    "Restaurant_ID": resto_id,
                    "Date": f"{m_str}-{jour:02d}",
                    "Catégorie": "Hygiene & Entretien",
                    "Description": fourn,
                    "Montant": round(base * variation, 2),
                    "Mois": m_str,
                    "Type": "Variable"
                })

        at.batch_create("depenses", var_batch)

    # Ponctuelles
    ponct_batch = []
    ponctuelles = [
        ("2025-11-12","Reparation","Reparation frigo chambre froide — Froid 13",1250),
        ("2025-12-05","Reparation","Revision hotte cuisine — Ventil Pro",680),
        ("2026-01-08","Reparation","Plomberie urgence — Allo Plombier",320),
        ("2026-02-15","Autre charges ope","Achat vaisselle remplacement — Metro",890),
        ("2026-03-02","Autre charges ope","Nappe et linge neuf — Blanchisserie Provence",450),
        ("2026-03-18","Reparation","Reparation lave-vaisselle — SAV Winterhalter",580),
    ]
    for dt_str,cat,desc,montant in ponctuelles:
        m = dt_str[:7]
        ponct_batch.append({
            "Restaurant_ID": resto_id,
            "Date": dt_str,
            "Catégorie": cat,
            "Description": desc,
            "Montant": float(montant),
            "Mois": m,
            "Type": "Variable"
        })
    at.batch_create("depenses", ponct_batch)

    # === FICHES FOOD ===
    fiches = [
        ("Foie gras maison toast brioche","Entrees",22.00,8.20,6,25),
        ("Soupe de poissons rouille croutons","Entrees",14.00,3.80,8,30),
        ("Salade chevre chaud miel noix","Entrees",15.00,4.20,1,10),
        ("Carpaccio de poulpe huile olive","Entrees",18.00,6.50,1,15),
        ("Bouillabaisse marseillaise","Plats",38.00,14.50,4,45),
        ("Entrecote grillee beurre maitre d'hotel","Plats",32.00,12.80,1,20),
        ("Magret de canard sauce aux cerises","Plats",29.00,11.20,1,30),
        ("Tartare de boeuf couteau","Plats",26.00,9.40,1,15),
        ("Risotto Saint-Jacques","Plats",34.00,13.60,1,35),
        ("Pave de saumon beurre blanc","Plats",28.00,10.80,1,20),
        ("Loup de mer grille fenouil","Plats",30.00,12.00,1,25),
        ("Plat du jour (moyenne)","Plats",18.00,5.80,1,20),
        ("Creme brulee vanille Bourbon","Desserts",9.00,1.80,6,20),
        ("Tarte tatin pommes calvados","Desserts",9.00,2.10,8,40),
        ("Assiette de fromages affines","Desserts",12.00,3.40,1,5),
        ("Mousse au chocolat maison","Desserts",8.00,1.50,8,15),
        ("Panacotta fruits rouges","Desserts",9.00,1.90,6,15),
    ]
    for nom,cat,pvttc,cout,portions,temps in fiches:
        pvht=round(pvttc/1.10,2);coeff=round(pvht/cout,1);ratio=round(cout/pvht*100,1);marge=round(pvht-cout,2)
        at.create_fiche(resto_id, "Food", nom, cat, pvht, pvttc, cout, coeff, ratio, marge, nb_portions=portions, temps_prepa=temps)

    # === COCKTAILS ===
    for nom,cat,vol,pvttc,cout in [("Mojito","Classiques",25,11.00,2.40),("Spritz Aperol","Classiques",18,10.00,1.80),
        ("Old Fashioned Bourbon","Creations",12,13.00,3.20),("Margarita","Classiques",15,12.00,2.60),
        ("Pastis Ricard maison","Creations",8,5.00,0.80)]:
        pvht=round(pvttc/1.10,2);coeff=round(pvht/cout,1);ratio=round(cout/pvht*100,1);marge=round(pvht-cout,2)
        at.create_fiche(resto_id, "Cocktail", nom, cat, pvht, pvttc, cout, coeff, ratio, marge, volume_cl=vol)

    # === BOISSONS BAR ===
    for nom,cat,cl,achat,pvttc in [("Cotes de Provence rose 75cl","Vins",75,6.50,28.00),
        ("Bordeaux Chateau Lascombes 75cl","Vins",75,14.00,52.00),
        ("Chablis Premier Cru 75cl","Vins",75,12.00,44.00),
        ("Kronenbourg pression 25cl","Bieres",25,0.45,5.00),
        ("San Pellegrino 50cl","Softs & Jus",50,0.90,5.00)]:
        pvht=round(pvttc/1.20,2)
        if cat=="Softs & Jus": pvht=round(pvttc/1.10,2)
        coutcl=round(achat/cl,4)
        cout_aj=round(achat*1.05,2)
        coeff=round(pvht/cout_aj,1) if cout_aj>0 else 0
        ratio=round(cout_aj/pvht*100,1) if pvht>0 else 0
        marge=round(pvht-cout_aj,2)
        at.create_fiche(resto_id, "Boisson", nom, cat, pvht, pvttc, cout_aj, coeff, ratio, marge,
                        contenance_cl=cl, prix_achat_ht=achat, cout_ht_cl=coutcl,
                        perte_casse=2, perte_degustation=2, perte_evaporation=1, cout_ajuste=cout_aj)

    # === EVENEMENTS ===
    evts = [
        ("Repas de groupe — Asso Sportive Marseille — 35 couverts midi","2026-03-28","#1D4ED8"),
        ("Soiree privee anniversaire — 20 pers — menu 45EUR","2026-03-29","#7C3AED"),
        ("Inventaire mensuel — fermeture 15h","2026-03-31","#6B7280"),
        ("Brunch de Paques — service continu 11h-15h","2026-04-05","#D97706"),
        ("Visite guide Michelin — tenue irreprochable","2026-04-11","#DC2626"),
        ("Soiree Jazz — Antoine Duval Quartet — 19h30","2026-04-18","#7C3AED"),
        ("Seminaire Cabinet Conseil MED — 18 couverts","2026-04-24","#1D4ED8"),
        ("Inventaire mensuel","2026-04-30","#6B7280"),
        ("Fete des Meres — menu 55EUR — complet","2026-05-10","#EC4899"),
        ("Briefing saison estivale 10h","2026-05-15","#6B7280"),
        ("Ascension — service midi uniquement","2026-05-21","#D97706"),
        ("Degustation vins Nicolas — 30 couverts 19h","2026-05-23","#2D6A4A"),
        ("Inventaire + cloture bilan mai","2026-05-31","#6B7280"),
    ]
    evt_batch = [{"Restaurant_ID": resto_id, "Titre": t, "Date": d, "Couleur": c} for t,d,c in evts]
    at.batch_create("evenements", evt_batch)

    # === FERMETURES ===
    at.create_fermeture(resto_id, recurrence=True, jour_semaine=0)
    for fd in ["2026-04-06","2026-05-01","2026-05-08","2026-05-29"]:
        at.create_fermeture(resto_id, date_str=fd)

    # === CATEGORIES FICHES ===
    for cat in ["Entrees","Plats","Desserts","Menu enfant"]:
        at.create_categorie(resto_id, "food", cat)
    for cat in ["Classiques","Creations","Sans Alcool"]:
        at.create_categorie(resto_id, "cocktail", cat)
    for cat in ["Vins","Bieres","Softs & Jus","Spiritueux","Autres"]:
        at.create_categorie(resto_id, "bar", cat)

    # === SHIFTS ===
    default_shifts = [
        ("Midi", "Salle", "11:00", "15:30", "#2D6A4A"),
        ("Soir", "Salle", "18:30", "23:30", "#1D4ED8"),
        ("Coupure", "Salle", "11:00", "23:30", "#7C3AED"),
        ("Midi", "Cuisine", "09:00", "15:00", "#D97706"),
        ("Soir", "Cuisine", "17:30", "23:00", "#DC2626"),
        ("Soir", "Bar", "17:00", "23:30", "#6B21A8"),
    ]
    for nom,pole,hd,hf,coul in default_shifts:
        at.create_shift(resto_id, nom, pole, hd, hf, coul)

    # === PLANNING MARS 2026 ===
    eid = emp_records  # prenom -> airtable record id
    conges_sophie = set(range(23,28)); conges_lucas = {9,10}

    planning_batch = []
    def _s(name,ds,hd,hf,pole):
        if name in eid:
            planning_batch.append({
                "Restaurant_ID": resto_id,
                "Employé_ID": eid[name],
                "Date": ds,
                "Shift_début": hd,
                "Shift_fin": hf,
                "Poste": pole
            })

    all_emp_names = list(eid.keys())
    for d in range(1,32):
        try: dt = _d(2026,3,d)
        except: continue
        dow = dt.weekday()
        ds = dt.isoformat()
        # Lundi = jour de fermeture → Repos pour tout le monde
        if dow == 0:
            for name in all_emp_names:
                _s(name, ds, "R", "R", "Repos")
            continue
        if dow not in (0,2): _s("Pierre",ds,"09:00","15:00","Cuisine")
        if dow in (4,5): _s("Pierre",ds,"18:30","22:30","Cuisine")
        if dow not in (0,6) and d not in conges_sophie: _s("Sophie",ds,"17:30","23:00","Cuisine")
        if dow in (1,2,3,4): _s("Karim",ds,"09:00","15:00","Cuisine")
        if dow==5: _s("Karim",ds,"18:00","23:00","Cuisine")
        if dow in (1,2,3,4,5): _s("Antoine",ds,"09:00","15:00","Cuisine")
        if dow in (1,2,3,4,5): _s("Fatima",ds,"18:00","23:00","Cuisine")
        if dow in (2,3,4,5,6): _s("Remi",ds,"17:30","23:00","Cuisine")
        if dow in (1,2,3,4,5): _s("Ines",ds,"09:00","15:30","Cuisine")
        if dow not in (0,2): _s("Marie",ds,"11:00","15:30","Salle")
        if dow in (3,4,5,6): _s("Marie",ds,"18:30","23:00","Salle")
        if d not in conges_lucas:
            if dow in (1,2,3): _s("Lucas",ds,"18:30","23:30","Salle")
            if dow in (4,5,6): _s("Lucas",ds,"11:00","15:30","Salle"); _s("Lucas",ds,"18:30","23:30","Salle")
        if dow in (2,3,4,5,6): _s("Emma",ds,"11:00","15:30","Salle")
        if dow in (1,2,3,4,5): _s("Camille",ds,"18:30","23:30","Salle")
        if dow in (3,4,5,6): _s("Baptiste",ds,"11:00","15:30","Salle"); _s("Baptiste",ds,"18:30","23:30","Salle")
        if dow in (1,2,3,4,5,6): _s("Nadia",ds,"11:30","14:30","Salle")
        if dow in (3,4,5,6): _s("Julien",ds,"18:00","23:00","Salle")
        if dow in (1,2,3,4,5): _s("Thomas",ds,"17:00","23:30","Bar")
        if dow in (3,4,5,6): _s("Julie",ds,"18:00","23:30","Bar")
        if dow in (1,2,3,4,5): _s("Chloe",ds,"17:00","23:00","Bar")
        if dow in (4,5,6): _s("Maxime",ds,"18:00","23:00","Bar")
        if dow in (4,5,6): _s("Luca",ds,"09:00","15:00","Cuisine")
        if dow in (4,5,6): _s("Sara",ds,"18:30","23:00","Salle")

    at.batch_create("planning", planning_batch)
    print(f"[SEED] {len(planning_batch)} planning entries created")

    # Conges
    for name,ct,d1,d2,co in [("Sophie","Conges payes","2026-03-23","2026-03-27","CP 5j"),
        ("Lucas","Conges payes","2026-03-09","2026-03-10","CP 2j"),
        ("Marie","Conges payes","2026-02-16","2026-02-18","CP 3j"),
        ("Thomas","Conges payes","2026-01-19","2026-01-20","CP 2j"),
        ("Karim","Conges payes","2026-02-23","2026-02-24","CP 2j"),
        ("Emma","Conges payes","2026-03-12","2026-03-12","CP 1j")]:
        if name in eid:
            at.create_conge(resto_id, eid[name], ct, d1, d2, co)

    print("[SEED] Donnees demo Le Bistrot du Port creees (Airtable)")


def create_user(email, password, restaurant_name="", prenom="", nom="", ville="", real_client=False):
    """Create a new user in Airtable. If real_client=True, creates a new empty restaurant too.
    Returns the Airtable record ID."""
    pw_hash = generate_password_hash(password)
    now = datetime.now().isoformat()
    if real_client:
        resto_id = at._make_restaurant_slug(prenom or "client", ville or "france")
        at.create_restaurant(
            nom=f"Restaurant de {prenom} {nom}".strip(),
            email=email.lower().strip(),
            ville=ville,
            gerant_prenom=prenom,
            gerant_nom=nom,
            restaurant_id=resto_id
        )
        first_login = 1
        for cat in ["Entrees","Plats","Desserts"]:
            at.create_categorie(resto_id, "food", cat)
        for cat in ["Classiques","Creations","Sans Alcool"]:
            at.create_categorie(resto_id, "cocktail", cat)
        for cat in ["Vins","Bieres","Softs & Jus"]:
            at.create_categorie(resto_id, "bar", cat)
    else:
        resto_id = DEMO_RESTAURANT_ID
        first_login = 0
    rec = at.create("utilisateurs", {
        "Email": email.lower().strip(),
        "Password_hash": pw_hash,
        "Prenom": prenom,
        "Nom": nom,
        "Restaurant_ID": resto_id,
        "Type": user_type_for_restaurant(resto_id),
        "First_login": first_login,
        "Actif": 1,
        "Created_at": now,
    })
    return rec["id"]


def verify_login(email, password):
    """Verify email/password against Airtable. Returns User or None.
    Uses find_first_nocache to avoid stale __NONE__ cache blocking logins."""
    email_clean = email.lower().strip()
    rec = at.find_first_nocache("utilisateurs", f"{{Email}}='{_safe_formula_value(email_clean)}'")
    if not rec:
        return None
    if not rec.get("Actif", 1):
        return None
    if not check_password_hash(rec.get("Password_hash", ""), password):
        return None

    user_id = rec["id"]
    resto_id = rec.get("Restaurant_ID", "") or ""
    resto_nom = ""
    demo = False
    if resto_id:
        try:
            resto = at.get_restaurant(resto_id)
            if resto:
                resto_nom = resto.get("Nom", "")
        except Exception:
            pass
        demo = _check_demo_mode(resto_id)

    prenom = rec.get("Prenom", "") or ""
    nom = rec.get("Nom", "") or ""
    role = rec.get("Role", "") or "Gerant"
    perms = _get_role_permissions(resto_id, role)
    _user_meta_cache[user_id] = {
        "email": email_clean, "rid": resto_id,
        "resto_nom": resto_nom, "demo": demo,
        "first_login": bool(rec.get("First_login", 0)),
        "prenom": prenom, "nom": nom, "role": role,
        "perms": perms,
        "ts": time.time()
    }
    return User(user_id, email_clean, resto_id, resto_nom, bool(rec.get("First_login", 0)), demo,
                prenom=prenom, nom=nom, role=role, permissions=perms)


def create_reset_token(email):
    """Create a password reset token. Returns (token, user_id) or (None, None)."""
    email_clean = email.lower().strip()
    rec = at.find_first_nocache("utilisateurs", f"{{Email}}='{_safe_formula_value(email_clean)}'")
    if not rec:
        return None, None
    token = secrets.token_urlsafe(48)
    expires = (datetime.now() + timedelta(hours=1)).isoformat()
    db = _get_auth_db()
    db.execute("INSERT INTO password_resets (user_id, token, expires_at) VALUES (?,?,?)",
               (rec["id"], token, expires))
    db.commit()
    db.close()
    return token, rec["id"]


def validate_reset_token(token):
    """Validate token. Returns user_id or None."""
    db = _get_auth_db()
    row = db.execute("SELECT * FROM password_resets WHERE token=? AND used=0", (token,)).fetchone()
    db.close()
    if not row:
        return None
    if datetime.now() > datetime.fromisoformat(row["expires_at"]):
        return None
    return row["user_id"]


def reset_password(token, new_password):
    """Reset password using token. Returns True on success."""
    db = _get_auth_db()
    row = db.execute("SELECT * FROM password_resets WHERE token=? AND used=0", (token,)).fetchone()
    if not row:
        db.close()
        return False
    if datetime.now() > datetime.fromisoformat(row["expires_at"]):
        db.close()
        return False
    pw_hash = generate_password_hash(new_password)
    # Update password in Airtable (user_id is now an Airtable record ID)
    at.update("utilisateurs", row["user_id"], {"Password_hash": pw_hash})
    db.execute("UPDATE password_resets SET used=1 WHERE id=?", (row["id"],))
    db.commit()
    db.close()
    return True


def complete_onboarding(user_id, data):
    """Complete first login onboarding. user_id is an Airtable record ID."""
    try:
        rec = at.get_one("utilisateurs", user_id)
    except Exception:
        return
    if not rec:
        return
    resto_id = rec.get("Restaurant_ID", "")
    if resto_id:
        at.update_restaurant(resto_id,
            nom=data.get("nom", ""),
            adresse=data.get("adresse", ""),
            ville=data.get("ville", ""),
            lat=float(data.get("lat", 48.8566)),
            lng=float(data.get("lng", 2.3522)),
            gerant_nom=data.get("gerant_nom", ""),
            gerant_prenom=data.get("gerant_prenom", ""),
            telephone=data.get("telephone", ""),
            email=data.get("email", "")
        )
    at.update("utilisateurs", user_id, {"First_login": 0})
    # Invalidate cache so load_user picks up the change
    _user_meta_cache.pop(user_id, None)
