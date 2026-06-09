# -*- coding: utf-8 -*-
"""
Module Gestion — Prototype standalone pour restaurateurs.
Saisie CA, dépenses, ratios automatiques, graphiques Chart.js.
Port 5001 | Airtable (source of truth) + SQLite (read cache) | Flask

PROTECTION: jamais de DELETE sur users ou restaurants.
GuardedDB (db.py) bloque tout DELETE non scope sur ces tables.
Suppression utilisateur = soft delete (actif=0).
"""

import os
import io
import time
import sqlite3
import json
from datetime import datetime, date, timedelta
from flask import render_template, request, redirect, url_for, jsonify, make_response, session, send_file

from flask_login import login_required, login_user, logout_user, current_user

from modules.config import app
from modules.db import get_db, close_db, init_db
from modules.helpers import _rid, MOIS_NOMS, _solde_cp, _compteur_total, _mois_label, _mois_nom, _mois_courant, _get_mois_list, _stats_mois, _depenses_mois, _seuil_rentabilite, _meilleur_mois_historique

# Auth imports used by routes
from modules.auth import login_manager, init_auth_tables, verify_login, create_user, create_reset_token, validate_reset_token, reset_password, complete_onboarding, invalidate_user_cache, user_type_for_restaurant, User

# Airtable sync
from modules import airtable_client as at
from modules import airtable_sync as sync
from modules.rapport_whatsapp import build_rapport, send_whatsapp, send_all_rapports
from modules import routes_depenses


@app.context_processor
def inject_globals():
    """Inject nb_messages_non_lus and current_user into all templates."""
    try:
        db = get_db()
        nb = db.execute("SELECT COUNT(*) as c FROM messages WHERE restaurant_id=? AND lu = 0", (_rid(),)).fetchone()["c"]
        resto_name = current_user.restaurant_name if current_user.is_authenticated else "Mon Restaurant"
        demo = getattr(current_user, "demo_mode", False) if current_user.is_authenticated else False
        user_fullname = ""
        if current_user.is_authenticated:
            p = getattr(current_user, "prenom", "") or ""
            n = getattr(current_user, "nom", "") or ""
            user_fullname = f"{p} {n}".strip()
        user_role = getattr(current_user, "role", "Gerant") if current_user.is_authenticated else "Gerant"
        user_perms = getattr(current_user, "permissions", set()) if current_user.is_authenticated else set()
        # Check if onboarding is incomplete (for "Pour commencer" tab)
        show_demarrage = False
        if current_user.is_authenticated and user_role == "Gerant":
            try:
                import json as _json
                _urec = at.get_one("utilisateurs", current_user.id) or {}
                _steps = _json.loads(_urec.get("Onboarding_Steps", "") or "{}")
                show_demarrage = sum(1 for v in _steps.values() if v) < 9
            except Exception:
                show_demarrage = True
        # Active options (from Airtable restaurant record — no cache)
        active_options = set()
        if current_user.is_authenticated:
            try:
                rid = getattr(current_user, "restaurant_id", "")
                if rid:
                    _resto_table = at._table("restaurants")
                    _recs = _resto_table.all(formula=f"{{Restaurant_ID}}='{rid}'", max_records=1)
                    if _recs:
                        opts_raw = _recs[0]["fields"].get("Options actives") or []
                        if isinstance(opts_raw, list):
                            active_options = {o.strip() for o in opts_raw if o}
                        elif isinstance(opts_raw, str):
                            active_options = {o.strip() for o in opts_raw.split(",") if o.strip()}
            except Exception as e:
                print(f"[OPTIONS] Error: {e}")
        return {"nb_messages_non_lus": nb, "restaurant_name": resto_name, "demo_mode": demo,
                "ga_id": os.environ.get("GA_MEASUREMENT_ID", ""), "user_fullname": user_fullname,
                "user_role": user_role, "user_perms": user_perms, "show_demarrage": show_demarrage,
                "active_options": active_options}
    except Exception:
        return {"nb_messages_non_lus": 0, "restaurant_name": "Mon Restaurant", "demo_mode": False,
                "ga_id": os.environ.get("GA_MEASUREMENT_ID", ""), "user_fullname": "", "user_role": "Gerant",
                "user_perms": set(), "show_demarrage": False, "active_options": set()}


# ---------------------------------------------------------------------------
#  Role-based access control
# ---------------------------------------------------------------------------

# Permission → page mapping for route enforcement
_PERM_PAGES = {
    "dashboard": {"dashboard", "recettes", "saisie_rapide"},
    "saisie_ca": {"recettes", "saisie_rapide"},
    "depenses": {"depenses", "analyse_depenses", "fournisseurs_page"},
    "fiches": {"fiches_hub"},
    "ratios": {"ratios", "ratios_exemples"},
    "planning": {"planning", "shifts"},
    "equipe": {"effectifs", "employe_fiche", "postes"},
    "avis_overview": {"avis_overview"},
    "avis_liste": {"avis_liste"},
    "avis_stats": {"avis_stats"},
    "fiches_techniques": {"fiches_techniques", "fiches_cocktails"},
    "allergenes": {"allergenes"},
    "fiches_bar": {"fiches_bar"},
    "calendrier": {"res_calendrier"},
    "messagerie": {"messagerie"},
    "ressources": {"res_gestion", "res_rh", "res_cuisine", "res_ereputation", "res_juridique"},
    "reglages": {"reglages_restaurant", "reglages_utilisateurs", "reglages_roles"},
    # Hygiène HACCP — sous-modules avec permissions individuelles
    "haccp_etiquettes": {"hygiene_etiquettes"},
    "haccp_releves": {"hygiene_releves"},
    "haccp_receptions": {"hygiene_receptions"},
    "haccp_viandes_origine": {"origine_viandes"},
    "haccp_viandes_tracabilite": {"hygiene_tracabilite"},
    "haccp_checklists": {"hygiene_checklists"},
    # Permission globale "hygiene" reste disponible pour la conformité légale + tableau de bord HACCP
    "hygiene": {"conformite_page", "hygiene_page"},
    "marketing_perf_ads": {"marketing_perf_ads"},
    "marketing_rapports_ads": {"marketing_rapports_ads", "marketing_rapports_ads_pdf"},
    "marketing_stats_reseaux": {"marketing_stats_reseaux"},
    "marketing_shooting": {"marketing_shooting"},
    "recrut_offres": {"recrutement"},
    "recrut_candidatures": {"recrutement_candidatures"},
    "recrut_vivier": {"recrutement_vivier"},
}

# Invert: page → required permission
_PAGE_TO_PERM = {}
for perm, pages in _PERM_PAGES.items():
    for p in pages:
        _PAGE_TO_PERM[p] = perm


def _check_perm(page_name):
    """Return True if current user has permission for this page."""
    if not current_user.is_authenticated:
        return False
    perms = getattr(current_user, "permissions", set())
    required = _PAGE_TO_PERM.get(page_name)
    if not required:
        return True  # pages without mapping are open
    return required in perms


# Keep _check_role as alias for backward compat
_check_role = _check_perm


@app.template_filter("datefr")
def datefr_filter(val):
    """Convert ISO date '2026-03-24' to French '24 mars 2026'. Pass-through on error."""
    if not val or len(str(val)) < 10:
        return val or "—"
    try:
        s = str(val)[:10]
        d, m, y = int(s[8:10]), int(s[5:7]), s[:4]
        return f"{d} {MOIS_NOMS[m-1].lower()} {y}"
    except Exception:
        return val

# ---------------------------------------------------------------------------
#  Database — moved to db.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
#  Auth — protect all routes
# ---------------------------------------------------------------------------

_PUBLIC_ROUTES = {"login", "forgot_password", "reset_password_page", "setup_password", "set_password",
                  "onboarding", "static",
                  "ping", "healthz", "version", "diag_airtable", "demo_access", "demo_code_access", "public_allergenes",
                  "admin_login", "admin_logout", "admin_create_account", "api_admin_create_account",
                  "admin_dashboard", "admin_delete_user", "admin_reactivate_user",
                  "admin_change_restaurant", "admin_edit_user", "admin_edit_restaurant",
                  "admin_create_restaurant", "admin_restaurant_detail",
                  "admin_test_rapport", "admin_preview_rapport",
                  "stripe_webhook"}

# Map endpoint names to ROLE_ACCESS page names for enforcement
_ENDPOINT_ROLE_MAP = {
    # Gestion — Manager+Gerant
    "dashboard": "dashboard", "recettes": "recettes", "saisie_rapide": "saisie_rapide",
    "recettes_save": "recettes", "recettes_export": "recettes",
    # Depenses — Manager+Chef+Gerant
    "depenses": "depenses", "depenses_export": "depenses", "depenses_scan": "depenses",
    "delete_depense": "depenses", "disable_recurrente": "depenses", "enable_recurrente": "depenses",
    "analyse_depenses": "analyse_depenses", "analyse_depenses_data": "analyse_depenses",
    "gestion_fournisseurs": "fournisseurs_page",
    # Fiches — Chef+Gerant
    "fiches_hub": "fiches_hub", "fiches_techniques": "fiches_techniques",
    "fiche_save": "fiches_techniques", "fiche_delete": "fiches_techniques",
    "fiche_archive": "fiches_techniques", "fiche_restore": "fiches_techniques",
    "fiches_cocktails": "fiches_cocktails", "cocktail_save": "fiches_cocktails",
    "cocktail_delete": "fiches_cocktails", "cocktail_archive": "fiches_cocktails",
    "cocktail_restore": "fiches_cocktails",
    "fiches_bar": "fiches_bar", "bar_save": "fiches_bar",
    "bar_delete": "fiches_bar", "bar_archive": "fiches_bar", "bar_restore": "fiches_bar",
    "allergenes_page": "allergenes", "allergenes_add": "allergenes",
    "allergenes_toggle": "allergenes", "allergenes_autres": "allergenes",
    "allergenes_delete": "allergenes",
    "categories_manage": "fiches_techniques", "categories_list": "fiches_techniques",
    # Ratios — Manager+Chef+Gerant
    "ratios_page": "ratios", "ratios_data": "ratios",
    "ratios_exemples": "ratios_exemples", "ratios_mes_ratios": "ratios_exemples",
    # RH — Manager+Gerant (planning for Staff too)
    "rh_planning": "planning", "planning_add": "planning", "planning_delete": "planning",
    "planning_repos": "planning", "planning_cp": "planning", "planning_absence": "planning",
    "rh_planning_reorder": "planning", "rh_planning_export_pdf": "planning",
    "rh_shifts": "shifts",
    "rh_effectifs": "effectifs", "employe_fiche": "employe_fiche",
    "employe_update": "effectifs", "employe_upload": "effectifs",
    "employe_doc_delete": "effectifs", "employe_archive": "effectifs",
    "employe_photo": "effectifs", "rh_postes": "postes",
    "rh_conges": "effectifs", "rh_postes_category": "postes",
    # E-reputation — Manager+Gerant
    "avis_overview": "avis_overview", "avis_liste": "avis_liste",
    "avis_stats": "avis_stats", "avis_export_pdf": "avis_stats",
    # Messagerie — Manager+Gerant
    "messagerie": "messagerie", "messagerie_send": "messagerie",
    "messagerie_read": "messagerie", "messagerie_delete": "messagerie",
    # Reglages — Gerant only (checked in route handlers)
    # Hygiene — Chef+Gerant
    "hygiene_page": "hygiene_page",
    # Étiquetage HACCP
    "hygiene_etiquettes": "hygiene_etiquettes",
    "hygiene_etiquettes_save": "hygiene_etiquettes", "hygiene_etiquette_pdf": "hygiene_etiquettes",
    # Relevé températures
    "hygiene_releves": "hygiene_releves", "hygiene_releves_save": "hygiene_releves",
    "hygiene_releves_pdf": "hygiene_releves", "hygiene_releves_fiche_semaine": "hygiene_releves",
    "hygiene_releves_signaler": "hygiene_releves", "temperatures_upload_doc": "hygiene_releves",
    "temperatures_delete_doc": "hygiene_releves",
    # Réception marchandises
    "hygiene_receptions": "hygiene_receptions", "hygiene_receptions_save": "hygiene_receptions",
    "hygiene_receptions_delete": "hygiene_receptions", "hygiene_receptions_pdf": "hygiene_receptions",
    # Origine viandes
    "origine_viandes": "origine_viandes",
    # Traçabilité viandes
    "hygiene_tracabilite": "hygiene_tracabilite", "hygiene_tracabilite_save": "hygiene_tracabilite",
    "hygiene_tracabilite_delete": "hygiene_tracabilite", "hygiene_tracabilite_pdf": "hygiene_tracabilite",
    # Checklists quotidiennes
    "hygiene_checklists": "hygiene_checklists", "hygiene_checklists_save": "hygiene_checklists",
    "hygiene_checklists_pdf": "hygiene_checklists", "hygiene_checklists_pdf_serie": "hygiene_checklists",
    "api_checklists_reorder": "hygiene_checklists",
    # Conformité légale + Kit HACCP (permission globale "hygiene")
    "hygiene_pms_pdf": "hygiene_page",
    "hygiene_kit": "hygiene_page", "hygiene_kit_interest": "hygiene_page",
    # Marketing
    "marketing_perf_ads": "marketing_perf_ads",
    "marketing_rapports_ads": "marketing_rapports_ads",
    "marketing_rapports_ads_pdf": "marketing_rapports_ads",
    "marketing_stats_reseaux": "marketing_stats_reseaux",
    "marketing_shooting": "marketing_shooting",
    # Recrutement
    "recrutement": "recrutement",
    "recrutement_candidatures": "recrutement_candidatures",
    "recrutement_vivier": "recrutement_vivier",
    "recrutement_missions": "recrutement_vivier",
}


@app.before_request
def require_login():
    if request.endpoint in _PUBLIC_ROUTES:
        return
    # Auto-login démo : tout host commençant par "demo." (ex. demo.paucoandco.com)
    # ou contenant "demo" loggue automatiquement en session démo Bistrot du Port,
    # quel que soit l'URL demandée. Garantit que l'utilisateur ne tombe JAMAIS
    # sur l'écran de login en navigation privée.
    if not current_user.is_authenticated:
        host = (request.host or "").lower().split(":")[0]
        if host.startswith("demo.") or host == "demo" or ".demo." in host:
            try:
                from modules.auth import User as _User
                _demo = _User("demo_session", "demo@paucoandco.com",
                              restaurant_id="bistrot_du_port",
                              restaurant_name="Le Bistrot du Port",
                              first_login=False, demo_mode=True)
                login_user(_demo, remember=False)
                print(f"[DEMO] auto-login on host={host} path={request.path}")
            except Exception as e:
                print(f"[DEMO] auto-login error: {e}")
        else:
            return redirect(url_for("login"))
    if not current_user.is_authenticated:
        return redirect(url_for("login"))
    # Force onboarding on first login OR missing restaurant_id
    _needs_onboarding = current_user.first_login or not getattr(current_user, "restaurant_id", "")
    if _needs_onboarding and request.endpoint not in ("onboarding", "logout", "static"):
        return redirect(url_for("onboarding"))
    # Role enforcement
    page = _ENDPOINT_ROLE_MAP.get(request.endpoint)
    if page and not _check_role(page):
        return render_template("base.html", page="403"), 403
    # Sync from Airtable if needed (per-restaurant, with cooldown)
    rid = getattr(current_user, "restaurant_id", "")
    if rid and sync.needs_sync(rid):
        if sync.has_local_data(rid):
            # Data exists — serve stale, sync in background
            sync.sync_restaurant_async(rid)
        else:
            # First ever load — must block to populate SQLite
            try:
                sync.sync_restaurant(rid)
            except Exception as e:
                print(f"[SYNC] Failed in before_request: {e}")


# ---------------------------------------------------------------------------
#  Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("accueil"))
    error = None
    if request.method == "POST":
        user = verify_login(request.form.get("email", ""), request.form.get("password", ""))
        if user:
            login_user(user, remember=True)
            # Track last login in Airtable
            try:
                at.update("utilisateurs", user.id, {"Dernière_connexion": datetime.now().isoformat()})
            except Exception:
                pass
            if user.first_login:
                return redirect(url_for("onboarding"))
            return redirect(url_for("accueil"))
        error = "Email ou mot de passe incorrect."
    return render_template("login.html", mode="login", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/demo")
def demo_access():
    """Acces demo universel sans code — session bistrot_du_port."""
    demo_user = User("demo_session", "demo@paucoandco.com",
                     restaurant_id="bistrot_du_port",
                     restaurant_name="Le Bistrot du Port",
                     first_login=False, demo_mode=True)
    login_user(demo_user, remember=False)
    return redirect(url_for("accueil"))


@app.route("/demo/<code>")
def demo_code_access(code):
    """Acces demo avec code trackable — marque le code comme utilise."""
    code = code.strip().upper()
    try:
        rows = at.get_all("demo_codes", formula=f"{{Code}}='{code}'")
        if not rows:
            return redirect(url_for("login"))
        rec = rows[0]
        if not rec.get("Utilise"):
            at.update("demo_codes", rec["id"], {
                "Utilise": True,
                "Date_Utilisation": date.today().isoformat(),
                "IP": request.remote_addr or "",
                "Restaurant_Nom": "Le Bistrot du Port"
            })
    except Exception as e:
        print(f"[DEMO] Code tracking error: {e}")
    demo_user = User("demo_session", "demo@paucoandco.com",
                     restaurant_id="bistrot_du_port",
                     restaurant_name="Le Bistrot du Port",
                     first_login=False, demo_mode=True)
    login_user(demo_user, remember=False)
    return redirect(url_for("accueil"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    error = None
    success = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        token, uid = create_reset_token(email)
        if token:
            # Send email via Brevo
            brevo_key = os.environ.get("BREVO_API_KEY", "")
            base_url = request.host_url.rstrip("/")
            link = f"{base_url}/reset-password/{token}"
            if brevo_key:
                try:
                    import requests as _rq
                    _rq.post("https://api.brevo.com/v3/smtp/email", timeout=10,
                        headers={"api-key": brevo_key, "Content-Type": "application/json"},
                        json={
                            "sender": {"name": "Pauco", "email": "paul@paucoandco.com"},
                            "to": [{"email": email}],
                            "subject": "Reinitialisation mot de passe Pauco",
                            "htmlContent": f"<p>Cliquez sur ce lien pour reinitialiser votre mot de passe :</p><p><a href='{link}'>{link}</a></p><p>Ce lien est valable 1 heure.</p>",
                        })
                except Exception:
                    pass
            success = f"Si un compte existe avec cet email, un lien a ete envoye."
        else:
            success = f"Si un compte existe avec cet email, un lien a ete envoye."
    return render_template("login.html", mode="forgot", error=error, success=success)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password_page(token):
    uid = validate_reset_token(token)
    if not uid:
        return render_template("login.html", mode="login", error="Lien expire ou invalide. Demandez un nouveau lien.")
    error = None
    if request.method == "POST":
        pw1 = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw1) < 6:
            error = "Le mot de passe doit faire au moins 6 caracteres."
        elif pw1 != pw2:
            error = "Les mots de passe ne correspondent pas."
        else:
            reset_password(token, pw1)
            return render_template("login.html", mode="login", error=None)
    return render_template("login.html", mode="reset", error=error)


@app.route("/setup-password", methods=["GET", "POST"])
def setup_password():
    token = request.args.get("token", "") if request.method == "GET" else request.form.get("token", "")
    uid = validate_reset_token(token)
    if not uid:
        return render_template("login.html", mode="login",
                               error="Ce lien est expiré ou invalide. Contactez paul@paucoandco.com pour recevoir un nouveau lien.")
    error = None
    if request.method == "POST":
        pw1 = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw1) < 6:
            error = "Le mot de passe doit faire au moins 6 caracteres."
        elif pw1 != pw2:
            error = "Les mots de passe ne correspondent pas."
        else:
            reset_password(token, pw1)
            at.update("utilisateurs", uid, {"First_login": 0})
            return render_template("login.html", mode="login",
                                   success="Mot de passe créé avec succès ! Vous pouvez maintenant vous connecter.")
    return render_template("login.html", mode="setup", token=token, error=error)


@app.route("/set-password", methods=["GET", "POST"])
def set_password():
    """Invitation flow: set password and auto-login."""
    token = request.args.get("token", "") if request.method == "GET" else request.form.get("token", "")
    uid = validate_reset_token(token)
    if not uid:
        return render_template("login.html", mode="login",
                               error="Ce lien d'invitation est expire ou invalide. Demandez un nouveau lien a votre gerant.")
    error = None
    if request.method == "POST":
        pw1 = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw1) < 8:
            error = "Le mot de passe doit faire au moins 8 caracteres."
        elif pw1 != pw2:
            error = "Les mots de passe ne correspondent pas."
        else:
            reset_password(token, pw1)
            at.update("utilisateurs", uid, {"First_login": 0})
            # Auto-login
            try:
                rec = at.get_one("utilisateurs", uid)
                if rec:
                    from modules.auth import User, _check_demo_mode
                    rid = rec.get("Restaurant_ID", "")
                    resto = at.get_restaurant(rid) if rid else None
                    resto_nom = resto.get("Nom", "") if resto else ""
                    user = User(uid, rec.get("Email", ""), rid, resto_nom,
                                prenom=rec.get("Prenom", ""), nom=rec.get("Nom", ""),
                                role=rec.get("Role", "Staff"))
                    login_user(user, remember=True)
                    return redirect(url_for("accueil"))
            except Exception:
                pass
            return render_template("login.html", mode="login",
                                   success="Mot de passe cree ! Connectez-vous.")
    return render_template("login.html", mode="setup", token=token, error=error)


@app.route("/admin/create-account", methods=["GET", "POST"])
def admin_create_account():
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    error = None
    success = None

    # Load restaurants for the dropdown
    try:
        restos_raw = at.get_all("restaurants")
    except Exception:
        restos_raw = []
    restaurants = []
    for r in restos_raw:
        actif = r.get("Actif", True)
        if actif is None:
            actif = True
        archive = r.get("Archive", False) or False
        if actif and not archive:
            rid = r.get("Restaurant_ID", "") or r["id"]
            restaurants.append({"restaurant_id": rid, "nom": r.get("Nom", "")})
    restaurants.sort(key=lambda r: r.get("nom", ""))

    if request.method == "POST":
        if request.form.get("admin_password", "") != admin_pw or not admin_pw:
            error = "Mot de passe admin incorrect."
        else:
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "").strip()
            prenom = request.form.get("prenom", "").strip()
            nom = request.form.get("nom", "").strip()
            ville = request.form.get("ville", "").strip()
            account_type = request.form.get("account_type", "demo")
            if not email or not password:
                error = "Email et mot de passe requis."
            else:
                try:
                    if account_type == "client":
                        create_user(email, password, prenom=prenom, nom=nom, ville=ville, real_client=True)
                        type_label = "Client (nouveau restaurant)"
                    elif account_type == "demo":
                        create_user(email, password, prenom=prenom, nom=nom)
                        type_label = "Démo"
                    else:
                        # Assign to an existing restaurant
                        from werkzeug.security import generate_password_hash
                        pw_hash = generate_password_hash(password)
                        at.create("utilisateurs", {
                            "Email": email.lower().strip(),
                            "Password_hash": pw_hash,
                            "Prenom": prenom,
                            "Nom": nom,
                            "Restaurant_ID": account_type,
                            "Type": user_type_for_restaurant(account_type),
                            "First_login": 0,
                            "Actif": 1,
                            "Created_at": datetime.now().isoformat(),
                        })
                        type_label = f"Restaurant existant ({account_type})"
                    success = f"Compte créé ! Type : {type_label} — Email : {email}\nEnvoyez le mot de passe à l'utilisateur par un canal sécurisé."
                except Exception as e:
                    error = f"Erreur : {e}"
    return render_template("login.html", mode="admin", error=error, success=success,
                           restaurants=restaurants)


# ---------------------------------------------------------------------------
#  API : Create account (appelé par Make / automation)
# ---------------------------------------------------------------------------

@app.route("/api/admin/create-account", methods=["POST"])
def api_admin_create_account():
    """Endpoint API pour créer un compte client.

    Auth : header Authorization: Bearer <ADMIN_PASSWORD>
    Body JSON : {email, prenom, nom, restaurant_id}
    Crée l'utilisateur, génère un setup token, envoie l'email Brevo.
    """
    import secrets as _secrets

    # --- Auth par header ---
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    auth_header = request.headers.get("Authorization", "")
    token_val = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    if not admin_pw or token_val != admin_pw:
        return jsonify({"success": False, "error": "Non autorisé"}), 401

    # --- Parse body ---
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    prenom = (data.get("prenom") or "").strip()
    nom = (data.get("nom") or "").strip()
    restaurant_id = (data.get("restaurant_id") or "").strip()

    if not email:
        return jsonify({"success": False, "error": "Email requis"}), 400

    # --- Vérifier doublon : si existe, retourner le setup_token existant ou en créer un ---
    from modules.auth import _safe_formula_value, _get_auth_db
    existing = at.find_first_nocache("utilisateurs", f"{{Email}}='{_safe_formula_value(email)}'")
    if existing:
        user_id = existing["id"]
        db = _get_auth_db()
        row = db.execute(
            "SELECT token FROM password_resets WHERE user_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (user_id, datetime.now().isoformat())
        ).fetchone()
        if row:
            setup_token = row[0]
        else:
            setup_token = _secrets.token_urlsafe(32)
            expires = (datetime.now() + timedelta(hours=48)).isoformat()
            db.execute("INSERT INTO password_resets (user_id, token, expires_at) VALUES (?,?,?)",
                       (user_id, setup_token, expires))
            db.commit()
        db.close()
        setup_link = f"https://app.paucoandco.com/onboarding?token={setup_token}"
        return jsonify({"success": True, "user_id": user_id, "setup_link": setup_link, "existing": True})

    # --- Créer l'utilisateur (sans mot de passe, sera défini via setup-password) ---
    from werkzeug.security import generate_password_hash
    placeholder_hash = generate_password_hash(_secrets.token_urlsafe(32))
    user_fields = {
        "Email": email,
        "Password_hash": placeholder_hash,
        "Prenom": prenom,
        "Nom": nom,
        "Actif": 1,
        "First_login": 1,
        "Created_at": datetime.now().isoformat(),
    }
    if restaurant_id:
        user_fields["Restaurant_ID"] = restaurant_id
        user_fields["Type"] = user_type_for_restaurant(restaurant_id)

    try:
        rec = at.create("utilisateurs", user_fields)
    except Exception as e:
        return jsonify({"success": False, "error": f"Erreur création utilisateur: {e}"}), 500

    user_id = rec["id"]

    # --- Générer setup token (48h) dans SQLite ---
    setup_token = _secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=48)).isoformat()
    db = _get_auth_db()
    db.execute("INSERT INTO password_resets (user_id, token, expires_at) VALUES (?,?,?)",
               (user_id, setup_token, expires))
    db.commit()
    db.close()

    # --- Envoyer email Brevo ---
    setup_link = f"https://app.paucoandco.com/onboarding?token={setup_token}"
    brevo_key = os.environ.get("BREVO_API_KEY", "")
    if brevo_key:
        try:
            import requests as _rq
            _rq.post("https://api.brevo.com/v3/smtp/email", timeout=10,
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                json={
                    "sender": {"name": "Pauco", "email": "paul@paucoandco.com"},
                    "to": [{"email": email, "name": f"{prenom} {nom}".strip()}],
                    "subject": "Bienvenue chez Pauco — Configurez votre espace",
                    "htmlContent": (
                        f"<p>Bonjour {prenom or ''},</p>"
                        f"<p>Votre espace Pauco est prêt. Cliquez ci-dessous pour le configurer :</p>"
                        f"<p><a href='{setup_link}'>{setup_link}</a></p>"
                        f"<p>Ce lien est valable 48 heures.</p>"
                        f"<p>À très vite,<br>Paul — Pauco</p>"
                    ),
                })
        except Exception:
            pass

    return jsonify({"success": True, "user_id": user_id, "setup_link": setup_link})


# ---------------------------------------------------------------------------
#  Admin Dashboard — session-based auth
# ---------------------------------------------------------------------------

def _require_admin():
    """Check admin session. Returns True if authenticated, otherwise None."""
    if not session.get("admin_authenticated"):
        return None
    # Check 2h expiry
    expires = session.get("admin_expires", 0)
    if datetime.now().timestamp() > expires:
        session.pop("admin_authenticated", None)
        session.pop("admin_expires", None)
        return None
    # Refresh expiry on activity
    session["admin_expires"] = (datetime.now() + timedelta(hours=2)).timestamp()
    return True


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        admin_pw = os.environ.get("ADMIN_PASSWORD", "")
        if admin_pw and pw == admin_pw:
            session["admin_authenticated"] = True
            session["admin_expires"] = (datetime.now() + timedelta(hours=2)).timestamp()
            return redirect(url_for("admin_dashboard"))
        error = "Mot de passe incorrect."
    return render_template("login.html", mode="admin_login", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    session.pop("admin_expires", None)
    return redirect(url_for("admin_login"))


@app.route("/admin", methods=["GET"])
def admin_dashboard():
    if not _require_admin():
        return redirect(url_for("admin_login"))

    # Read all users from Airtable
    try:
        at_users = at.get_all("utilisateurs")
    except Exception:
        at_users = []
    users = []
    for u in at_users:
        users.append({
            "id": u["id"],
            "email": u.get("Email", ""),
            "restaurant_id": u.get("Restaurant_ID", "") or "",
            "created_at": u.get("Created_at", "") or "",
            "last_login": u.get("Dernière_connexion", "") or "",
            "first_login": u.get("First_login", 0),
            "actif": u.get("Actif", 1),
            "prenom": u.get("Prenom", "") or "",
            "nom": u.get("Nom", "") or "",
            "type": u.get("Type", "demo") or "demo",
        })
    # Sort: active first, then by email
    users.sort(key=lambda x: (0 if x["actif"] else 1, x["email"]))

    # Fetch all restaurants from Airtable
    try:
        restos_raw = at.get_all("restaurants")
    except Exception:
        restos_raw = []

    # One-time cleanup: archive restaurant_chez_paul duplicates
    for r in restos_raw:
        if r.get("Restaurant_ID") == "restaurant_chez_paul" and not r.get("Archive"):
            try:
                at.update("restaurants", r["id"], {"Archive": True, "Actif": False})
                print(f"[ADMIN] Archivé doublon restaurant_chez_paul: {r['id']}")
            except Exception as e:
                print(f"[ADMIN] Erreur archivage chez_paul: {e}")

    restaurants = []
    for r in restos_raw:
        rid = r.get("Restaurant_ID", "") or r["id"]
        actif = r.get("Actif", True)
        if actif is None:
            actif = True
        archive = r.get("Archive", False) or False
        user_count = sum(1 for u in users if u["restaurant_id"] == rid)
        restaurants.append({
            "restaurant_id": rid,
            "airtable_id": r["id"],
            "nom": r.get("Nom", ""),
            "ville": r.get("Ville", ""),
            "gerant_prenom": r.get("Gerant_prenom", ""),
            "gerant_nom": r.get("Gerant_nom", ""),
            "telephone": r.get("Téléphone", "") or r.get("Telephone", "") or "",
            "email": r.get("Email", ""),
            "user_count": user_count,
            "actif": actif,
            "archive": archive,
        })

    # Sort: active first, then inactive, then archived
    def _resto_sort_key(r):
        if r["archive"]:
            return 2
        if not r["actif"]:
            return 1
        return 0
    restaurants.sort(key=lambda r: (_resto_sort_key(r), r.get("nom", "")))

    # Stats (only count active, non-archived)
    active_restos = [r for r in restaurants if r["actif"] and not r["archive"]]
    demo_count = sum(1 for u in users if u.get("type") == "demo")
    stats = {
        "total_users": len(users),
        "total_restaurants": len(active_restos),
        "demo_count": demo_count,
        "client_count": len(users) - demo_count,
    }

    error = request.args.get("error", "")
    success = request.args.get("success", "")

    return render_template("admin.html",
        users=users, restaurants=restaurants, stats=stats,
        error=error, success=success)


@app.route("/admin/delete-user", methods=["POST"])
def admin_delete_user():
    if not _require_admin():
        return redirect(url_for("admin_login"))

    user_id = request.form.get("user_id", "").strip()
    if user_id:
        try:
            at.update("utilisateurs", user_id, {"Actif": 0})
            invalidate_user_cache(user_id)
        except Exception as e:
            return redirect(url_for("admin_dashboard", error=f"Erreur Airtable: {e}"))
    return redirect(url_for("admin_dashboard", success="Compte desactive"))


@app.route("/admin/reactivate-user", methods=["POST"])
def admin_reactivate_user():
    if not _require_admin():
        return redirect(url_for("admin_login"))

    user_id = request.form.get("user_id", "").strip()
    if user_id:
        try:
            at.update("utilisateurs", user_id, {"Actif": 1})
            invalidate_user_cache(user_id)
        except Exception as e:
            return redirect(url_for("admin_dashboard", error=f"Erreur Airtable: {e}"))
    return redirect(url_for("admin_dashboard", success="Compte reactive"))


@app.route("/admin/change-restaurant", methods=["POST"])
def admin_change_restaurant():
    if not _require_admin():
        return redirect(url_for("admin_login"))

    user_id = request.form.get("user_id", "").strip()
    new_rid = request.form.get("restaurant_id", "").strip()
    if user_id and new_rid:
        try:
            at.update("utilisateurs", user_id, {
                "Restaurant_ID": new_rid,
                "Type": user_type_for_restaurant(new_rid),
            })
            invalidate_user_cache(user_id)
        except Exception as e:
            return redirect(url_for("admin_dashboard", error=f"Erreur Airtable: {e}"))
    return redirect(url_for("admin_dashboard", success="Restaurant modifie"))


@app.route("/admin/edit-user", methods=["POST"])
def admin_edit_user():
    if not _require_admin():
        return redirect(url_for("admin_login"))

    user_id = request.form.get("user_id", "").strip()
    if not user_id:
        return redirect(url_for("admin_dashboard", error="Utilisateur introuvable"))

    prenom = request.form.get("prenom", "").strip()
    nom = request.form.get("nom", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "").strip()
    restaurant_id = request.form.get("restaurant_id", "").strip()

    fields = {
        "Prenom": prenom, "Nom": nom, "Email": email,
        "Restaurant_ID": restaurant_id,
        "Type": user_type_for_restaurant(restaurant_id),
    }
    if password:
        from werkzeug.security import generate_password_hash
        fields["Password_hash"] = generate_password_hash(password)
    try:
        at.update("utilisateurs", user_id, fields)
        invalidate_user_cache(user_id)
    except Exception as e:
        return redirect(url_for("admin_dashboard", error=f"Erreur Airtable: {e}"))
    return redirect(url_for("admin_dashboard", success="Utilisateur modifie"))


@app.route("/admin/create-restaurant", methods=["POST"])
def admin_create_restaurant():
    if not _require_admin():
        return redirect(url_for("admin_login"))

    import re
    resto_id = request.form.get("restaurant_id", "").strip()
    nom = request.form.get("nom", "").strip()
    prenom = request.form.get("prenom", "").strip()
    nom_gerant = request.form.get("nom_gerant", "").strip()
    ville = request.form.get("ville", "").strip()
    email = request.form.get("email", "").strip()

    if not resto_id or not nom:
        return redirect(url_for("admin_dashboard", error="Restaurant ID et nom requis"))

    if not re.fullmatch(r"[a-z0-9_]+", resto_id):
        return redirect(url_for("admin_dashboard", error="Restaurant ID invalide : minuscules, chiffres et underscores uniquement"))

    try:
        existing = at.find_first("restaurants", f"{{Restaurant_ID}}='{resto_id}'")
        if existing:
            return redirect(url_for("admin_dashboard", error="Cet ID est deja utilise"))
        result = at.create_restaurant(
            nom=nom, email=email, ville=ville,
            gerant_prenom=prenom, gerant_nom=nom_gerant,
            restaurant_id=resto_id
        )
        seed_default_event_types(resto_id)
        print(f"[ADMIN] Restaurant créé dans Airtable: {result}")
    except Exception as e:
        return redirect(url_for("admin_dashboard", error=f"Erreur Airtable: {e}"))
    return redirect(url_for("admin_dashboard", success=f"Restaurant {resto_id} cree"))


@app.route("/admin/edit-restaurant", methods=["POST"])
def admin_edit_restaurant():
    if not _require_admin():
        return redirect(url_for("admin_login"))

    airtable_id = request.form.get("airtable_id", "").strip()
    if not airtable_id:
        return redirect(url_for("admin_dashboard", error="Restaurant introuvable"))

    try:
        at.update("restaurants", airtable_id, {
            "Nom": request.form.get("nom", "").strip(),
            "Ville": request.form.get("ville", "").strip(),
            "Gerant_nom": request.form.get("gerant_nom", "").strip(),
            "Gerant_prenom": request.form.get("gerant_prenom", "").strip(),
            "Téléphone": request.form.get("telephone", "").strip(),
            "Email": request.form.get("email", "").strip(),
        })
    except Exception as e:
        return redirect(url_for("admin_dashboard", error=f"Erreur Airtable: {e}"))
    return redirect(url_for("admin_dashboard", success="Restaurant modifie"))


@app.route("/admin/deactivate-restaurant", methods=["POST"])
def admin_deactivate_restaurant():
    """Désactiver un restaurant — les utilisateurs liés ne peuvent plus se connecter."""
    if not _require_admin():
        return redirect(url_for("admin_login"))

    airtable_id = request.form.get("airtable_id", "").strip()
    resto_id = request.form.get("restaurant_id", "").strip()
    if not airtable_id:
        return redirect(url_for("admin_dashboard", error="Restaurant introuvable"))

    try:
        at.update("restaurants", airtable_id, {"Actif": False})
        # Désactiver tous les utilisateurs liés
        if resto_id:
            all_users = at.get_all("utilisateurs")
            for u in all_users:
                if u.get("Restaurant_ID") == resto_id and u.get("Actif", 1):
                    at.update("utilisateurs", u["id"], {"Actif": 0})
                    print(f"[ADMIN] Utilisateur {u.get('Email', '')} désactivé (restaurant {resto_id})")
        at.invalidate_cache("restaurants")
    except Exception as e:
        return redirect(url_for("admin_dashboard", error=f"Erreur: {e}"))
    return redirect(url_for("admin_dashboard", success=f"Restaurant {resto_id} desactive"))


@app.route("/admin/archive-restaurant", methods=["POST"])
def admin_archive_restaurant():
    """Archiver un restaurant — masqué de la liste principale, conservé dans Airtable."""
    if not _require_admin():
        return redirect(url_for("admin_login"))

    airtable_id = request.form.get("airtable_id", "").strip()
    resto_id = request.form.get("restaurant_id", "").strip()
    if not airtable_id:
        return redirect(url_for("admin_dashboard", error="Restaurant introuvable"))

    try:
        at.update("restaurants", airtable_id, {"Archive": True})
        at.invalidate_cache("restaurants")
    except Exception as e:
        return redirect(url_for("admin_dashboard", error=f"Erreur: {e}"))
    return redirect(url_for("admin_dashboard", success=f"Restaurant {resto_id} archive"))


@app.route("/admin/reactivate-restaurant", methods=["POST"])
def admin_reactivate_restaurant():
    """Réactiver un restaurant désactivé ou archivé."""
    if not _require_admin():
        return redirect(url_for("admin_login"))

    airtable_id = request.form.get("airtable_id", "").strip()
    resto_id = request.form.get("restaurant_id", "").strip()
    if not airtable_id:
        return redirect(url_for("admin_dashboard", error="Restaurant introuvable"))

    try:
        at.update("restaurants", airtable_id, {"Actif": True, "Archive": False})
        # Réactiver les utilisateurs liés
        if resto_id:
            all_users = at.get_all("utilisateurs")
            for u in all_users:
                if u.get("Restaurant_ID") == resto_id and not u.get("Actif", 1):
                    at.update("utilisateurs", u["id"], {"Actif": 1})
                    print(f"[ADMIN] Utilisateur {u.get('Email', '')} réactivé (restaurant {resto_id})")
        at.invalidate_cache("restaurants")
    except Exception as e:
        return redirect(url_for("admin_dashboard", error=f"Erreur: {e}"))
    return redirect(url_for("admin_dashboard", success=f"Restaurant {resto_id} reactive"))


@app.route("/admin/restaurant/<rid>")
def admin_restaurant_detail(rid):
    if not _require_admin():
        return redirect(url_for("admin_login"))

    try:
        resto = at.get_restaurant(rid)
    except Exception:
        resto = None
    if not resto:
        return redirect(url_for("admin_dashboard", error="Restaurant introuvable"))

    try:
        data = {
                "employes": at.get_all("employes", restaurant_id=rid),
                "ca_jour": sorted(at.get_all("ca_jour", restaurant_id=rid),
                                  key=lambda x: x.get("Date", ""), reverse=True),
                "depenses": at.get_all("depenses", restaurant_id=rid),
                "fiches": at.get_all("fiches", restaurant_id=rid),
                "planning": at.get_all("planning", restaurant_id=rid),
                "evenements": at.get_all("evenements", restaurant_id=rid),
            }
    except Exception:
        data = {"employes": [], "ca_jour": [], "depenses": [], "fiches": [], "planning": [], "evenements": []}

    return render_template("admin_restaurant.html", resto=resto, data=data)


@app.route("/admin/rapport-whatsapp/test", methods=["POST"])
def admin_test_rapport():
    """Envoie un rapport WhatsApp test pour un restaurant donné."""
    if not _require_admin():
        return redirect(url_for("admin_login"))

    rid = request.form.get("restaurant_id", "").strip()
    phone = request.form.get("phone", "").strip()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "fetch" in request.headers.get("Sec-Fetch-Mode", "")

    if not rid or not phone:
        msg = "Restaurant et numéro requis"
        return (f'<div class="flash flash-error">{msg}</div>', 400) if is_ajax else redirect(url_for("admin_dashboard", error=msg))

    message, err = build_rapport(rid)
    if err:
        msg = f"Erreur rapport: {err}"
        return (f'<div class="flash flash-error">{msg}</div>', 500) if is_ajax else redirect(url_for("admin_dashboard", error=msg))

    ok, send_err = send_whatsapp(phone, message)
    if ok:
        msg = f"Rapport test envoyé à {phone}"
        return (f'<div class="flash flash-success">{msg}</div>', 200) if is_ajax else redirect(url_for("admin_dashboard", success=msg))
    msg = f"Échec envoi: {send_err}"
    return (f'<div class="flash flash-error">{msg}</div>', 500) if is_ajax else redirect(url_for("admin_dashboard", error=msg))


@app.route("/admin/rapport-whatsapp/preview/<rid>")
def admin_preview_rapport(rid):
    """Prévisualise le rapport WhatsApp en texte brut."""
    if not _require_admin():
        return redirect(url_for("admin_login"))

    message, err = build_rapport(rid)
    if err:
        return f"<pre>Erreur: {err}</pre>", 500
    return f"<pre style='font-family:monospace;white-space:pre-wrap;max-width:600px;margin:40px auto;padding:20px;background:#f5f5f5;border-radius:8px'>{message}</pre>"


@app.route("/admin/backup/run", methods=["POST"])
def admin_backup_run():
    """Déclenche un backup Airtable manuellement. Auth par session admin ou header X-Admin-Password."""
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    header_pw = request.headers.get("X-Admin-Password", "")
    if not ((_require_admin()) or (admin_pw and header_pw == admin_pw)):
        return jsonify({"error": "Non autorisé"}), 401

    import threading
    def _bg():
        _run_backup()
    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "Backup lancé en arrière-plan"}), 202


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    import secrets as _secrets
    from werkzeug.security import generate_password_hash
    from modules.auth import _get_auth_db, _safe_formula_value

    # ── Mode session_id Stripe (cs_live_... ou cs_test_...) ──
    session_id = request.args.get("session_id", "") or request.form.get("session_id", "")
    if session_id and session_id.startswith("cs_"):
        app.logger.info(f"[ONBOARDING] Stripe session_id: {session_id}")
        step = int(request.form.get("step", "0"))
        if request.method == "POST" and step == 1:
            prenom = request.form.get("prenom", "").strip()
            nom = request.form.get("nom", "").strip()
            nom_restaurant = request.form.get("nom_restaurant", "").strip()
            adresse = request.form.get("adresse", "").strip()
            ville = request.form.get("ville", "").strip()
            email = request.form.get("email", "").strip().lower()
            telephone = request.form.get("telephone", "").strip()
            password = request.form.get("password", "")
            if not email or not nom_restaurant or not nom or not password or len(password) < 8:
                return render_template("onboarding.html", step=1, session_id=session_id,
                                       prenom=prenom, nom=nom, nom_restaurant=nom_restaurant,
                                       adresse=adresse, ville=ville, email=email, telephone=telephone,
                                       error="Tous les champs sont requis (mot de passe 8 caract\u00e8res min).")
            # Créer le restaurant + utilisateur
            import unicodedata as _ud2, re as _re2
            def _slug2(s):
                s = _ud2.normalize("NFD", s)
                s = "".join(c for c in s if _ud2.category(c) != "Mn")
                s = s.lower().strip()
                s = _re2.sub(r"[^a-z0-9]+", "_", s)
                return s.strip("_")
            resto_id = _slug2(nom_restaurant)
            # Vérifier doublon restaurant
            try:
                existing = at.find_first("restaurants", f"{{Restaurant_ID}}='{resto_id}'")
            except Exception:
                existing = None
            if not existing:
                try:
                    at.create_restaurant(nom=nom_restaurant, email=email, ville=ville,
                                         adresse=adresse,
                                         gerant_prenom=prenom, gerant_nom=nom, telephone=telephone,
                                         restaurant_id=resto_id)
                    seed_default_event_types(resto_id)
                except Exception as e:
                    app.logger.error(f"[ONBOARDING] Erreur creation restaurant: {e}")
            else:
                resto_id = existing.get("Restaurant_ID", resto_id)
            # Créer l'utilisateur
            from werkzeug.security import generate_password_hash as _gph
            pw_hash = _gph(password)
            try:
                safe_email = email.replace("'", "\\'")
                existing_user = at.find_first("utilisateurs", f"{{Email}}='{safe_email}'")
            except Exception:
                existing_user = None
            if not existing_user:
                try:
                    at.create("utilisateurs", {
                        "Email": email, "Password_hash": pw_hash,
                        "Prenom": prenom, "Nom": nom,
                        "Restaurant_ID": resto_id, "Type": "client",
                        "First_login": 0, "Actif": 1,
                        "Created_at": datetime.now().isoformat(),
                    })
                except Exception as e:
                    app.logger.error(f"[ONBOARDING] Erreur creation utilisateur: {e}")
            else:
                # Mettre à jour le mot de passe et le restaurant
                try:
                    at.update("utilisateurs", existing_user["id"], {
                        "Password_hash": pw_hash, "Restaurant_ID": resto_id,
                        "First_login": 0,
                    })
                except Exception as e:
                    app.logger.error(f"[ONBOARDING] Erreur update utilisateur: {e}")
            app.logger.info(f"[ONBOARDING] Compte cr\u00e9\u00e9: {email} -> {resto_id}")
            return render_template("onboarding.html", step=4, session_id="")
        # GET → afficher le formulaire step 1
        return render_template("onboarding.html", step=1, session_id=session_id,
                               prenom="", nom="", nom_restaurant="", adresse="", ville="", email="", telephone="")

    # ── Mode token (nouveau client, pas encore connecté) ──
    token = request.args.get("token", "") or request.form.get("token", "")
    if token:
        uid = validate_reset_token(token)
        if not uid:
            return render_template("login.html", mode="login",
                                   error="Ce lien est expiré ou invalide. Contactez paul@paucoandco.com pour recevoir un nouveau lien.")

        # Charger l'utilisateur depuis Airtable
        try:
            user_rec = at.get_one("utilisateurs", uid)
        except Exception:
            return render_template("login.html", mode="login",
                                   error="Utilisateur introuvable. Contactez paul@paucoandco.com")

        step = int(request.form.get("step", "0"))

        # ── POST : traiter l'étape soumise ──
        if request.method == "POST" and step > 0:

            if step == 1:
                # Sauvegarder infos personnelles dans Airtable
                prenom = request.form.get("prenom", "").strip()
                nom = request.form.get("nom", "").strip()
                telephone = request.form.get("telephone", "").strip()
                # Normaliser au format +33
                telephone = telephone.replace(" ", "").replace(".", "").replace("-", "")
                if telephone.startswith("0") and len(telephone) >= 10:
                    telephone = "+33" + telephone[1:]
                try:
                    at.update("utilisateurs", uid, {
                        "Prenom": prenom,
                        "Nom": nom,
                    })
                except Exception:
                    pass
                # Passer le téléphone à l'étape 2 (sera sauvé sur la fiche restaurant)
                return render_template("onboarding.html", step=2, token=token,
                                       email=user_rec.get("Email", ""),
                                       telephone=telephone)

            elif step == 2:
                # Créer le restaurant dans Airtable
                nom_restaurant = request.form.get("nom_restaurant", "").strip()
                ville = request.form.get("ville", "").strip()
                telephone = request.form.get("telephone", "").strip()
                # Normaliser au format +33
                telephone = telephone.replace(" ", "").replace(".", "").replace("-", "")
                if telephone.startswith("0") and len(telephone) >= 10:
                    telephone = "+33" + telephone[1:]
                prenom_user = user_rec.get("Prenom", "")
                nom_user = user_rec.get("Nom", "")

                if not nom_restaurant or not ville:
                    return render_template("onboarding.html", step=2, token=token,
                                           email=user_rec.get("Email", ""), telephone=telephone,
                                           error="Nom du restaurant et ville requis.")

                # Slug basé sur le nom du restaurant (ex: "le_bistrot_du_port")
                import unicodedata as _ud, re as _re
                def _slug(s):
                    s = _ud.normalize("NFD", s)
                    s = "".join(c for c in s if _ud.category(c) != "Mn")
                    s = s.lower().strip()
                    s = _re.sub(r"[^a-z0-9]+", "_", s)
                    return s.strip("_")
                resto_id = _slug(nom_restaurant)

                # Vérifier si l'utilisateur a déjà un restaurant (créé par webhook)
                existing_rid = user_rec.get("Restaurant_ID", "")
                if existing_rid:
                    resto_id = existing_rid
                    app.logger.info(f"[ONBOARDING] Restaurant deja lie: {resto_id}")
                else:
                    # Vérifier si ce Restaurant_ID ou cet email existe déjà dans Airtable
                    user_email = user_rec.get("Email", "")
                    try:
                        existing_by_rid = at.find_first("restaurants", f"{{Restaurant_ID}}='{resto_id}'")
                    except Exception:
                        existing_by_rid = None
                    try:
                        safe_email = user_email.replace("'", "\\'")
                        existing_by_email = at.find_first("restaurants", f"{{Email}}='{safe_email}'")
                    except Exception:
                        existing_by_email = None

                    if existing_by_rid:
                        # Restaurant_ID existe → ne pas écraser, utiliser l'existant
                        resto_id = existing_by_rid.get("Restaurant_ID", resto_id)
                        app.logger.info(f"[ONBOARDING] Restaurant_ID deja existant: {resto_id}")
                    elif existing_by_email:
                        resto_id = existing_by_email.get("Restaurant_ID", resto_id)
                        app.logger.info(f"[ONBOARDING] Email deja existant, restaurant: {resto_id}")
                    else:
                        # Créer le restaurant
                        gerant_complet = f"{prenom_user} {nom_user}".strip()
                        try:
                            at.create_restaurant(
                                nom=nom_restaurant,
                                email=user_email,
                                ville=ville,
                                gerant_prenom=prenom_user,
                                gerant_nom=nom_user,
                                telephone=telephone,
                                restaurant_id=resto_id,
                            )
                        except Exception as e:
                            return render_template("onboarding.html", step=2, token=token,
                                                   email=user_email,
                                                   nom_restaurant=nom_restaurant, ville=ville, telephone=telephone,
                                                   error=f"Erreur creation restaurant : {e}")

                # Lier l'utilisateur au restaurant
                try:
                    at.update("utilisateurs", uid, {
                        "Restaurant_ID": resto_id,
                        "Type": user_type_for_restaurant(resto_id),
                    })
                except Exception as e:
                    return render_template("onboarding.html", step=2, token=token,
                                           email=user_rec.get("Email", ""),
                                           nom_restaurant=nom_restaurant, ville=ville, telephone=telephone,
                                           error=f"Erreur liaison restaurant : {e}")

                return render_template("onboarding.html", step=3, token=token,
                                       email=user_rec.get("Email", ""))

            elif step == 3:
                # Définir le mot de passe
                pw1 = request.form.get("password", "")
                pw2 = request.form.get("password2", "")
                if len(pw1) < 8:
                    return render_template("onboarding.html", step=3, token=token,
                                           email=user_rec.get("Email", ""),
                                           error="Le mot de passe doit faire au moins 8 caracteres.")
                if pw1 != pw2:
                    return render_template("onboarding.html", step=3, token=token,
                                           email=user_rec.get("Email", ""),
                                           error="Les mots de passe ne correspondent pas.")

                # Hash et sauvegarde
                pw_hash = generate_password_hash(pw1)
                try:
                    at.update("utilisateurs", uid, {
                        "Password_hash": pw_hash,
                        "First_login": 0,
                    })
                except Exception:
                    pass

                # Marquer le token comme utilisé
                db = _get_auth_db()
                db.execute("UPDATE password_resets SET used=1 WHERE token=?", (token,))
                db.commit()
                db.close()

                # Générer contrat YouSign (non-bloquant)
                _create_yousign_contract(uid, user_rec)

                return render_template("onboarding.html", step=4, token="")

        # ── GET : afficher l'étape 1 ──
        return render_template("onboarding.html", step=1, token=token,
                               prenom=user_rec.get("Prenom", ""),
                               nom=user_rec.get("Nom", ""),
                               telephone=user_rec.get("Telephone", ""),
                               email=user_rec.get("Email", ""))

    # ── Mode legacy (utilisateur déjà connecté, first_login) ──
    if not current_user.is_authenticated:
        return render_template("login.html", mode="login",
                               error="Utilisez le lien re\u00e7u par email pour acc\u00e9der \u00e0 l'onboarding, ou connectez-vous.")

    if request.method == "POST":
        complete_onboarding(current_user.id, {
            "nom": request.form.get("nom", ""),
            "adresse": request.form.get("adresse", ""),
            "ville": request.form.get("ville", ""),
            "lat": request.form.get("lat", "48.8566"),
            "lng": request.form.get("lng", "2.3522"),
            "gerant_nom": request.form.get("gerant_nom", ""),
            "gerant_prenom": request.form.get("gerant_prenom", ""),
            "telephone": request.form.get("telephone", ""),
            "email": request.form.get("email", ""),
        })
        return redirect(url_for("accueil"))
    return render_template("onboarding_legacy.html", restaurant_name=current_user.restaurant_name)


# ---------------------------------------------------------------------------
#  Helpers — moved to helpers.py
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Donnees fictives E-reputation
# ---------------------------------------------------------------------------

def _fetch_avis_data():
    """Fetch avis from Airtable for current restaurant. Returns (avis_list, rapports_list)."""
    rid = current_user.restaurant_id
    try:
        raw = at.get_all("avis", restaurant_id=rid)
    except Exception:
        raw = []

    # Normalize Airtable records to template format
    avis = []
    for r in raw:
        d = r.get("Date", "") or ""
        # Airtable date is ISO "2026-03-22", convert to "22/03/2026" for display
        if d and len(d) >= 10:
            date_display = f"{d[8:10]}/{d[5:7]}/{d[:4]}"
        else:
            date_display = d
        avis.append({
            "date": date_display,
            "date_iso": d[:10] if d else "",
            "plateforme": r.get("Plateforme", "Google") or "Google",
            "auteur": r.get("Auteur", "Anonyme") or "Anonyme",
            "note": int(r.get("Note", 5) or 5),
            "texte": r.get("Texte", "") or "",
            "repondu": bool(r.get("Répondu", False)),
            "reponse": r.get("Réponse", "") or "",
        })
    avis.sort(key=lambda x: x.get("date_iso", ""), reverse=True)

    # Compute monthly reports dynamically
    from collections import defaultdict
    months = defaultdict(lambda: {"nb": 0, "notes": [], "repondu": 0})
    for a in avis:
        d = a.get("date_iso", "")
        if len(d) >= 7:
            mkey = d[:7]  # "2026-03"
            months[mkey]["nb"] += 1
            months[mkey]["notes"].append(a["note"])
            if a["repondu"]:
                months[mkey]["repondu"] += 1

    MOIS_LABELS = ["Janvier", "Fevrier", "Mars", "Avril", "Mai", "Juin",
                   "Juillet", "Aout", "Septembre", "Octobre", "Novembre", "Decembre"]
    # Build chronologically so evolution = current - previous
    rapports = []
    prev_note = None
    for mkey in sorted(months.keys()):
        m = months[mkey]
        avg_note = round(sum(m["notes"]) / len(m["notes"]), 1) if m["notes"] else 0
        taux = round(m["repondu"] / m["nb"] * 100) if m["nb"] > 0 else 0
        y, mo = mkey.split("-")
        label = f"{MOIS_LABELS[int(mo)-1]} {y}"
        if prev_note is not None:
            diff = round(avg_note - prev_note, 1)
            evo = f"{diff:+.1f}"
        else:
            evo = "Nouveau"
        rapports.append({"mois": label, "mois_key": mkey, "nb": m["nb"], "note": avg_note, "taux": taux, "evo": evo})
        prev_note = avg_note
    rapports.reverse()  # Newest first for display

    return avis, rapports


def _avis_stats(avis):
    """Compute overview stats from avis list."""
    from collections import Counter
    today = date.today()
    mois_str = f"{today.day:02d}/{today.month:02d}/{today.year}"[:3]  # not used
    mois_prefix = f"{today.month:02d}/{today.year}"

    total = len(avis)
    ce_mois = [a for a in avis if a.get("date", "").endswith(f"/{today.year}") and a.get("date", "")[3:5] == f"{today.month:02d}"]
    repondu_mois = sum(1 for a in ce_mois if a["repondu"])
    notes = [a["note"] for a in avis]
    note_moy = round(sum(notes) / len(notes), 1) if notes else 0
    taux_reponse = round(repondu_mois / len(ce_mois) * 100) if ce_mois else 0

    # Distribution
    dist = Counter(notes)
    dist_pct = {}
    for star in range(5, 0, -1):
        dist_pct[star] = round(dist.get(star, 0) / total * 100) if total > 0 else 0

    # Per platform
    plats = {}
    for a in avis:
        p = a["plateforme"]
        plats.setdefault(p, {"notes": [], "count": 0})
        plats[p]["notes"].append(a["note"])
        plats[p]["count"] += 1
    platform_stats = {}
    for p, d in plats.items():
        platform_stats[p] = {"note": round(sum(d["notes"]) / len(d["notes"]), 1), "count": d["count"]}

    # Monthly chart data (last 12 months)
    from collections import defaultdict
    monthly = defaultdict(lambda: {"notes": [], "count": 0})
    for a in avis:
        iso = a.get("date_iso", "")
        if len(iso) >= 7:
            monthly[iso[:7]]["notes"].append(a["note"])
            monthly[iso[:7]]["count"] += 1

    chart_months = sorted(monthly.keys())[-12:]
    MOIS_COURTS = ["Jan", "Fev", "Mar", "Avr", "Mai", "Jun", "Jul", "Aou", "Sep", "Oct", "Nov", "Dec"]
    chart_labels = [MOIS_COURTS[int(m[5:7])-1] for m in chart_months]
    chart_notes = [round(sum(monthly[m]["notes"])/len(monthly[m]["notes"]), 1) for m in chart_months]
    chart_counts = [monthly[m]["count"] for m in chart_months]

    return {
        "total": total,
        "ce_mois": len(ce_mois),
        "note_moy": note_moy,
        "taux_reponse": taux_reponse,
        "distribution": dist_pct,
        "platforms": platform_stats,
        "chart_labels": chart_labels,
        "chart_notes": chart_notes,
        "chart_counts": chart_counts,
    }


@app.route("/avis/overview")
def avis_overview():
    avis, _ = _fetch_avis_data()
    stats = _avis_stats(avis)
    return render_template("base.html", page="avis_overview", demo_avis=avis, avis_stats=stats)


@app.route("/avis/liste")
def avis_liste():
    avis, _ = _fetch_avis_data()
    return render_template("base.html", page="avis_liste", demo_avis_full=avis)


@app.route("/avis/statistiques")
def avis_statistiques():
    avis, rapports = _fetch_avis_data()
    stats = _avis_stats(avis)
    return render_template("base.html", page="avis_stats", demo_rapports=rapports, avis_stats=stats)


@app.route("/avis/rapports")
def avis_rapports():
    _, rapports = _fetch_avis_data()
    return render_template("base.html", page="avis_rapports", demo_rapports=rapports)


@app.route("/avis/export-pdf")
def avis_export_pdf():
    """Export PDF du rapport e-réputation."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm

    avis, rapports = _fetch_avis_data()
    stats = _avis_stats(avis)
    mois_req = request.args.get("mois", "")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20*mm, bottomMargin=15*mm,
                            leftMargin=15*mm, rightMargin=15*mm)
    styles = getSampleStyleSheet()
    elems = []

    # Title
    elems.append(Paragraph("Rapport E-Réputation", styles["Title"]))
    elems.append(Paragraph(f"{current_user.restaurant_name or current_user.restaurant_id}", styles["Heading2"]))
    if mois_req:
        elems.append(Paragraph(f"Mois : {mois_req}", styles["Normal"]))
    elems.append(Spacer(1, 8*mm))

    # KPIs
    kpi_data = [
        ["Note moyenne", "Total avis", "Taux de réponse"],
        [str(stats["note_moy"]), str(stats["total"]), f"{stats['taux_reponse']}%"],
    ]
    kpi_table = Table(kpi_data, colWidths=[60*mm, 60*mm, 60*mm])
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2D6A4A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 1), (-1, 1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E4DDD3")),
    ]))
    elems.append(kpi_table)
    elems.append(Spacer(1, 8*mm))

    # Monthly evolution table
    elems.append(Paragraph("Évolution mensuelle", styles["Heading3"]))
    rap_data = [["Mois", "Avis", "Note", "Taux réponse", "Évolution"]]
    for r in rapports[:6]:
        rap_data.append([r["mois"], str(r["nb"]), str(r["note"]), f"{r['taux']}%", r["evo"]])
    rap_table = Table(rap_data, colWidths=[45*mm, 20*mm, 20*mm, 35*mm, 30*mm])
    rap_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F4EF")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E4DDD3")),
    ]))
    elems.append(rap_table)
    elems.append(Spacer(1, 8*mm))

    # Recent reviews
    elems.append(Paragraph("Derniers avis", styles["Heading3"]))
    target_avis = avis
    if mois_req:
        target_avis = [a for a in avis if a.get("date_iso", "").startswith(mois_req)]
    for a in target_avis[:15]:
        stars = "★" * a["note"] + "☆" * (5 - a["note"])
        elems.append(Paragraph(
            f"<b>{a['auteur']}</b> — {a['plateforme']} — {stars} — {a.get('date', '')}",
            styles["Normal"]))
        elems.append(Paragraph(a["texte"][:200], styles["BodyText"]))
        if a["reponse"]:
            elems.append(Paragraph(f"<i>→ {a['reponse'][:150]}</i>", styles["BodyText"]))
        elems.append(Spacer(1, 3*mm))

    doc.build(elems)
    buf.seek(0)

    from flask import send_file
    filename = f"rapport-ereputation-{mois_req or 'complet'}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


# ---------------------------------------------------------------------------
#  Fiches Techniques
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
#  Fiches Cocktails
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
#  Allergenes
# ---------------------------------------------------------------------------

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
    cur = db.execute("INSERT INTO allergenes (nom_plat, categorie) VALUES (?, ?)", (nom, cat))
    db.commit()
    plat_id = cur.lastrowid
    at_id = ""
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    if rid:
        try:
            rec = at.create_allergene(rid, nom, cat)
            at_id = rec.get("id", "")
            db.execute("UPDATE allergenes SET airtable_id=? WHERE id=?", (at_id, plat_id))
            db.commit()
        except Exception:
            pass
    row = db.execute("SELECT * FROM allergenes WHERE restaurant_id=? AND id=?", (_rid(), plat_id,)).fetchone()
    return jsonify({"ok": True, "plat": dict(row)})


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
        from flask import send_file
        return send_file(buf, mimetype="image/png",
                         download_name=f"qr_allergenes_{rid}.png",
                         as_attachment=True)
    except Exception as e:
        return f"Erreur: {e}", 500


# ---------------------------------------------------------------------------
#  Reglages (Gerant only)
# ---------------------------------------------------------------------------

from modules.auth import ALL_PERMISSIONS as _ALL_PERMS, _FALLBACK_PRESETS, invalidate_roles_cache

_DEFAULT_ROLES = [
    ("Gerant", ",".join(_ALL_PERMS), True),
    ("Manager", ",".join(_FALLBACK_PRESETS["Manager"]), True),
    ("Staff", ",".join(_FALLBACK_PRESETS["Staff"]), True),
]


def _ensure_default_roles(rid, existing_roles):
    """Create default roles if none exist for this restaurant."""
    if existing_roles:
        return
    for nom, perms, is_def in _DEFAULT_ROLES:
        try:
            at.create_role(rid, nom, perms, is_def)
        except Exception:
            pass


@app.route("/gestion/reglages/roles")
@login_required
def reglages_roles():
    if "reglages" not in getattr(current_user, "permissions", set()):
        return render_template("base.html", page="403"), 403
    rid = current_user.restaurant_id
    at.invalidate_cache("roles")  # Always fresh data on this page
    roles = at.get_roles(rid)
    _ensure_default_roles(rid, roles)
    if not roles:
        roles = at.get_roles(rid)
    # Count users per role — restreint au restaurant courant côté serveur Airtable
    resto_users = at.get_all("utilisateurs", restaurant_id=rid)
    role_counts = {}
    for u in resto_users:
        r = u.get("Role", "Gerant")
        role_counts[r] = role_counts.get(r, 0) + 1
    return render_template("base.html", page="reglages_roles", roles=roles,
                           role_counts=role_counts, all_permissions=_ALL_PERMS)


@app.route("/gestion/reglages/roles/save", methods=["POST"])
@login_required
def reglages_roles_save():
    if "reglages" not in getattr(current_user, "permissions", set()):
        return jsonify({"ok": False}), 403
    data = request.get_json()
    action = data.get("action", "")
    rid = current_user.restaurant_id

    if action == "create":
        nom = data.get("nom", "").strip()
        perms = data.get("permissions", "")
        if not nom:
            return jsonify({"ok": False, "error": "Nom requis"}), 400
        try:
            rec = at.create_role(rid, nom, perms, False)
            at.invalidate_cache("roles")
            invalidate_roles_cache(rid)
            return jsonify({"ok": True, "id": rec.get("id", "")})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    elif action == "update":
        role_id = data.get("id", "")
        fields = {}
        if "nom" in data:
            fields["Nom"] = data["nom"].strip()
        if "permissions" in data:
            fields["Permissions"] = data["permissions"]
        if fields and role_id:
            try:
                at.update_role(role_id, fields)
                at.invalidate_cache("roles")
                invalidate_roles_cache(rid)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    elif action == "delete":
        role_id = data.get("id", "")
        if role_id:
            try:
                role = at.get_one("roles", role_id) if hasattr(at, 'get_one') else None
                if role and role.get("Is_default"):
                    return jsonify({"ok": False, "error": "Impossible de supprimer un role par defaut"}), 400
                at.delete_role(role_id)
                at.invalidate_cache("roles")
                invalidate_roles_cache(rid)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Action inconnue"}), 400


@app.route("/gestion/reglages/restaurant", methods=["GET", "POST"])
@login_required
def reglages_restaurant():
    if "reglages" not in getattr(current_user, "permissions", set()):
        return render_template("base.html", page="403"), 403
    rid = current_user.restaurant_id
    resto = at.get_restaurant(rid) or {}
    if request.method == "POST":
        fields = {}
        for key in ("nom", "ville", "telephone", "email", "adresse", "site_web",
                     "type_etablissement", "capacite_interieur", "capacite_terrasse",
                     "horaires_ouverture", "fermeture_hebdo", "fermeture_annuelle",
                     "siret", "tva_intra", "url_google", "url_tripadvisor", "url_booking"):
            val = request.form.get(key, "").strip()
            if key in ("capacite_interieur", "capacite_terrasse") and val:
                try:
                    val = int(val)
                except ValueError:
                    val = 0
            fields[key] = val
        try:
            at.update_restaurant(rid, **fields)
            at.invalidate_cache("restaurants")
            resto = at.get_restaurant(rid) or {}
        except Exception as e:
            print(f"[REGLAGES] Update error: {e}")
            import traceback
            traceback.print_exc()
        return render_template("base.html", page="reglages_restaurant", resto=resto, saved=True)
    return render_template("base.html", page="reglages_restaurant", resto=resto)


@app.route("/gestion/reglages/utilisateurs")
@login_required
def reglages_utilisateurs():
    if "reglages" not in getattr(current_user, "permissions", set()):
        return render_template("base.html", page="403"), 403
    rid = current_user.restaurant_id
    resto_users = at.get_all("utilisateurs", restaurant_id=rid)
    roles = at.get_roles(rid)
    _ensure_default_roles(rid, roles)
    roles = at.get_roles(rid)  # reload after ensuring defaults
    role_names = [r.get("Nom", "") for r in roles]
    return render_template("base.html", page="reglages_utilisateurs", users=resto_users, roles=roles, role_names=role_names)


@app.route("/gestion/reglages/utilisateurs/invite", methods=["POST"])
@login_required
def reglages_user_invite():
    if "reglages" not in getattr(current_user, "permissions", set()):
        return jsonify({"ok": False}), 403
    data = request.get_json()
    prenom = data.get("prenom", "").strip()
    nom = data.get("nom", "").strip()
    email = data.get("email", "").strip().lower()
    role = data.get("role", "Staff")
    if not email or not prenom:
        return jsonify({"ok": False, "error": "Prenom et email requis"}), 400
    # Accept any role name (custom roles from Roles table)
    if not role:
        role = "Staff"
    rid = current_user.restaurant_id

    # Check if email already exists
    existing = at.find_first_nocache("utilisateurs", f"{{Email}}='{email}'")
    if existing:
        return jsonify({"ok": False, "error": "Un compte existe deja avec cet email"}), 400

    import secrets as _secrets
    password = data.get("password", "").strip()
    try:
        # Create user in Airtable — with or without password
        pw_hash = ""
        if password:
            from werkzeug.security import generate_password_hash
            pw_hash = generate_password_hash(password)
        rec = at.create("utilisateurs", {
            "Email": email,
            "Password_hash": pw_hash,
            "Prenom": prenom,
            "Nom": nom,
            "Restaurant_ID": rid,
            "Role": role,
            "Actif": 1,
            "First_login": 0 if password else 1,
            "Created_at": datetime.now().isoformat(),
        })
        user_id = rec.get("id", "")

        # If password was set by the manager, skip email invitation
        if password:
            return jsonify({"ok": True, "password_set": True})

        # Generate invitation token (7 days expiry) using existing password_resets table
        token = _secrets.token_urlsafe(48)
        expires = (datetime.now() + __import__("datetime").timedelta(days=7)).isoformat()
        from modules.auth import _get_auth_db
        db_auth = _get_auth_db()
        db_auth.execute("INSERT INTO password_resets (user_id, token, expires_at) VALUES (?,?,?)",
                        (user_id, token, expires))
        db_auth.commit()
        db_auth.close()

        # Send invitation email via Brevo
        brevo_key = os.environ.get("BREVO_API_KEY", "")
        base_url = request.host_url.rstrip("/").replace("http://", "https://")
        link = f"{base_url}/set-password?token={token}"
        gerant_name = f"{current_user.prenom} {current_user.nom}".strip() or "Le gerant"
        resto_name = current_user.restaurant_name or "votre restaurant"
        email_sent = False
        if brevo_key:
            try:
                import requests as _rq
                resp = _rq.post("https://api.brevo.com/v3/smtp/email", timeout=10,
                    headers={"api-key": brevo_key, "Content-Type": "application/json"},
                    json={
                        "sender": {"name": "Pauco", "email": "paul@paucoandco.com"},
                        "to": [{"email": email, "name": f"{prenom} {nom}"}],
                        "subject": "Votre acces Pauco — definissez votre mot de passe",
                        "htmlContent": f"""<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:20px">
                            <h2 style="color:#0F1F14">Bienvenue sur Pauco</h2>
                            <p>{gerant_name} vous a invite a rejoindre <strong>{resto_name}</strong> sur Pauco en tant que <strong>{role}</strong>.</p>
                            <p>Cliquez sur le bouton ci-dessous pour definir votre mot de passe et acceder a votre espace :</p>
                            <p style="text-align:center;margin:24px 0"><a href="{link}" style="background:#0F1F14;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600">Definir mon mot de passe</a></p>
                            <p style="font-size:12px;color:#999">Ce lien est valable 7 jours. Si vous n'avez pas demande cet acces, ignorez cet email.</p>
                        </div>""",
                    })
                email_sent = resp.status_code in (200, 201)
            except Exception as e:
                print(f"[INVITE] Brevo error: {e}")

        return jsonify({"ok": True, "email_sent": email_sent, "link": link})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/gestion/reglages/utilisateurs/update", methods=["POST"])
@login_required
def reglages_user_update():
    if "reglages" not in getattr(current_user, "permissions", set()):
        return jsonify({"ok": False}), 403
    data = request.get_json()
    user_id = data.get("id", "")
    role = data.get("role", "")
    actif = data.get("actif")
    # Verify target user belongs to same restaurant
    if user_id:
        try:
            target = at.get_one("utilisateurs", user_id)
            if not target or target.get("Restaurant_ID", "") != current_user.restaurant_id:
                return jsonify({"ok": False, "error": "Utilisateur non trouve"}), 404
        except Exception:
            return jsonify({"ok": False, "error": "Utilisateur non trouve"}), 404
    fields = {}
    if role:
        fields["Role"] = role
    permissions = data.get("permissions")
    if permissions is not None:
        fields["Permissions"] = permissions
    if actif is not None:
        fields["Actif"] = 1 if actif else 0
    if fields and user_id:
        try:
            at.update("utilisateurs", user_id, fields)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/gestion/reglages/utilisateurs/delete", methods=["POST"])
@login_required
def reglages_user_delete():
    if "reglages" not in getattr(current_user, "permissions", set()):
        return jsonify({"ok": False}), 403
    data = request.get_json()
    user_id = data.get("id", "")
    if user_id == current_user.id:
        return jsonify({"ok": False, "error": "Impossible de supprimer votre propre compte"}), 400
    # Verify target user belongs to same restaurant
    if user_id:
        try:
            target = at.get_one("utilisateurs", user_id)
            if not target or target.get("Restaurant_ID", "") != current_user.restaurant_id:
                return jsonify({"ok": False, "error": "Utilisateur non trouve"}), 404
            at.update("utilisateurs", user_id, {"Actif": 0})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
#  Mon compte
# ---------------------------------------------------------------------------

@app.route("/mentions-legales")
def mentions_legales():
    return render_template("mentions_legales.html")


@app.route("/compte")
@login_required
def compte():
    user_rec = {}
    try:
        user_rec = at.get_one("utilisateurs", current_user.id) or {}
    except Exception:
        pass
    return render_template("compte.html",
        email=current_user.email,
        prenom=user_rec.get("Prenom", getattr(current_user, "prenom", "")),
        nom=user_rec.get("Nom", getattr(current_user, "nom", "")),
        telephone=user_rec.get("Telephone", "") or "")


@app.route("/compte/save", methods=["POST"])
@login_required
def compte_save():
    data = request.get_json()
    # Airtable Utilisateurs fields: Prenom, Nom, Email, Telephone (no accents)
    user_fields = {}
    if "prenom" in data:
        user_fields["Prenom"] = data["prenom"].strip()
    if "nom" in data:
        user_fields["Nom"] = data["nom"].strip()
    if "telephone" in data:
        user_fields["Telephone"] = data["telephone"].strip()
    if user_fields:
        try:
            at.update("utilisateurs", current_user.id, user_fields)
            from modules.auth import _user_meta_cache
            _user_meta_cache.pop(current_user.id, None)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True})


@app.route("/compte/password", methods=["POST"])
@login_required
def compte_password():
    from werkzeug.security import check_password_hash, generate_password_hash
    data = request.get_json()
    old_pw = data.get("old", "")
    new_pw = data.get("new", "")
    confirm = data.get("confirm", "")
    if len(new_pw) < 8:
        return jsonify({"ok": False, "error": "Le mot de passe doit faire au moins 8 caracteres"}), 400
    if new_pw != confirm:
        return jsonify({"ok": False, "error": "Les mots de passe ne correspondent pas"}), 400
    # Verify old password
    try:
        user_rec = at.get_one("utilisateurs", current_user.id)
        if not user_rec or not check_password_hash(user_rec.get("Password_hash", ""), old_pw):
            return jsonify({"ok": False, "error": "Mot de passe actuel incorrect"}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Erreur de verification"}), 500
    # Update password
    try:
        at.update("utilisateurs", current_user.id, {"Password_hash": generate_password_hash(new_pw)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/compte/export", methods=["POST"])
@login_required
def compte_export():
    print(f"[COMPTE] Export demande par {current_user.email}")
    return jsonify({"ok": True})


@app.route("/compte/supprimer", methods=["POST"])
@login_required
def compte_supprimer():
    print(f"[COMPTE] Suppression demandee par {current_user.email}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
#  Conformite legale
# ---------------------------------------------------------------------------

_CONFORMITE_DOCS = [
    {"id": "allergenes", "cat": "Affichages obligatoires", "nom": "Tableau des 14 allergènes",
     "desc": "Affichage réglementaire INCO n°1169/2011", "link": "/gestion/allergenes", "modele": False},
    {"id": "interdiction_fumer", "cat": "Affichages obligatoires", "nom": "Interdiction de fumer",
     "desc": "Affichage obligatoire dans tous les ERP", "modele": True},
    {"id": "numeros_urgence", "cat": "Affichages obligatoires", "nom": "Numéros d'urgence",
     "desc": "SAMU 15, Police 17, Pompiers 18, Prévention suicide 3114", "modele": True},
    {"id": "origine_viandes", "cat": "Affichages obligatoires", "nom": "Origine des viandes bovines",
     "desc": "Décret n°2002-1465 — obligatoire si vous servez du bœuf", "link": "/gestion/origine-viandes", "modele": False},
    {"id": "registre_personnel", "cat": "RH", "nom": "Registre unique du personnel",
     "desc": "Article L1221-13 du Code du travail", "modele": True},
    {"id": "duer", "cat": "RH", "nom": "Document unique d'évaluation des risques (DUER)",
     "desc": "Obligatoire dès le 1er salarié — modèle simplifié CHR", "modele": True},
    {"id": "horaires", "cat": "RH", "nom": "Affichage des horaires de travail",
     "desc": "Horaires collectifs affichés dans le local", "link": "/gestion/horaires", "modele": False},
    {"id": "formation_hygiene", "cat": "RH", "nom": "Attestation formation hygiène alimentaire",
     "desc": "DRAAF — 14h obligatoires pour au moins 1 personne", "modele": False},
    {"id": "temperatures", "cat": "Sanitaire", "nom": "Relevé de températures frigos",
     "desc": "Contrôle quotidien obligatoire — grille hebdomadaire", "link": "/gestion/temperatures", "modele": False},
    {"id": "pms_haccp", "cat": "Sanitaire", "nom": "Plan de maîtrise sanitaire (PMS/HACCP)",
     "desc": "Document établi par votre prestataire ou consultant HACCP — obligatoire depuis le règlement CE 852/2004", "modele": False},
    {"id": "nuisibles", "cat": "Sanitaire", "nom": "Contrat prestataire nuisibles",
     "desc": "Document fourni et signé par votre prestataire de dératisation/désinsectisation", "modele": False},
    {"id": "kbis", "cat": "Administratif", "nom": "Extrait Kbis",
     "desc": "Immatriculation au RCS — moins de 3 mois", "modele": False},
    {"id": "permis_exploitation", "cat": "Administratif", "nom": "Permis d'exploitation",
     "desc": "Formation obligatoire 20h — validité 10 ans", "modele": False},
    {"id": "assurance_rc", "cat": "Administratif", "nom": "Assurance RC Professionnelle",
     "desc": "Attestation annuelle d'assurance", "modele": False},
    {"id": "licence_alcool", "cat": "Administratif", "nom": "Licence de débit de boissons",
     "desc": "Licence III (bière/vin) ou IV (tous alcools)", "modele": False},
]


@app.route("/gestion/conformite")
@login_required
def conformite_page():
    rid = current_user.restaurant_id
    # Load saved states from Airtable
    saved = {}
    try:
        recs = at.get_all("conformite", rid)
        for r in recs:
            saved[r.get("Document_ID", "")] = {
                "possede": bool(r.get("Possede")),
                "fichier_url": r.get("Fichier_URL", "") or "",
                "date_upload": r.get("Date_upload", "") or "",
                "at_id": r["id"],
            }
    except Exception:
        pass
    # Build docs list with saved state
    docs = []
    for d in _CONFORMITE_DOCS:
        doc = dict(d)
        s = saved.get(d["id"], {})
        doc["possede"] = s.get("possede", False)
        doc["fichier_url"] = s.get("fichier_url", "")
        doc["date_upload"] = s.get("date_upload", "")
        doc["at_id"] = s.get("at_id", "")
        docs.append(doc)
    # Group by category
    cats = []
    seen = set()
    for d in docs:
        if d["cat"] not in seen:
            cats.append(d["cat"])
            seen.add(d["cat"])
    done = sum(1 for d in docs if d["possede"])
    return render_template("base.html", page="conformite", docs=docs, cats=cats,
                           done_count=done, total_docs=len(docs))


@app.route("/gestion/conformite/toggle", methods=["POST"])
@login_required
def conformite_toggle():
    data = request.get_json()
    doc_id = data.get("doc_id", "")
    possede = data.get("possede", False)
    rid = current_user.restaurant_id
    try:
        existing = at.find_first_nocache("conformite",
            f"AND({{Restaurant_ID}}='{rid}',{{Document_ID}}='{doc_id}')")
        if existing:
            at.update("conformite", existing["id"], {"Possede": bool(possede)})
        else:
            at.create("conformite", {
                "Restaurant_ID": rid, "Document_ID": doc_id, "Possede": bool(possede)
            })
        at.invalidate_cache("conformite")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/gestion/conformite/upload", methods=["POST"])
@login_required
def conformite_upload():
    doc_id = request.form.get("doc_id", "")
    file = request.files.get("file")
    if not file or not doc_id:
        return jsonify({"ok": False, "error": "Fichier requis"}), 400
    rid = current_user.restaurant_id
    # Upload to R2
    try:
        from modules.r2_client import upload_file, get_file_url, is_configured
        if not is_configured():
            return jsonify({"ok": False, "error": "Stockage R2 non configuré"}), 500
        folder = f"conformite/{rid}/{doc_id}"
        # sécurise le nom de fichier (espaces, accents, slashs)
        from werkzeug.utils import secure_filename
        safe_name = secure_filename(file.filename) or "document"
        r2_key = upload_file(file, folder, safe_name)
        file_url = get_file_url(r2_key)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[CONFORMITE-UPLOAD] {e}\n{tb}")
        try:
            with open("errors.log", "a", encoding="utf-8") as _lf:
                _lf.write(f"[{datetime.now().isoformat()}] conformite_upload rid={rid} doc={doc_id}: {e}\n{tb}\n")
        except Exception:
            pass
        return jsonify({"ok": False, "error": f"Upload R2 échoué : {e}"}), 500
    # Save in Airtable
    try:
        existing = at.find_first("conformite",
            f"AND({{Restaurant_ID}}='{rid}',{{Document_ID}}='{doc_id}')")
        fields = {"Fichier_URL": file_url, "Date_upload": datetime.now().strftime("%d/%m/%Y"),
                  "Possede": True}
        if existing:
            at.update("conformite", existing["id"], fields)
        else:
            fields["Restaurant_ID"] = rid
            fields["Document_ID"] = doc_id
            at.create("conformite", fields)
        at.invalidate_cache("conformite")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "url": file_url})


def _generate_interdiction_fumer_pdf(resto_name, today):
    """Generate a professional no-smoking sign PDF with sequential layout."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO
        import math

        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        cx = w / 2

        # Sequential top-down layout with cursor
        y = h - 60  # start 60pt from top

        # === PICTOGRAM (center, 100px radius) ===
        r = 90
        py = y - r  # pictogram center
        y = py - r - 30  # move cursor below pictogram + margin

        # Red circle
        c.setStrokeColor(HexColor("#DC2626"))
        c.setLineWidth(14)
        c.setFillColor(HexColor("#FFFFFF"))
        c.circle(cx, py, r, fill=True, stroke=True)

        # Cigarette body
        c.setFillColor(HexColor("#6B7280"))
        c.rect(cx - 50, py - 7, 65, 14, fill=True, stroke=False)
        # Filter
        c.setFillColor(HexColor("#F59E0B"))
        c.rect(cx - 50, py - 7, 18, 14, fill=True, stroke=False)
        # Tip
        c.setFillColor(HexColor("#E5E7EB"))
        c.rect(cx + 15, py - 5, 22, 10, fill=True, stroke=False)

        # Smoke
        c.setStrokeColor(HexColor("#9CA3AF"))
        c.setLineWidth(2)
        p = c.beginPath()
        p.moveTo(cx + 32, py + 10)
        p.curveTo(cx + 38, py + 30, cx + 26, py + 42, cx + 34, py + 55)
        c.drawPath(p, stroke=True, fill=False)
        p2 = c.beginPath()
        p2.moveTo(cx + 24, py + 12)
        p2.curveTo(cx + 30, py + 32, cx + 18, py + 44, cx + 26, py + 54)
        c.drawPath(p2, stroke=True, fill=False)

        # Diagonal bar
        c.setStrokeColor(HexColor("#DC2626"))
        c.setLineWidth(14)
        a = math.radians(45)
        c.line(cx - r * math.cos(a), py + r * math.sin(a),
               cx + r * math.cos(a), py - r * math.sin(a))

        # === TITLE ===
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 30)
        c.drawCentredString(cx, y, "INTERDICTION DE FUMER")
        y -= 36
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(cx, y, "ET DE VAPOTER")
        y -= 40

        # === SUBTITLE ===
        c.setFont("Helvetica", 17)
        c.setFillColor(HexColor("#374151"))
        c.drawCentredString(cx, y, "Dans l'ensemble de cet établissement")
        y -= 35

        # === LEGAL TEXT ===
        c.setFont("Helvetica", 10)
        c.setFillColor(HexColor("#6B7280"))
        c.drawCentredString(cx, y, "Conformément au décret n°2006-1386 du 15 novembre 2006")
        y -= 15
        c.drawCentredString(cx, y, "modifié par le décret n°2017-633 du 25 avril 2017")
        y -= 35

        # === PENALTIES BOX ===
        box_h = 72
        box_w = 350
        box_x = cx - box_w / 2
        box_y = y - box_h
        c.setStrokeColor(HexColor("#E5E7EB"))
        c.setLineWidth(1)
        c.setFillColor(HexColor("#F9FAFB"))
        c.roundRect(box_x, box_y, box_w, box_h, 8, fill=True, stroke=True)

        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(cx, box_y + box_h - 20, "SANCTIONS")
        c.setFont("Helvetica", 10)
        c.setFillColor(HexColor("#374151"))
        c.drawCentredString(cx, box_y + box_h - 38, "Contrevenant : amende forfaitaire de 68 €")
        c.drawCentredString(cx, box_y + box_h - 54, "Responsable des lieux : amende de 135 €")

        # === RESTAURANT NAME ===
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(cx, 75, resto_name)

        # === FOOTER ===
        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, 40, f"Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"interdiction_fumer_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        text = f"INTERDICTION DE FUMER ET DE VAPOTER\n{resto_name}\n\nAmende contrevenant: 68€\nAmende responsable: 135€\n\nGénéré par Pauco"
        return Response(text, mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=interdiction_fumer.txt"})


def _generate_numeros_urgence_pdf(resto_name, today):
    """Generate a professional emergency numbers poster PDF."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO

        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        cx = w / 2

        # Header bar
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h - 90, w, 90, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(cx, h - 40, "NUMÉROS D'URGENCE")
        c.setFont("Helvetica", 11)
        c.drawString(40, h - 70, resto_name)
        c.drawRightString(w - 40, h - 70, today)

        # Emergency numbers
        _NUMS = [
            ("15", "SAMU", "Urgence médicale", "#DC2626"),
            ("17", "POLICE / GENDARMERIE", "Agression, vol, accident", "#1D4ED8"),
            ("18", "POMPIERS", "Incendie, accident, secours", "#D97706"),
            ("112", "URGENCES EUROPÉEN", "Numéro universel", "#1D4ED8"),
            ("3114", "PRÉVENTION SUICIDE", "Écoute 24h/24, 7j/7", "#7C3AED"),
            ("01 40 05 48 48", "CENTRE ANTIPOISON", "Paris — 24h/24", "#059669"),
            ("3919", "VIOLENCES FEMMES", "Anonyme et gratuit", "#BE185D"),
        ]

        y = h - 130
        for num, name, desc, color in _NUMS:
            # Number badge — wider for long numbers
            c.setFillColor(HexColor(color))
            is_long = len(num) > 5
            badge_w = 145 if is_long else 110
            font_sz = 14 if is_long else 18
            c.roundRect(40, y - 4, badge_w, 36, 8, fill=True, stroke=False)
            c.setFillColor(HexColor("#FFFFFF"))
            c.setFont("Helvetica-Bold", font_sz)
            c.drawCentredString(40 + badge_w / 2, y + 6, num)

            # Name and description
            txt_x = 40 + badge_w + 15
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 13)
            c.drawString(txt_x, y + 14, name)
            c.setFillColor(HexColor("#6B7280"))
            c.setFont("Helvetica", 10)
            c.drawString(txt_x, y, desc)

            y -= 52

        # === ANGELA SECTION ===
        angela_y = y - 20
        angela_h = 210
        # Green background box
        c.setFillColor(HexColor("#D1FAE5"))
        c.setStrokeColor(HexColor("#6EE7B7"))
        c.setLineWidth(1.5)
        c.roundRect(30, angela_y - angela_h + 30, w - 60, angela_h, 12, fill=True, stroke=True)

        # Angela title
        tx = 50
        ty = angela_y + 10
        c.setFillColor(HexColor("#065F46"))
        c.setFont("Helvetica-Bold", 16)
        c.drawString(tx, ty, "MOT DE PASSE ANGELA")

        c.setFont("Helvetica", 10)
        c.setFillColor(HexColor("#065F46"))
        ty -= 20
        c.drawString(tx, ty, "Si un(e) client(e) demande à parler à Angela, ou commande un Angela,")
        ty -= 14
        c.drawString(tx, ty, "c'est qu'il/elle est en danger et a besoin d'aide discrète.")

        ty -= 24
        c.setFont("Helvetica-Bold", 11)
        c.drawString(tx, ty, "Que faire :")

        steps = [
            "1. Rester calme et naturel",
            "2. Emmener la personne dans un endroit sécurisé (bureau, réserve)",
            "3. Appeler le 17 ou le 3919",
            "4. Ne pas confronter l'accompagnant",
        ]
        c.setFont("Helvetica", 10)
        for step in steps:
            ty -= 16
            c.drawString(tx + 10, ty, step)

        ty -= 22
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(HexColor("#92400E"))
        c.drawString(tx, ty, "Ce dispositif est confidentiel — ne pas en parler en salle.")

        # Footer
        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, 30, f"Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"numeros_urgence_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        text = f"NUMÉROS D'URGENCE\n{resto_name}\n\nSAMU 15\nPolice 17\nPompiers 18\n112\n3114\nAntipoison 01 40 05 48 48\n3919\n\nMOT DE PASSE ANGELA\nSi un(e) client(e) demande Angela, il/elle a besoin d'aide.\n\nGénéré par Pauco"
        return Response(text, mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=numeros_urgence.txt"})


def _generate_registre_personnel_pdf(resto_name, today, rid):
    """Generate employee register PDF pre-filled with real data."""
    emps = at.get_employes(rid) if rid else []
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO

        buf = BytesIO()
        page_size = landscape(A4)
        c = pdf_canvas.Canvas(buf, pagesize=page_size)
        w, h = page_size
        cx = w / 2

        def _draw_header():
            c.setFillColor(HexColor("#0F1F14"))
            c.rect(0, h - 70, w, 70, fill=True, stroke=False)
            c.setFillColor(HexColor("#FFFFFF"))
            c.setFont("Helvetica-Bold", 18)
            c.drawCentredString(cx, h - 32, "REGISTRE UNIQUE DU PERSONNEL")
            c.setFont("Helvetica", 10)
            c.drawString(30, h - 55, resto_name)
            c.drawRightString(w - 30, h - 55, today)

        def _draw_table_header(y):
            headers = ["N°", "Nom", "Prénom", "Date nais.", "Nationalité",
                        "Emploi", "Qualification", "Contrat", "Entrée", "Sortie", "N° autor."]
            col_w = [30, 80, 70, 65, 65, 80, 75, 45, 60, 60, 70]
            c.setFillColor(HexColor("#E4DDD3"))
            c.rect(25, y - 2, w - 50, 20, fill=True, stroke=False)
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 7.5)
            x = 30
            for i, hdr in enumerate(headers):
                c.drawString(x, y + 4, hdr)
                x += col_w[i]
            return col_w

        _draw_header()
        y = h - 100
        col_w = _draw_table_header(y)
        y -= 22

        c.setFont("Helvetica", 8)
        num = 0
        for emp in emps:
            if not emp.get("Actif", True):
                continue
            num += 1
            if y < 50:
                c.showPage()
                _draw_header()
                y = h - 100
                col_w = _draw_table_header(y)
                y -= 22
                c.setFont("Helvetica", 8)

            prenom = emp.get("Prénom", emp.get("Prenom", ""))
            nom_emp = emp.get("Nom", "")
            dn = emp.get("Date_naissance", "") or ""
            poste = emp.get("Poste", "")
            contrat = emp.get("Type_contrat", "")
            entree = emp.get("Date_entrée", emp.get("Date_entree", "")) or ""
            sortie = emp.get("Date_fin", "") or ""

            # Format dates dd/mm/yyyy
            for _d in [dn, entree, sortie]:
                pass  # already formatted or empty

            vals = [str(num), nom_emp, prenom, dn[:10] if len(dn) >= 10 else dn,
                    "Française", poste, poste, contrat,
                    entree[:10] if len(entree) >= 10 else entree,
                    sortie[:10] if len(sortie) >= 10 else sortie, ""]

            # Alternate row color
            if num % 2 == 0:
                c.setFillColor(HexColor("#F9FAFB"))
                c.rect(25, y - 2, w - 50, 16, fill=True, stroke=False)

            c.setFillColor(HexColor("#374151"))
            x = 30
            for i, v in enumerate(vals):
                c.drawString(x, y + 2, v[:int(col_w[i] / 4.5)])
                x += col_w[i]
            y -= 18

        # Empty rows for future employees
        c.setStrokeColor(HexColor("#E5E7EB"))
        c.setLineWidth(0.5)
        for _ in range(max(5, 15 - num)):
            num += 1
            if y < 50:
                break
            c.setFillColor(HexColor("#F9FAFB" if num % 2 == 0 else "#FFFFFF"))
            c.rect(25, y - 2, w - 50, 16, fill=True, stroke=False)
            c.line(25, y - 2, w - 25, y - 2)
            c.setFillColor(HexColor("#D1D5DB"))
            c.setFont("Helvetica", 8)
            c.drawString(30, y + 2, str(num))
            y -= 18

        # Footer
        c.setFillColor(HexColor("#6B7280"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, 28, "Conformément à l'article L1221-13 du Code du travail")
        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(cx, 16, f"Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"registre_personnel_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        lines = [f"REGISTRE UNIQUE DU PERSONNEL — {resto_name} — {today}", ""]
        for emp in emps:
            lines.append(f"{emp.get('Nom','')} {emp.get('Prénom', emp.get('Prenom',''))} — {emp.get('Poste','')} — {emp.get('Type_contrat','')}")
        lines.append("\nGénéré par Pauco")
        return Response("\n".join(lines), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=registre_personnel.txt"})


def _generate_pms_pdf(resto_name, today, rid):
    """Generate a multi-page PMS/HACCP document."""
    resto = at.get_restaurant(rid) or {} if rid else {}
    equips = at.get_all("equipements_froid", rid) if rid else []
    emps = at.get_employes(rid) if rid else []
    ville = resto.get("Ville", "") or ""
    adresse = resto.get("Adresse", "") or ""
    gerant_prenom = resto.get("Gerant_prenom", "") or ""
    gerant_nom = resto.get("Gerant_nom", "") or ""
    gerant = f"{gerant_prenom} {gerant_nom}".strip() or "Le gérant"

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO

        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        cx = w / 2

        def _header(title, subtitle=""):
            c.setFillColor(HexColor("#0F1F14"))
            c.rect(0, h - 70, w, 70, fill=True, stroke=False)
            c.setFillColor(HexColor("#FFFFFF"))
            c.setFont("Helvetica-Bold", 16)
            c.drawCentredString(cx, h - 30, title)
            if subtitle:
                c.setFont("Helvetica", 10)
                c.drawCentredString(cx, h - 48, subtitle)
            c.setFont("Helvetica", 9)
            c.drawString(30, h - 62, resto_name)
            c.drawRightString(w - 30, h - 62, today)

        def _footer():
            c.setFillColor(HexColor("#9CA3AF"))
            c.setFont("Helvetica", 7)
            c.drawCentredString(cx, 18, f"PMS — {resto_name} — Généré par Pauco · paucoandco.com · {today}")

        def _section(y, title):
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 12)
            c.drawString(30, y, title)
            c.setStrokeColor(HexColor("#E4DDD3"))
            c.setLineWidth(1)
            c.line(30, y - 4, w - 30, y - 4)
            return y - 22

        def _table_header(y, cols, widths):
            c.setFillColor(HexColor("#E4DDD3"))
            c.rect(30, y - 2, w - 60, 18, fill=True, stroke=False)
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 7.5)
            x = 35
            for i, col in enumerate(cols):
                c.drawString(x, y + 3, col)
                x += widths[i]
            return y - 20

        def _table_row(y, vals, widths, alt=False):
            if alt:
                c.setFillColor(HexColor("#F9FAFB"))
                c.rect(30, y - 2, w - 60, 16, fill=True, stroke=False)
            c.setStrokeColor(HexColor("#E5E7EB"))
            c.setLineWidth(0.3)
            c.line(30, y - 2, w - 30, y - 2)
            c.setFillColor(HexColor("#374151"))
            c.setFont("Helvetica", 8)
            x = 35
            for i, v in enumerate(vals):
                c.drawString(x, y + 2, str(v)[:int(widths[i] / 4)])
                x += widths[i]
            return y - 16

        # ═══ PAGE 1 — IDENTIFICATION ═══
        _header("PLAN DE MAÎTRISE SANITAIRE (PMS)")
        y = h - 100
        y = _section(y, "1. IDENTIFICATION DE L'ÉTABLISSEMENT")
        info = [
            ("Établissement", resto_name),
            ("Adresse", adresse or "(à compléter)"),
            ("Ville", ville or "(à compléter)"),
            ("Responsable HACCP", gerant),
            ("Activité", "Restauration traditionnelle"),
            ("Agrément sanitaire", "(à compléter)"),
            ("Date de mise à jour", today),
        ]
        for label, val in info:
            c.setFillColor(HexColor("#6B7280"))
            c.setFont("Helvetica", 10)
            c.drawString(40, y, f"{label} :")
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 10)
            c.drawString(200, y, val)
            y -= 18

        y -= 20
        y = _section(y, "SOMMAIRE")
        sommaire = [
            "1. Identification de l'établissement",
            "2. Procédures de réception des marchandises",
            "3. Températures de stockage et chaîne du froid",
            "4. Plan de nettoyage et désinfection",
            "5. Formation et hygiène du personnel",
            "6. Gestion des déchets et nuisibles",
        ]
        c.setFont("Helvetica", 10)
        c.setFillColor(HexColor("#374151"))
        for s in sommaire:
            c.drawString(40, y, s)
            y -= 16
        _footer()

        # ═══ PAGE 2 — RÉCEPTION ═══
        c.showPage()
        _header("PMS — RÉCEPTION DES MARCHANDISES", "Procédure de contrôle à réception")
        y = h - 100
        y = _section(y, "2. PROCÉDURES DE RÉCEPTION")
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#374151"))
        c.drawString(40, y, "Tout produit non conforme est refusé et retourné au fournisseur avec mention sur le bon de livraison.")
        y -= 20

        cols = ["Produit", "T° réception", "Emballage", "DLC", "Fournisseur", "Signature"]
        ww = [100, 75, 70, 55, 100, 80]
        y = _table_header(y, cols, ww)
        produits = ["Viandes fraîches", "Poissons / fruits de mer", "Produits laitiers",
                    "Fruits et légumes", "Produits surgelés", "Produits secs", "Pain / viennoiseries"]
        temps = ["0 à 4°C", "0 à 2°C", "0 à 4°C", "Ambiante", "-18°C", "Ambiante", "Ambiante"]
        for i, (prod, temp) in enumerate(zip(produits, temps)):
            y = _table_row(y, [prod, temp, "☐ OK", "☐ OK", "", ""], ww, alt=i % 2 == 1)

        # Empty rows
        for i in range(5):
            y = _table_row(y, ["", "", "☐ OK", "☐ OK", "", ""], ww, alt=(len(produits) + i) % 2 == 1)
        _footer()

        # ═══ PAGE 3 — STOCKAGE ═══
        c.showPage()
        _header("PMS — STOCKAGE ET CHAÎNE DU FROID", "Températures de conservation")
        y = h - 100
        y = _section(y, "3. TEMPÉRATURES DE STOCKAGE")

        cols3 = ["Zone de stockage", "T° cible", "Fréquence", "Responsable"]
        ww3 = [180, 100, 100, 100]
        y = _table_header(y, cols3, ww3)

        actifs = [e for e in equips if e.get("Actif")]
        if actifs:
            for i, eq in enumerate(actifs):
                nom = eq.get("Nom", "")
                typ = eq.get("Type", "")
                cible = ""
                for t, cib in _TYPES_FROID:
                    if t == typ:
                        cible = cib
                freq = "2x/jour" if eq.get("Frequence") == "2x" else "1x/jour"
                y = _table_row(y, [nom, cible, freq, ""], ww3, alt=i % 2 == 1)
        else:
            for i, (zone, temp) in enumerate([("Frigo cuisine", "0 à 4°C"), ("Frigo bar", "0 à 4°C"),
                                               ("Chambre froide", "0 à 4°C"), ("Congélateur", "-18°C")]):
                y = _table_row(y, [zone, temp, "1x/jour", ""], ww3, alt=i % 2 == 1)

        y -= 20
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(HexColor("#0F1F14"))
        c.drawString(40, y, "Règle FIFO (First In, First Out)")
        y -= 14
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#374151"))
        for line in ["• Les produits les plus anciens sont utilisés en premier",
                     "• Étiqueter chaque produit avec la date d'ouverture",
                     "• DLC après ouverture : 3 jours maximum (sauf indication contraire)"]:
            c.drawString(50, y, line)
            y -= 14
        _footer()

        # ═══ PAGE 4 — NETTOYAGE ═══
        c.showPage()
        _header("PMS — PLAN DE NETTOYAGE", "Nettoyage et désinfection hebdomadaire")
        y = h - 100
        y = _section(y, "4. PLAN DE NETTOYAGE-DÉSINFECTION")

        jours = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        cols4 = ["Zone"] + jours + ["Produit", "Init."]
        ww4 = [95] + [42] * 7 + [80, 40]
        y = _table_header(y, cols4, ww4)
        zones = ["Plan de travail", "Frigos", "Sol cuisine", "Surfaces contact",
                 "Plonge", "WC / sanitaires", "Salle / tables", "Poubelles"]
        for i, zone in enumerate(zones):
            y = _table_row(y, [zone] + ["☐"] * 7 + ["", ""], ww4, alt=i % 2 == 1)
        # Empty rows
        for i in range(3):
            y = _table_row(y, [""] + ["☐"] * 7 + ["", ""], ww4, alt=(len(zones) + i) % 2 == 1)
        _footer()

        # ═══ PAGE 5 — PERSONNEL ═══
        c.showPage()
        _header("PMS — HYGIÈNE DU PERSONNEL", "Formation et règles d'hygiène")
        y = h - 100
        y = _section(y, "5. FORMATION ET HYGIÈNE DU PERSONNEL")

        # Employee list
        cols5 = ["Nom", "Prénom", "Poste", "Formé hygiène", "Date formation"]
        ww5 = [100, 80, 100, 80, 100]
        y = _table_header(y, cols5, ww5)
        actifs_emp = [e for e in emps if e.get("Actif", True)]
        for i, emp in enumerate(actifs_emp[:15]):
            prenom = emp.get("Prénom", emp.get("Prenom", ""))
            nom_e = emp.get("Nom", "")
            poste = emp.get("Poste", "")
            y = _table_row(y, [nom_e, prenom, poste, "☐ Oui  ☐ Non", "___/___/______"], ww5, alt=i % 2 == 1)

        y -= 20
        y = _section(y, "RÈGLES D'HYGIÈNE OBLIGATOIRES")
        regles = [
            "• Lavage des mains : avant manipulation, après passage aux toilettes, après manipulation de déchets",
            "• Tenue de travail propre, cheveux attachés, ongles courts et sans vernis",
            "• Pas de bijoux (bagues, bracelets, montres) en zone de préparation",
            "• Toute blessure doit être couverte par un pansement bleu détectable",
            "• En cas de maladie gastro-intestinale : interdiction de manipuler les denrées",
        ]
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#374151"))
        for r in regles:
            if y < 50:
                break
            c.drawString(40, y, r)
            y -= 14
        _footer()

        # ═══ PAGE 6 — DÉCHETS + SIGNATURE ═══
        c.showPage()
        _header("PMS — DÉCHETS ET NUISIBLES", "Gestion des déchets et lutte anti-nuisibles")
        y = h - 100
        y = _section(y, "6. GESTION DES DÉCHETS")
        dechets = [
            "• Tri sélectif : déchets alimentaires / emballages / verre / huiles usagées",
            "• Poubelles fermées, vidées quotidiennement, nettoyées chaque semaine",
            "• Zone de stockage déchets séparée de la zone de préparation",
            "• Huiles de friture : collecte par prestataire agréé",
        ]
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#374151"))
        for d in dechets:
            c.drawString(40, y, d)
            y -= 14

        y -= 20
        y = _section(y, "LUTTE CONTRE LES NUISIBLES")
        nuisibles = [
            "Prestataire 3D : _______________________________________________",
            "N° contrat : __________________     Fréquence : trimestrielle",
            "Date dernier passage : ___/___/______",
            "Certificat à jour : ☐ Oui  ☐ Non",
        ]
        for n in nuisibles:
            c.drawString(40, y, n)
            y -= 16

        # Signature
        y -= 30
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(30, y, "ENGAGEMENT DU RESPONSABLE")
        y -= 18
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#374151"))
        c.drawString(30, y, f"Je soussigné(e) {gerant}, gérant(e) de {resto_name},")
        y -= 14
        c.drawString(30, y, "certifie que le présent Plan de Maîtrise Sanitaire est appliqué dans mon établissement.")
        y -= 30
        c.drawString(30, y, f"Fait à {ville or '_______________'},  le ___/___/______")
        y -= 30
        c.drawString(30, y, "Signature : ___________________________________")
        _footer()

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"PMS_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        return Response(f"PMS — {resto_name}\nGénéré par Pauco", mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=PMS.txt"})


def _generate_duer_pdf(resto_name, today, rid):
    """Generate a multi-page DUER (occupational risk assessment)."""
    resto = at.get_restaurant(rid) or {} if rid else {}
    emps = at.get_employes(rid) if rid else []
    nb_emp = sum(1 for e in emps if e.get("Actif", True))
    ville = resto.get("Ville", "") or ""
    adresse = resto.get("Adresse", "") or ""
    gp = resto.get("Gerant_prenom", "") or ""
    gn = resto.get("Gerant_nom", "") or ""
    gerant = f"{gp} {gn}".strip() or "Le gérant"
    next_year = str(int(today.split("/")[2]) + 1) if len(today) >= 10 else "2027"

    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO

        buf = BytesIO()
        page = landscape(A4)
        c = pdf_canvas.Canvas(buf, pagesize=page)
        w, h = page
        cx = w / 2

        def _hdr(title, sub=""):
            c.setFillColor(HexColor("#0F1F14"))
            c.rect(0, h - 60, w, 60, fill=True, stroke=False)
            c.setFillColor(HexColor("#FFFFFF"))
            c.setFont("Helvetica-Bold", 14)
            c.drawCentredString(cx, h - 25, title)
            if sub:
                c.setFont("Helvetica", 8)
                c.drawCentredString(cx, h - 40, sub)
            c.setFont("Helvetica", 8)
            c.drawString(20, h - 52, resto_name)
            c.drawRightString(w - 20, h - 52, today)

        def _ftr():
            c.setFillColor(HexColor("#9CA3AF"))
            c.setFont("Helvetica", 7)
            c.drawCentredString(cx, 12, f"DUER — {resto_name} — Généré par Pauco · paucoandco.com · {today}")

        def _risk_table(y, risks):
            """Draw risk assessment table. Returns new y."""
            cols = ["Risque", "Description", "Exposés", "P", "G", "C", "Mesures de prévention", "Resp.", "Délai"]
            ww = [90, 120, 80, 22, 22, 22, 210, 50, 50]
            # Header
            c.setFillColor(HexColor("#E4DDD3"))
            c.rect(20, y - 2, w - 40, 16, fill=True, stroke=False)
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 6.5)
            x = 25
            for i, col in enumerate(cols):
                c.drawString(x, y + 2, col)
                x += ww[i]
            y -= 18
            # Rows
            for idx, r in enumerate(risks):
                if y < 40:
                    break
                if idx % 2 == 1:
                    c.setFillColor(HexColor("#F9FAFB"))
                    c.rect(20, y - 2, w - 40, 14, fill=True, stroke=False)
                c.setStrokeColor(HexColor("#E5E7EB"))
                c.setLineWidth(0.3)
                c.line(20, y - 2, w - 20, y - 2)
                c.setFillColor(HexColor("#374151"))
                c.setFont("Helvetica", 7)
                x = 25
                for i, v in enumerate(r):
                    # Criticité color
                    if i == 5 and str(v).isdigit():
                        crit = int(v)
                        if crit >= 6:
                            c.setFillColor(HexColor("#DC2626"))
                            c.setFont("Helvetica-Bold", 7)
                        elif crit >= 4:
                            c.setFillColor(HexColor("#D97706"))
                            c.setFont("Helvetica-Bold", 7)
                        else:
                            c.setFillColor(HexColor("#059669"))
                            c.setFont("Helvetica-Bold", 7)
                    maxch = int(ww[i] / 3.8)
                    c.drawString(x, y + 1, str(v)[:maxch])
                    x += ww[i]
                    c.setFillColor(HexColor("#374151"))
                    c.setFont("Helvetica", 7)
                y -= 14
            return y

        # ═══ PAGE 1 — IDENTIFICATION ═══
        c.setPageSize(A4)
        w1, h1 = A4
        cx1 = w1 / 2
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h1 - 90, w1, 90, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(cx1, h1 - 35, "DOCUMENT UNIQUE D'ÉVALUATION")
        c.drawCentredString(cx1, h1 - 58, "DES RISQUES PROFESSIONNELS")
        c.setFont("Helvetica", 9)
        c.drawCentredString(cx1, h1 - 78, "Conformément à l'article R.4121-1 du Code du travail")

        y = h1 - 130
        info = [
            ("Établissement", resto_name),
            ("Adresse", adresse or "(à compléter)"),
            ("Ville", ville or "(à compléter)"),
            ("Activité", "Restauration — Code NAF 5610A"),
            ("Effectif", f"{nb_emp} salarié{'s' if nb_emp > 1 else ''}"),
            ("Responsable de l'évaluation", gerant),
            ("Date de création", today),
            ("Date de mise à jour", today),
            ("Prochaine révision", f"___/___/{next_year}"),
        ]
        for label, val in info:
            c.setFillColor(HexColor("#6B7280"))
            c.setFont("Helvetica", 10)
            c.drawString(50, y, f"{label} :")
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 10)
            c.drawString(250, y, val)
            y -= 20

        y -= 30
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(50, y, "ÉCHELLE DE COTATION")
        y -= 18
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#374151"))
        for line in [
            "Probabilité (P) : 1 = Peu probable  |  2 = Probable  |  3 = Très probable",
            "Gravité (G) :     1 = Bénin          |  2 = Sérieux    |  3 = Grave",
            "Criticité (C) :   C = P × G  |  1-3 = Acceptable  |  4-5 = À surveiller  |  6-9 = Prioritaire",
        ]:
            c.drawString(60, y, line)
            y -= 14

        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(cx1, 20, f"DUER — {resto_name} — Généré par Pauco · paucoandco.com · {today}")

        # ═══ PAGE 2 — CUISINE ═══
        c.showPage()
        c.setPageSize(page)
        w, h = page
        cx = w / 2
        _hdr("DUER — UNITÉ DE TRAVAIL : CUISINE")
        y = h - 80
        cuisine_risks = [
            ["Brûlures", "Four, plaques, friteuse, liquides chauds", "Cuisiniers", "2", "3", "6", "EPI (gants, manches), formation, signalétique", "", ""],
            ["Coupures", "Couteaux, mandoline, boîtes de conserve", "Cuisiniers", "3", "2", "6", "Gants anti-coupure, formation, couteaux entretenus", "", ""],
            ["Chutes plain-pied", "Sols glissants (graisse, eau)", "Personnel cuisine", "2", "2", "4", "Chaussures antidérapantes, nettoyage régulier", "", ""],
            ["Port de charges", "Caisses, marmites, sacs", "Cuisiniers", "3", "2", "6", "Formation gestes et postures, aide mécanique", "", ""],
            ["Stress / surcharge", "Coup de feu, horaires décalés", "Encadrement", "2", "2", "4", "Planning adapté, pauses obligatoires", "", ""],
            ["Risque chimique", "Produits de nettoyage/désinfection", "Plongeur", "2", "2", "4", "EPI, fiches de sécurité, stockage séparé", "", ""],
            ["Risque électrique", "Équipements défectueux", "Tout le personnel", "1", "3", "3", "Contrôle annuel, maintenance préventive", "", ""],
            ["Incendie", "Huile, gaz, équipements", "Tout le personnel", "1", "3", "3", "Extincteur, formation évacuation", "", ""],
        ]
        y = _risk_table(y, cuisine_risks)
        _ftr()

        # ═══ PAGE 3 — SALLE ═══
        c.showPage()
        _hdr("DUER — UNITÉ DE TRAVAIL : SALLE")
        y = h - 80
        salle_risks = [
            ["Chutes plain-pied", "Sol glissant, obstacles, escaliers", "Serveurs", "2", "2", "4", "Chaussures adaptées, éclairage, rangement", "", ""],
            ["TMS", "Port de plateaux, postures contraignantes", "Serveurs", "3", "2", "6", "Formation gestes, rotation des tâches, pauses", "", ""],
            ["Agression / stress", "Clients agressifs, rythme intense", "Tout le personnel", "1", "2", "2", "Formation gestion conflits, mot de passe Angela", "", ""],
            ["Risque routier", "Déplacements, livraisons", "Manager", "1", "3", "3", "Formation sécurité routière", "", ""],
            ["Bruit", "Musique, ambiance, vaisselle", "Serveurs", "2", "1", "2", "Limitation décibels, pauses en zone calme", "", ""],
            ["Brûlures", "Plats chauds, café, soupe", "Serveurs", "2", "2", "4", "Plateaux adaptés, signalisation plats chauds", "", ""],
        ]
        y = _risk_table(y, salle_risks)
        _ftr()

        # ═══ PAGE 4 — BAR ═══
        c.showPage()
        _hdr("DUER — UNITÉ DE TRAVAIL : BAR")
        y = h - 80
        bar_risks = [
            ["Coupures", "Verres cassés, bouteilles, shaker", "Barman", "2", "2", "4", "Formation, EPI, poubelle verre dédiée", "", ""],
            ["Chutes plain-pied", "Sol glissant (liquides renversés)", "Barman", "2", "2", "4", "Tapis antidérapants, nettoyage immédiat", "", ""],
            ["Risque alcool", "Proximité permanente avec l'alcool", "Barman", "1", "2", "2", "Sensibilisation, procédure refus de service", "", ""],
            ["TMS", "Station debout prolongée, gestes répétitifs", "Barman", "2", "2", "4", "Tapis de confort, rotation, pauses", "", ""],
            ["Bruit", "Musique, ambiance nocturne", "Barman", "2", "1", "2", "Limitation volume, protections auditives", "", ""],
        ]
        y = _risk_table(y, bar_risks)
        _ftr()

        # ═══ PAGE 5 — PLAN D'ACTION ═══
        c.showPage()
        _hdr("DUER — PLAN D'ACTION PRIORITAIRE", "Actions pour les risques de criticité ≥ 6")
        y = h - 80
        cols5 = ["Action", "Unité", "Responsable", "Délai", "Coût", "Réalisé"]
        ww5 = [280, 70, 80, 70, 70, 50]
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(20, y - 2, w - 40, 16, fill=True, stroke=False)
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 7)
        x = 25
        for i, col in enumerate(cols5):
            c.drawString(x, y + 2, col)
            x += ww5[i]
        y -= 18
        actions = [
            ["Achat gants anti-coupure pour la cuisine", "Cuisine", "", "1 mois", "50 €", "☐"],
            ["Formation gestes et postures — tout le personnel", "Tous", "", "3 mois", "300 €", "☐"],
            ["Mise en place chaussures antidérapantes obligatoires", "Tous", "", "1 mois", "150 €", "☐"],
            ["Formation HACCP / hygiène annuelle", "Cuisine", "", "6 mois", "200 €", "☐"],
            ["Contrôle électrique annuel des équipements", "Cuisine", "", "12 mois", "250 €", "☐"],
            ["Affichage numéros d'urgence + mot de passe Angela", "Salle", "", "Immédiat", "0 €", "☐"],
        ]
        c.setFont("Helvetica", 7)
        for idx, a in enumerate(actions):
            if idx % 2 == 1:
                c.setFillColor(HexColor("#F9FAFB"))
                c.rect(20, y - 2, w - 40, 14, fill=True, stroke=False)
            c.setStrokeColor(HexColor("#E5E7EB"))
            c.line(20, y - 2, w - 20, y - 2)
            c.setFillColor(HexColor("#374151"))
            x = 25
            for i, v in enumerate(a):
                c.drawString(x, y + 1, v[:int(ww5[i] / 3.5)])
                x += ww5[i]
            y -= 14
        # Empty rows
        for i in range(5):
            c.setStrokeColor(HexColor("#E5E7EB"))
            c.line(20, y - 2, w - 20, y - 2)
            y -= 14
        _ftr()

        # ═══ PAGE 6 — SIGNATURE ═══
        c.showPage()
        c.setPageSize(A4)
        w1, h1 = A4
        cx1 = w1 / 2
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h1 - 70, w1, 70, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(cx1, h1 - 35, "DUER — SIGNATURE ET MISE À JOUR")
        c.setFont("Helvetica", 9)
        c.drawCentredString(cx1, h1 - 55, resto_name)

        y = h1 - 110
        c.setFillColor(HexColor("#374151"))
        c.setFont("Helvetica", 10)
        paragraphs = [
            "Le présent Document Unique d'Évaluation des Risques Professionnels a été établi",
            "après consultation des représentants du personnel (ou des salariés en l'absence",
            "de représentants).",
            "",
            "Conformément à l'article R.4121-2 du Code du travail, ce document est mis à jour :",
            "  • Au moins une fois par an",
            "  • Lors de toute décision d'aménagement modifiant les conditions de travail",
            "  • Lorsqu'une information nouvelle concernant un risque est recueillie",
            "",
            "Ce document est tenu à la disposition :",
            "  • Des salariés et anciens salariés",
            "  • De l'inspection du travail",
            "  • Du médecin du travail",
            "  • Des agents de la CARSAT",
        ]
        for p in paragraphs:
            c.drawString(50, y, p)
            y -= 16

        y -= 30
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(50, y, "ENGAGEMENT")
        y -= 20
        c.setFont("Helvetica", 10)
        c.setFillColor(HexColor("#374151"))
        c.drawString(50, y, f"Je soussigné(e) {gerant}, gérant(e) de {resto_name},")
        y -= 16
        c.drawString(50, y, "certifie avoir réalisé l'évaluation des risques professionnels de mon établissement")
        y -= 16
        c.drawString(50, y, "et m'engage à mettre en œuvre les mesures de prévention identifiées.")
        y -= 30
        c.drawString(50, y, f"Fait à {ville or '_______________'},  le ___/___/______")
        y -= 30
        c.drawString(50, y, "Signature : ___________________________________")

        y -= 40
        c.setFillColor(HexColor("#6B7280"))
        c.setFont("Helvetica", 9)
        c.drawString(50, y, f"Date de prochaine révision obligatoire : ___/___/{next_year}")

        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(cx1, 20, f"DUER — {resto_name} — Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"DUER_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        return Response(f"DUER — {resto_name}\nGénéré par Pauco", mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=DUER.txt"})


_JOURS_SEMAINE = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
_JOURS_COURTS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

_DEFAULT_JOURS_CONFIG = [
    {"jour": "Lundi", "ouvert": False, "type": "repos"},
    {"jour": "Mardi", "ouvert": True, "type": "coupure", "midi_deb": "11:30", "midi_fin": "15:30", "soir_deb": "18:30", "soir_fin": "23:30"},
    {"jour": "Mercredi", "ouvert": True, "type": "coupure", "midi_deb": "11:30", "midi_fin": "15:30", "soir_deb": "18:30", "soir_fin": "23:30"},
    {"jour": "Jeudi", "ouvert": True, "type": "coupure", "midi_deb": "11:30", "midi_fin": "15:30", "soir_deb": "18:30", "soir_fin": "23:30"},
    {"jour": "Vendredi", "ouvert": True, "type": "coupure", "midi_deb": "11:30", "midi_fin": "15:30", "soir_deb": "18:30", "soir_fin": "23:30"},
    {"jour": "Samedi", "ouvert": True, "type": "coupure", "midi_deb": "11:30", "midi_fin": "15:30", "soir_deb": "18:30", "soir_fin": "23:30"},
    {"jour": "Dimanche", "ouvert": False, "type": "repos"},
]


def _get_horaires(rid):
    """Charge les horaires depuis la table Horaires_Travail (1 ligne par jour).

    Migration transparente : si l'ancien format mono-ligne avec Config_Jours JSON
    existe et qu'aucun enregistrement multi-rows n'est présent, on parse et seed.
    """
    import json as _json
    try:
        # Double invalidation : avec ET sans rid pour purger toutes les clés
        at.invalidate_cache("horaires_travail", rid)
        at.invalidate_cache("horaires_travail")
        recs = at.get_all("horaires_travail", rid)
        print(f"[HORAIRES READ] rid={rid} fetched {len(recs)} records")
    except Exception as e:
        print(f"[HORAIRES READ] error: {e}")
        recs = []

    # Index par jour des nouvelles lignes (avec champ Jour)
    by_jour = {}
    legacy_rec = None
    orphans_no_jour = []  # records sans Jour ni Config_Jours → à purger
    for r in recs:
        jour = (r.get("Jour") or "").strip()
        if jour:
            # Si plusieurs records pour le même jour (cas pathologique), on garde le plus ancien
            if jour in by_jour:
                # Doublon : le nouveau est ignoré (déjà mappé). Log pour audit.
                print(f"[HORAIRES READ] doublon ignoré pour {jour}: {r['id']} (gardé: {by_jour[jour]['id']})")
            else:
                by_jour[jour] = r
        elif r.get("Config_Jours"):
            legacy_rec = r
        else:
            orphans_no_jour.append(r["id"])
    if orphans_no_jour:
        print(f"[HORAIRES READ] {len(orphans_no_jour)} records orphelins sans Jour: {orphans_no_jour}")

    # Migration : si pas de multi-rows mais legacy Config_Jours présent, on importe
    if not by_jour and legacy_rec:
        try:
            jours_legacy = _json.loads(legacy_rec.get("Config_Jours") or "[]")
            for j in jours_legacy:
                _upsert_horaire_jour(rid, j)
            recs = at.get_all("horaires_travail", rid)
            for r in recs:
                jour = (r.get("Jour") or "").strip()
                if jour:
                    by_jour[jour] = r
        except Exception as e:
            print(f"[HORAIRES] legacy migration error: {e}")

    # Si toujours rien → seed les défauts
    if not by_jour:
        for j in _DEFAULT_JOURS_CONFIG:
            try:
                _upsert_horaire_jour(rid, j)
            except Exception as e:
                print(f"[HORAIRES] seed default error: {e}")
        try:
            recs = at.get_all("horaires_travail", rid)
            for r in recs:
                jour = (r.get("Jour") or "").strip()
                if jour:
                    by_jour[jour] = r
        except Exception:
            pass

    # Construit la liste ordonnée des 7 jours
    jours = []
    for jour_nom in _JOURS_SEMAINE:
        rec = by_jour.get(jour_nom)
        if rec:
            jtype = (rec.get("Type_Service") or "repos").strip().lower()
            jours.append({
                "jour": jour_nom,
                "type": jtype,
                "ouvert": jtype not in ("repos", "ferme"),
                "midi_deb": rec.get("Midi_Debut") or "",
                "midi_fin": rec.get("Midi_Fin") or "",
                "soir_deb": rec.get("Soir_Debut") or "",
                "soir_fin": rec.get("Soir_Fin") or "",
                "_at_id": rec["id"],
            })
        else:
            # Fallback default si jour absent
            default = next((d for d in _DEFAULT_JOURS_CONFIG if d["jour"] == jour_nom), {"jour": jour_nom, "ouvert": False, "type": "repos"})
            jours.append(dict(default))

    return {"jours": jours}


def _upsert_horaire_jour(rid, jour_dict):
    """Upsert d'une ligne (Restaurant_ID, Jour) dans Horaires_Travail."""
    jour_nom = (jour_dict.get("jour") or "").strip()
    if not jour_nom or not rid:
        print(f"[HORAIRES UPSERT] skip jour vide ou rid manquant : rid={rid!r} jour={jour_nom!r}")
        return
    jtype = (jour_dict.get("type") or "").strip() or ("repos" if not jour_dict.get("ouvert") else "coupure")
    fields = {
        "Restaurant_ID": rid,
        "Jour": jour_nom,
        "Type_Service": jtype,
        "Midi_Debut": jour_dict.get("midi_deb") or "",
        "Midi_Fin": jour_dict.get("midi_fin") or "",
        "Soir_Debut": jour_dict.get("soir_deb") or "",
        "Soir_Fin": jour_dict.get("soir_fin") or "",
    }
    rid_esc = rid.replace("'", "\\'")
    jour_esc = jour_nom.replace("'", "\\'")
    formula = f"AND({{Restaurant_ID}}='{rid_esc}',{{Jour}}='{jour_esc}')"
    existing = at.find_first_nocache("horaires_travail", formula)
    if existing:
        print(f"[HORAIRES UPSERT] UPDATE {existing['id']} ({jour_nom}) → {fields}")
        at.update("horaires_travail", existing["id"], fields)
    else:
        print(f"[HORAIRES UPSERT] CREATE ({jour_nom}) → {fields}")
        at.create("horaires_travail", fields)


@app.route("/gestion/horaires", methods=["GET", "POST"])
@app.route("/legal/horaires", methods=["GET", "POST"])
@login_required
def horaires_config():
    """Horaires de travail — réécriture from scratch.

    POST accepte 3 formats de payload :
      a) JSON `{jours:[{jour, type, midi_deb, midi_fin, soir_deb, soir_fin}, ...]}`
      b) form-data avec champs `type_Lundi`, `midi_debut_Lundi`, `midi_fin_Lundi`,
         `soir_debut_Lundi`, `soir_fin_Lundi`, etc.
      c) form-data avec abréviations historiques `midi_deb_Lundi`, `soir_deb_Lundi`
    Pour chaque jour, upsert direct (matching par {Restaurant_ID, Jour}).
    GET retourne soit le rendu HTML dashboard (Accept HTML) soit JSON (Accept JSON).
    """
    rid = current_user.restaurant_id
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not data:
            data = {"jours": []}
            for jour_nom in _JOURS_SEMAINE:
                jour_dict = {"jour": jour_nom}
                # Accepte les deux conventions de nommage : midi_debut_Xxx et midi_deb_Xxx
                jour_dict["type"] = (request.form.get(f"type_{jour_nom}") or "").strip()
                jour_dict["midi_deb"] = (request.form.get(f"midi_debut_{jour_nom}")
                                         or request.form.get(f"midi_deb_{jour_nom}") or "")
                jour_dict["midi_fin"] = request.form.get(f"midi_fin_{jour_nom}", "")
                jour_dict["soir_deb"] = (request.form.get(f"soir_debut_{jour_nom}")
                                         or request.form.get(f"soir_deb_{jour_nom}") or "")
                jour_dict["soir_fin"] = request.form.get(f"soir_fin_{jour_nom}", "")
                # Inclus uniquement si au moins un champ a été soumis
                if any([jour_dict["type"], jour_dict["midi_deb"], jour_dict["midi_fin"],
                        jour_dict["soir_deb"], jour_dict["soir_fin"]]):
                    jour_dict["ouvert"] = jour_dict["type"] not in ("repos", "ferme", "")
                    data["jours"].append(jour_dict)
        result = _save_horaires_jours(rid, data)
        if not request.is_json and "application/json" not in (request.headers.get("Accept") or ""):
            return redirect(url_for("horaires_config"))
        return result
    h = _get_horaires(rid)
    return render_template("base.html", page="horaires_config", jours_config=h["jours"])


def _save_horaires_jours(rid, data):
    jours = data.get("jours", [])
    print(f"[HORAIRES SAVE] rid={rid} payload={data}")
    if not isinstance(jours, list) or not jours:
        print(f"[HORAIRES SAVE] payload invalide : jours={jours!r}")
        return jsonify({"ok": False, "error": "Données manquantes (jours vide ou mal formé)"}), 400
    errors = []
    saved = 0
    for j in jours:
        try:
            _upsert_horaire_jour(rid, j)
            saved += 1
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[HORAIRES SAVE] erreur jour {j.get('jour','?')}: {e}\n{tb}")
            errors.append(f"{j.get('jour','?')}: {e}")
    try:
        at.invalidate_cache("horaires_travail", rid)
    except Exception:
        pass
    if errors:
        return jsonify({"ok": False, "error": " | ".join(errors), "saved": saved}), 500
    return jsonify({"ok": True, "count": saved})


@app.route("/api/horaires", methods=["GET", "POST"])
@login_required
def api_horaires():
    rid = current_user.restaurant_id
    if request.method == "POST":
        return _save_horaires_jours(rid, request.get_json() or {})
    return jsonify({"ok": True, "jours": _get_horaires(rid)["jours"]})


@app.route("/gestion/gestes-utiles")
@login_required
def gestes_utiles():
    return render_template("base.html", page="gestes_utiles")


def _generate_horaires_pdf(resto_name, today, rid):
    """Generate working hours poster PDF with per-day schedule."""
    resto = at.get_restaurant(rid) or {} if rid else {}
    adresse = resto.get("Adresse", "") or ""
    ville = resto.get("Ville", "") or ""
    gp = resto.get("Gerant_prenom", "") or ""
    gn = resto.get("Gerant_nom", "") or ""
    gerant = f"{gp} {gn}".strip() or "Le gérant"
    h = _get_horaires(rid)
    jours = h["jours"]

    def _fmt(t):
        return t.replace(":", "h") if t else ""

    repos_jours = [j["jour"] for j in jours if not j.get("ouvert")]

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO

        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, pg_h = A4
        cx = w / 2

        # Header
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, pg_h - 90, w, 90, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 24)
        c.drawCentredString(cx, pg_h - 38, "HORAIRES COLLECTIFS DE TRAVAIL")
        c.setFont("Helvetica", 10)
        c.drawString(40, pg_h - 65, resto_name)
        if adresse:
            c.drawString(40, pg_h - 78, f"{adresse}, {ville}")
        c.drawRightString(w - 40, pg_h - 65, today)

        y = pg_h - 125

        # Table header
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(40, y - 2, w - 80, 26, fill=True, stroke=False)
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(50, y + 4, "Jour")
        c.drawString(160, y + 4, "Type")
        c.drawString(280, y + 4, "Horaires")
        y -= 30

        # Rows per day
        for idx, j in enumerate(jours):
            alt = idx % 2 == 1
            c.setFillColor(HexColor("#F9FAFB" if alt else "#FFFFFF"))
            c.rect(40, y - 4, w - 80, 28, fill=True, stroke=False)
            c.setStrokeColor(HexColor("#E5E7EB"))
            c.setLineWidth(0.5)
            c.line(40, y - 4, w - 40, y - 4)

            jour_nom = j.get("jour", "")
            c.setFillColor(HexColor("#0F1F14"))
            c.setFont("Helvetica-Bold", 12)
            c.drawString(50, y + 4, jour_nom)

            if not j.get("ouvert"):
                c.setFillColor(HexColor("#DC2626"))
                c.setFont("Helvetica-Bold", 11)
                c.drawString(160, y + 4, "REPOS")
            elif j.get("type") == "continu":
                c.setFillColor(HexColor("#059669"))
                c.setFont("Helvetica", 11)
                c.drawString(160, y + 4, "Continu")
                c.setFillColor(HexColor("#0F1F14"))
                c.setFont("Helvetica-Bold", 12)
                deb = _fmt(j.get("midi_deb", j.get("cont_deb", "")))
                fin = _fmt(j.get("soir_fin", j.get("cont_fin", "")))
                c.drawString(280, y + 4, f"{deb} — {fin}")
            elif j.get("type") == "24h":
                c.setFillColor(HexColor("#1D4ED8"))
                c.setFont("Helvetica-Bold", 11)
                c.drawString(160, y + 4, "24h/24")
            else:  # coupure
                c.setFillColor(HexColor("#374151"))
                c.setFont("Helvetica", 11)
                c.drawString(160, y + 4, "Coupure")
                c.setFillColor(HexColor("#0F1F14"))
                c.setFont("Helvetica-Bold", 12)
                md = _fmt(j.get("midi_deb", ""))
                mf = _fmt(j.get("midi_fin", ""))
                sd = _fmt(j.get("soir_deb", ""))
                sf = _fmt(j.get("soir_fin", ""))
                c.drawString(280, y + 4, f"{md}—{mf}  /  {sd}—{sf}")
            y -= 28

        # Repos box
        if repos_jours:
            y -= 15
            repos_txt = " · ".join(repos_jours)
            c.setFillColor(HexColor("#D1FAE5"))
            c.setStrokeColor(HexColor("#6EE7B7"))
            c.setLineWidth(1.5)
            c.roundRect(40, y - 8, w - 80, 40, 10, fill=True, stroke=True)
            c.setFillColor(HexColor("#065F46"))
            c.setFont("Helvetica-Bold", 13)
            c.drawCentredString(cx, y + 8, f"Jour(s) de repos : {repos_txt}")
            y -= 55

        # Legal
        c.setFillColor(HexColor("#6B7280"))
        c.setFont("Helvetica", 10)
        c.drawCentredString(cx, y, "Conformément à l'article L3171-1 du Code du travail")
        y -= 40
        c.setFillColor(HexColor("#374151"))
        c.setFont("Helvetica", 10)
        c.drawString(40, y, f"Responsable : {gerant}")
        y -= 25
        c.drawString(40, y, "Signature : ___________________________________")

        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, 25, f"Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"horaires_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        lines = [f"HORAIRES — {resto_name}"]
        for j in jours:
            if j.get("ouvert"):
                lines.append(f"{j['jour']}: {j.get('type','')} {j.get('midi_deb','')}-{j.get('soir_fin','')}")
            else:
                lines.append(f"{j['jour']}: REPOS")
        return Response("\n".join(lines), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=horaires.txt"})


@app.route("/gestion/conformite/modele/<doc_id>")
@login_required
def conformite_modele(doc_id):
    """Generate a pre-filled PDF template for the given document."""
    resto_name = current_user.restaurant_name or "Mon Restaurant"
    today = date.today().strftime("%d/%m/%Y")

    # Document content templates
    _MODELES = {
        "interdiction_fumer": {
            "title": "INTERDICTION DE FUMER",
            "content": "Conformément au décret n°2006-1386 du 15 novembre 2006,\nil est INTERDIT DE FUMER dans l'ensemble de cet établissement.\n\nContrevenant : amende forfaitaire de 68€\nResponsable des lieux : amende de 135€",
        },
        "numeros_urgence": {
            "title": "NUMÉROS D'URGENCE",
            "content": "SAMU : 15\nPOLICE / GENDARMERIE : 17\nPOMPIERS : 18\nNUMÉRO D'URGENCE EUROPÉEN : 112\nPRÉVENTION SUICIDE : 3114\nCENTRE ANTIPOISON : 01 40 05 48 48\nVIOLENCES FEMMES : 3919",
        },
        "origine_viandes": {
            "title": "ORIGINE DES VIANDES BOVINES",
            "content": "Conformément au décret n°2002-1465,\nnous vous informons de l'origine des viandes bovines\nservies dans notre établissement :\n\nNé en : France\nÉlevé en : France\nAbattu en : France\nDécoupé en : France",
        },
        "registre_personnel": {
            "title": "REGISTRE UNIQUE DU PERSONNEL",
            "content": "Article L1221-13 du Code du travail\n\nNom | Prénom | Nationalité | Date de naissance\nEmploi | Qualification | Date d'entrée | Date de sortie\nType de contrat | N° autorisation de travail (si étranger)",
        },
        "duer": {
            "title": "DOCUMENT UNIQUE D'ÉVALUATION DES RISQUES PROFESSIONNELS",
            "content": "Modèle simplifié CHR\n\nUnité de travail : Cuisine\nRisques identifiés : brûlures, coupures, chutes, port de charges\nMesures de prévention : EPI, formation, signalétique\n\nUnité de travail : Salle\nRisques identifiés : chutes, TMS, stress\nMesures de prévention : chaussures antidérapantes, pauses\n\nUnité de travail : Bar\nRisques identifiés : coupures, sols glissants\nMesures de prévention : tapis antidérapants, formation",
        },
        "horaires": {
            "title": "HORAIRES DE TRAVAIL",
            "content": "Horaires collectifs de travail\n\nService du midi : 11h00 — 15h00\nService du soir : 18h00 — 23h00\n\nJours de repos hebdomadaire : à compléter\n\nAffichage obligatoire — Article L3171-1 du Code du travail",
        },
        "temperatures": {
            "title": "RELEVÉ DE TEMPÉRATURES — SEMAINE DU ___/___/______",
            "content": "Contrôle quotidien obligatoire\n\nFrigo 1 (positif 0-4°C) :\nLun ___ Mar ___ Mer ___ Jeu ___ Ven ___ Sam ___ Dim ___\n\nFrigo 2 (positif 0-4°C) :\nLun ___ Mar ___ Mer ___ Jeu ___ Ven ___ Sam ___ Dim ___\n\nCongélateur (-18°C) :\nLun ___ Mar ___ Mer ___ Jeu ___ Ven ___ Sam ___ Dim ___\n\nSignature du responsable : _______________",
        },
        "pms_haccp": {
            "title": "PLAN DE MAÎTRISE SANITAIRE — RÉSUMÉ",
            "content": "1. RÉCEPTION DES MARCHANDISES\nContrôle températures, DLC, état emballages\n\n2. STOCKAGE\nRespect chaîne du froid, FIFO, séparation des produits\n\n3. PRÉPARATION\nLavage des mains, plan de nettoyage, marche en avant\n\n4. SERVICE\nTempératures de maintien, traçabilité\n\n5. NETTOYAGE\nPlan de nettoyage-désinfection, produits agréés\n\n6. GESTION DES DÉCHETS\nTri, stockage, enlèvement régulier",
        },
        "nuisibles": {
            "title": "REGISTRE DE LUTTE CONTRE LES NUISIBLES",
            "content": "Prestataire 3D : _______________\nN° contrat : _______________\nFréquence : trimestrielle\n\nDate intervention | Type | Observations | Visa\n___________|_______|____________|_____\n___________|_______|____________|_____\n___________|_______|____________|_____",
        },
    }

    # Special PDFs
    if doc_id == "interdiction_fumer":
        return _generate_interdiction_fumer_pdf(resto_name, today)
    if doc_id == "numeros_urgence":
        return _generate_numeros_urgence_pdf(resto_name, today)
    if doc_id == "registre_personnel":
        return _generate_registre_personnel_pdf(resto_name, today, current_user.restaurant_id)
    if doc_id == "pms_haccp":
        return _generate_pms_pdf(resto_name, today, current_user.restaurant_id)
    if doc_id == "duer":
        return _generate_duer_pdf(resto_name, today, current_user.restaurant_id)
    if doc_id == "horaires":
        return _generate_horaires_pdf(resto_name, today, current_user.restaurant_id)

    modele = _MODELES.get(doc_id)
    if not modele:
        return "Modèle non disponible", 404

    # Generate simple text-based PDF
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as pdf_canvas
        from io import BytesIO

        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, h = A4

        # Header
        c.setFillColorRGB(15/255, 31/255, 20/255)
        c.rect(0, h - 80, w, 80, fill=True, stroke=False)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(40, h - 35, modele["title"])
        c.setFont("Helvetica", 10)
        c.drawString(40, h - 55, resto_name)
        c.drawRightString(w - 40, h - 55, today)

        # Content
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 11)
        y = h - 110
        for line in modele["content"].split("\n"):
            if not line.strip():
                y -= 10
                continue
            if y < 60:
                c.showPage()
                y = h - 40
            c.drawString(40, y, line)
            y -= 16

        # Footer
        c.setFillColorRGB(0.6, 0.6, 0.6)
        c.setFont("Helvetica", 8)
        c.drawCentredString(w / 2, 30, f"Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)

        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"{doc_id}_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        # Fallback: return plain text if reportlab not installed
        from flask import Response
        text = f"{modele['title']}\n{resto_name} — {today}\n\n{modele['content']}\n\nGénéré par Pauco"
        return Response(text, mimetype="text/plain",
                        headers={"Content-Disposition": f"attachment; filename={doc_id}.txt"})


# ---------------------------------------------------------------------------
#  Origine des viandes
# ---------------------------------------------------------------------------

_PAYS_ORIGINE = ["France", "Irlande", "Écosse", "Espagne", "Allemagne", "Italie",
                 "Pologne", "Argentine", "Uruguay", "Brésil", "Autre"]


@app.route("/gestion/origine-viandes")
@login_required
def origine_viandes_page():
    rid = current_user.restaurant_id
    rows = at.get_all("origine_viandes", rid)
    viandes = [dict(r) for r in rows]
    return render_template("base.html", page="origine_viandes", viandes=viandes, pays=_PAYS_ORIGINE)


@app.route("/gestion/origine-viandes/save", methods=["POST"])
@login_required
def origine_viandes_save():
    data = request.get_json()
    action = data.get("action", "")
    rid = current_user.restaurant_id
    if action == "add":
        try:
            rec = at.create("origine_viandes", {
                "Restaurant_ID": rid,
                "Fournisseur": data.get("fournisseur", "").strip(),
                "Ne_en": data.get("ne_en", "France"),
                "Eleve_en": data.get("eleve_en", "France"),
                "Abattu_en": data.get("abattu_en", "France"),
                "Decoupe_en": data.get("decoupe_en", "France"),
            })
            at.invalidate_cache("origine_viandes")
            return jsonify({"ok": True, "id": rec.get("id", "")})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    elif action == "update":
        rec_id = data.get("id", "")
        fields = {}
        for k in ("Fournisseur", "Ne_en", "Eleve_en", "Abattu_en", "Decoupe_en"):
            if k.lower() in data:
                fields[k] = data[k.lower()]
            elif k in data:
                fields[k] = data[k]
        if fields and rec_id:
            try:
                at.update("origine_viandes", rec_id, fields)
                at.invalidate_cache("origine_viandes")
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})
    elif action == "delete":
        rec_id = data.get("id", "")
        if rec_id:
            try:
                at.delete("origine_viandes", rec_id)
                at.invalidate_cache("origine_viandes")
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/gestion/origine-viandes/pdf")
@login_required
def origine_viandes_pdf():
    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"
    today = date.today().strftime("%d/%m/%Y")
    rows = at.get_all("origine_viandes", rid)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO

        buf = BytesIO()
        c = pdf_canvas.Canvas(buf, pagesize=A4)
        w, h = A4
        cx = w / 2

        # Header
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h - 90, w, 90, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 22)
        c.drawCentredString(cx, h - 40, "ORIGINE DES VIANDES BOVINES")
        c.setFont("Helvetica", 11)
        c.drawString(40, h - 70, resto_name)
        c.drawRightString(w - 40, h - 70, today)

        # Table header
        y = h - 120
        cols = [40, 170, 280, 370, 460]
        headers = ["Produit", "Né en", "Élevé en", "Abattu en", "Découpé en"]
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(30, y - 5, w - 60, 24, fill=True, stroke=False)
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 10)
        for i, hdr in enumerate(headers):
            c.drawString(cols[i], y + 2, hdr)

        # Table rows
        c.setFont("Helvetica", 10)
        y -= 28
        for r in rows:
            if y < 80:
                c.showPage()
                y = h - 40
            vals = [
                r.get("Fournisseur", "") or "—",
                r.get("Ne_en", "") or "—",
                r.get("Eleve_en", "") or "—",
                r.get("Abattu_en", "") or "—",
                r.get("Decoupe_en", "") or "—",
            ]
            c.setFillColor(HexColor("#374151"))
            for i, v in enumerate(vals):
                c.drawString(cols[i], y, v)
            y -= 22

        if not rows:
            c.setFillColor(HexColor("#9CA3AF"))
            c.drawCentredString(cx, y, "Aucune origine renseignée")

        # Legal text
        c.setFillColor(HexColor("#6B7280"))
        c.setFont("Helvetica", 9)
        c.drawCentredString(cx, 60, "Conformément au règlement UE n°1337/2013 relatif à l'indication du pays d'origine")
        c.drawCentredString(cx, 48, "des viandes des espèces porcine, ovine, caprine et de volaille")

        # Footer
        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, 28, f"Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"origine_viandes_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        lines = ["ORIGINE DES VIANDES BOVINES", resto_name, ""]
        for r in rows:
            lines.append(f"{r.get('Fournisseur','—')} | {r.get('Ne_en','—')} | {r.get('Eleve_en','—')} | {r.get('Abattu_en','—')} | {r.get('Decoupe_en','—')}")
        lines.append("\nGénéré par Pauco")
        return Response("\n".join(lines), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=origine_viandes.txt"})


# ---------------------------------------------------------------------------
#  Relevé des températures
# ---------------------------------------------------------------------------

_TYPES_FROID = [
    ("Positif (0-4°C)", "0 à 4°C"),
    ("Négatif (-18°C)", "-18°C"),
    ("Surgélateur (-25°C)", "-25°C"),
]


@app.route("/gestion/temperatures")
@login_required
def temperatures_page():
    return redirect(url_for("hygiene_releves"))


@app.route("/gestion/temperatures/save", methods=["POST"])
@login_required
def temperatures_save():
    data = request.get_json()
    action = data.get("action", "")
    rid = current_user.restaurant_id
    if action == "add":
        nom = data.get("nom", "").strip()
        type_f = data.get("type", "Positif (0-4°C)")
        freq = data.get("frequence", "1x")
        if not nom:
            return jsonify({"ok": False, "error": "Nom requis"}), 400
        try:
            rec = at.create("equipements_froid", {
                "Restaurant_ID": rid, "Nom": nom, "Type": type_f,
                "Actif": True, "Frequence": freq,
            })
            at.invalidate_cache("equipements_froid")
            return jsonify({"ok": True, "id": rec.get("id", "")})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    elif action == "delete":
        rec_id = data.get("id", "")
        if rec_id:
            try:
                at.delete("equipements_froid", rec_id)
                at.invalidate_cache("equipements_froid")
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/gestion/temperatures/pdf")
@login_required
def temperatures_pdf():
    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"
    today = date.today().strftime("%d/%m/%Y")
    equips = at.get_all("equipements_froid", rid)
    actifs = [e for e in equips if e.get("Actif")]

    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.colors import HexColor
        from io import BytesIO

        buf = BytesIO()
        page = landscape(A4)
        c = pdf_canvas.Canvas(buf, pagesize=page)
        w, h = page
        cx = w / 2

        # Header
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h - 70, w, 70, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(cx, h - 32, "RELEVÉ DES TEMPÉRATURES — SEMAINE DU ___/___/______")
        c.setFont("Helvetica", 10)
        c.drawString(30, h - 55, resto_name)
        c.drawRightString(w - 30, h - 55, today)

        # Table
        jours = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        y = h - 100

        # Header row
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(25, y - 2, w - 50, 22, fill=True, stroke=False)
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 8)
        c.drawString(30, y + 4, "Équipement")
        c.drawString(160, y + 4, "Cible")
        col_start = 220
        col_w = (w - 50 - col_start - 70) / 7  # 7 days
        for i, j in enumerate(jours):
            c.drawCentredString(col_start + i * col_w + col_w / 2, y + 4, j)
        c.drawString(w - 95, y + 4, "Observations")
        y -= 24

        # Equipment rows
        c.setFont("Helvetica", 8)
        c.setStrokeColor(HexColor("#E5E7EB"))
        c.setLineWidth(0.5)
        for eq in actifs:
            nom = eq.get("Nom", "")
            type_f = eq.get("Type", "")
            freq = eq.get("Frequence", "1x")
            cible = ""
            for t, cib in _TYPES_FROID:
                if t == type_f:
                    cible = cib
                    break

            if freq == "2x":
                # Two rows: Matin + Soir
                for label in ["Matin", "Soir"]:
                    if y < 50:
                        break
                    c.setFillColor(HexColor("#FFFFFF" if label == "Matin" else "#F9FAFB"))
                    c.rect(25, y - 2, w - 50, 18, fill=True, stroke=False)
                    c.line(25, y - 2, w - 25, y - 2)
                    c.setFillColor(HexColor("#374151"))
                    c.drawString(30, y + 2, f"{nom} ({label})" if label == "Matin" else f"  ({label})")
                    c.drawString(160, y + 2, cible if label == "Matin" else "")
                    # Empty cells for temperatures
                    for i in range(7):
                        cx_cell = col_start + i * col_w
                        c.line(cx_cell, y - 2, cx_cell, y + 16)
                    y -= 18
            else:
                # One row
                if y < 50:
                    break
                c.setFillColor(HexColor("#FFFFFF"))
                c.rect(25, y - 2, w - 50, 18, fill=True, stroke=False)
                c.line(25, y - 2, w - 25, y - 2)
                c.setFillColor(HexColor("#374151"))
                c.drawString(30, y + 2, nom)
                c.drawString(160, y + 2, cible)
                for i in range(7):
                    cx_cell = col_start + i * col_w
                    c.line(cx_cell, y - 2, cx_cell, y + 16)
                y -= 18

        # Empty rows if no equipment
        if not actifs:
            for i in range(5):
                c.setFillColor(HexColor("#F9FAFB" if i % 2 else "#FFFFFF"))
                c.rect(25, y - 2, w - 50, 18, fill=True, stroke=False)
                c.line(25, y - 2, w - 25, y - 2)
                y -= 18

        # Signature
        y -= 20
        c.setFillColor(HexColor("#374151"))
        c.setFont("Helvetica", 9)
        c.drawString(30, y, "Responsable : ________________________     Signature : ________________________     Date : ___/___/______")

        # Footer
        c.setFillColor(HexColor("#6B7280"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(cx, 28, "Contrôle obligatoire — Plan de Maîtrise Sanitaire")
        c.setFillColor(HexColor("#9CA3AF"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(cx, 16, f"Généré par Pauco · paucoandco.com · {today}")

        c.save()
        buf.seek(0)
        from flask import send_file
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"releve_temperatures_{resto_name.replace(' ', '_')}.pdf",
                         as_attachment=True)
    except ImportError:
        from flask import Response
        lines = [f"RELEVÉ DES TEMPÉRATURES — {resto_name}", ""]
        for eq in actifs:
            lines.append(f"{eq.get('Nom','')} ({eq.get('Type','')}) : Lun ___ Mar ___ Mer ___ Jeu ___ Ven ___ Sam ___ Dim ___")
        lines.append("\nGénéré par Pauco")
        return Response("\n".join(lines), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=releve_temperatures.txt"})


@app.route("/gestion/temperatures/upload-doc", methods=["POST"])
@login_required
def temperatures_upload_doc():
    db = get_db()
    periode = request.form.get("periode", "").strip()
    type_doc = request.form.get("type_doc", "Releve hebdomadaire")
    fichier = request.files.get("fichier")
    if not periode or not fichier:
        return redirect(url_for("hygiene_releves"))
    rid = current_user.restaurant_id or "unknown"
    from werkzeug.utils import secure_filename
    fname = secure_filename(fichier.filename) or "document"
    safe_key = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{fname}"
    folder = f"temperatures/{rid}"
    r2_key = f"{folder}/{safe_key}"
    try:
        from modules.r2_client import upload_file, is_configured
        if not is_configured():
            raise RuntimeError("Stockage R2 non configuré")
        r2_key = upload_file(fichier, folder, safe_key)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[TEMP-DOC] R2 upload error: {e}\n{tb}")
        try:
            with open("errors.log", "a", encoding="utf-8") as _lf:
                _lf.write(f"[{datetime.now().isoformat()}] temperatures_upload_doc rid={rid}: {e}\n{tb}\n")
        except Exception:
            pass
        return redirect(url_for("hygiene_releves"))
    db.execute("INSERT INTO documents_temperatures (restaurant_id, periode, type_doc, filename, r2_key, uploaded_at) VALUES (?,?,?,?,?,?)",
               (rid, periode, type_doc, fname, r2_key, datetime.now().isoformat()))
    db.commit()
    return redirect(url_for("hygiene_releves"))


@app.route("/gestion/temperatures/delete-doc/<int:doc_id>")
@login_required
def temperatures_delete_doc(doc_id):
    db = get_db()
    row = db.execute("SELECT r2_key FROM documents_temperatures WHERE restaurant_id=? AND id=?", (_rid(), doc_id,)).fetchone()
    if row:
        try:
            from modules.r2_client import delete_file, is_configured
            if is_configured():
                delete_file(row["r2_key"])
        except Exception:
            pass
        db.execute("DELETE FROM documents_temperatures WHERE id=?", (doc_id,))
        db.commit()
    return redirect(url_for("temperatures_page"))


@app.route("/marketing")
def marketing():
    return redirect(url_for("marketing_publicite"))


@app.route("/gestion/marketing/publicite")
def marketing_publicite():
    return render_template("base.html", page="publicite")


@app.route("/gestion/marketing/communication")
def marketing_communication():
    return render_template("base.html", page="communication")


@app.route("/gestion/marketing/performances-ads")
def marketing_perf_ads():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    mois = request.args.get("mois", _mois_courant())
    stats = None
    campagnes = []
    mois_list = []
    if rid:
        try:
            all_stats = at.get_all("stats_pub", rid, sort=["-Mois"])
            mois_list = [{"value": s.get("Mois",""), "label": _mois_label(s.get("Mois",""))} for s in all_stats if s.get("Mois")]
            for s in all_stats:
                if s.get("Mois") == mois:
                    stats = dict(s)
                    import json as _json
                    try:
                        campagnes = _json.loads(s.get("Campagnes") or "[]")
                    except Exception:
                        campagnes = []
                    break
        except Exception as e:
            print(f"[PERF_ADS] Airtable error: {e}")
    return render_template("base.html", page="perf_ads", stats=stats, campagnes=campagnes,
                           mois=mois, mois_list_ads=mois_list)


@app.route("/gestion/marketing/rapports-ads")
def marketing_rapports_ads():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    rapports = []
    if rid:
        try:
            recs = at.get_all("stats_pub", rid, sort=["-Mois"])
            rapports = [dict(r) for r in recs]
        except Exception as e:
            print(f"[RAPPORTS_ADS] Airtable error: {e}")
    return render_template("base.html", page="rapports_ads", rapports=rapports)


@app.route("/gestion/marketing/rapports-ads/pdf/<path:record_id>")
@login_required
def marketing_rapports_ads_pdf(record_id):
    """Génère à la volée le PDF d'un rapport de campagne."""
    rid = current_user.restaurant_id
    try:
        rpt = at.get_one("stats_pub", record_id) or {}
    except Exception as e:
        print(f"[RAPPORTS_ADS_PDF] read error: {e}")
        return "Rapport introuvable", 404
    if not rpt or (rpt.get("Restaurant_ID") or "") != rid:
        return "Rapport introuvable", 404

    import io as _io
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
    except Exception as e:
        return f"reportlab indisponible : {e}", 500

    VERT = colors.HexColor("#152B1E")
    GRIS_CLAIR = colors.HexColor("#F6F7F4")
    GRIS_TRAIT = colors.HexColor("#E5E7E1")
    GRIS_FOOTER = colors.HexColor("#9CA3AF")

    resto_name = current_user.restaurant_name or "Mon Restaurant"
    mois_label = rpt.get("Mois") or ""
    plateforme = rpt.get("Plateforme") or "Toutes plateformes"
    budget = float(rpt.get("Budget") or 0)
    budget_alloue = float(rpt.get("Budget_alloue") or 0)
    impressions = int(rpt.get("Impressions") or 0)
    clics = int(rpt.get("Clics") or 0)
    leads = int(rpt.get("Leads") or 0)
    ctr = round((clics / impressions * 100), 2) if impressions else 0
    cout_par_couvert = round(budget / leads, 2) if leads else 0
    statut = rpt.get("Statut") or ""

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm,
                            title=f"Rapport campagne {mois_label} — {resto_name}")
    styles = getSampleStyleSheet()
    logo_style = ParagraphStyle("logo", parent=styles["Normal"], fontName="Helvetica-Bold",
                                fontSize=28, textColor=VERT, leading=30, alignment=TA_LEFT)
    meta_style = ParagraphStyle("meta", parent=styles["Normal"], fontName="Helvetica",
                                fontSize=10, textColor=VERT, leading=13, alignment=TA_RIGHT)
    section_style = ParagraphStyle("sec", parent=styles["Normal"], fontName="Helvetica-Bold",
                                   fontSize=12, textColor=VERT, spaceBefore=12, spaceAfter=8)
    footer_style = ParagraphStyle("ftr", parent=styles["Normal"], fontName="Helvetica",
                                  fontSize=8, textColor=GRIS_FOOTER, alignment=TA_CENTER)

    elements = []
    # Header
    meta_html = f"<b>{resto_name}</b><br/>Rapport de campagne — {mois_label}"
    header_tbl = Table(
        [[Paragraph("PAUCO", logo_style), Paragraph(meta_html, meta_style)]],
        colWidths=[8*cm, 9*cm]
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 1.5, VERT),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 14))

    # Tableau récapitulatif
    elements.append(Paragraph("Synthèse de la campagne", section_style))
    recap_data = [
        ["Indicateur", "Valeur"],
        ["Plateforme", plateforme],
        ["Statut", statut],
        ["Budget dépensé", f"{budget:,.0f} €".replace(",", " ")],
        ["Budget alloué", f"{budget_alloue:,.0f} €".replace(",", " ")],
        ["Impressions", f"{impressions:,}".replace(",", " ")],
        ["Clics", f"{clics:,}".replace(",", " ")],
        ["Taux de clic (CTR)", f"{ctr} %"],
        ["Couverts générés", str(leads)],
        ["Coût par couvert", f"{cout_par_couvert:.2f} €"],
    ]
    t = Table(recap_data, colWidths=[8*cm, 9*cm])
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), VERT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 1), (1, -1), "Helvetica"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, GRIS_TRAIT),
    ]
    for i in range(1, len(recap_data)):
        if i % 2 == 0:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), GRIS_CLAIR))
    t.setStyle(TableStyle(style_cmds))
    elements.append(t)
    elements.append(Spacer(1, 18))

    # Footer
    elements.append(Paragraph(
        f"Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')} via Pauco — paucoandco.com",
        footer_style
    ))

    try:
        doc.build(elements)
    except Exception as e:
        import traceback
        print(f"[RAPPORTS_ADS_PDF] build error: {e}\n{traceback.format_exc()}")
        return f"Erreur génération PDF : {e}", 500

    buf.seek(0)
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f"attachment; filename=rapport-campagne-{mois_label or 'campagne'}.pdf"
    return resp


@app.route("/gestion/marketing/stats-reseaux")
def marketing_stats_reseaux():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    mois = request.args.get("mois", _mois_courant())
    stats = None
    top_posts = []
    mois_list = []
    if rid:
        try:
            all_stats = at.get_all("stats_reseaux", rid, sort=["-Mois"])
            mois_list = [{"value": s.get("Mois",""), "label": _mois_label(s.get("Mois",""))} for s in all_stats if s.get("Mois")]
            for s in all_stats:
                if s.get("Mois") == mois:
                    stats = dict(s)
                    import json as _json
                    try:
                        top_posts = _json.loads(s.get("Top_Posts") or "[]")
                    except Exception:
                        top_posts = []
                    break
        except Exception as e:
            print(f"[STATS_RESEAUX] Airtable error: {e}")
    return render_template("base.html", page="stats_reseaux", stats_rs=stats, top_posts=top_posts,
                           mois=mois, mois_list_rs=mois_list)


@app.route("/gestion/marketing/calendrier-editorial")
def marketing_calendrier_editorial():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    mois = request.args.get("mois", _mois_courant())
    posts = []
    mois_list = []
    if rid:
        try:
            all_stats = at.get_all("stats_reseaux", rid, sort=["-Mois"])
            mois_list = [{"value": s.get("Mois",""), "label": _mois_label(s.get("Mois",""))} for s in all_stats if s.get("Mois")]
            for s in all_stats:
                if s.get("Mois") == mois:
                    import json as _json
                    try:
                        posts = _json.loads(s.get("Calendrier_Editorial") or "[]")
                    except Exception:
                        posts = []
                    break
        except Exception as e:
            print(f"[CAL_EDITORIAL] Airtable error: {e}")
    return render_template("base.html", page="calendrier_editorial", cal_posts=posts,
                           mois=mois, mois_list_cal=mois_list)


@app.route("/gestion/marketing/shooting")
def marketing_shooting():
    return render_template("base.html", page="shooting")


@app.route("/gestion/marketing/shooting/medias")
def shooting_medias():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    medias = []
    if rid:
        try:
            medias = [dict(r) for r in at.get_all("shooting_medias", rid, sort=["-Date_Upload"])]
        except Exception as e:
            print(f"[SHOOTING] Airtable error: {e}")
    return render_template("base.html", page="shooting_medias", medias=medias)


@app.route("/recrutement")
def recrutement():
    return render_template("base.html", page="recrutement")


@app.route("/recrutement/candidatures")
def recrutement_candidatures():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    candidatures = []
    if rid:
        try:
            candidatures = [dict(r) for r in at.get_all("recrutement_candidatures", rid, sort=["-Date_Candidature"])]
        except Exception as e:
            print(f"[RECRUTEMENT] Candidatures error: {e}")
    return render_template("base.html", page="candidatures", candidatures=candidatures)


@app.route("/recrutement/vivier")
def recrutement_vivier():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    vivier = []
    if rid:
        try:
            vivier = [dict(r) for r in at.get_all("recrutement_vivier", rid, sort=["Nom"])]
        except Exception as e:
            print(f"[RECRUTEMENT] Vivier error: {e}")
    return render_template("base.html", page="vivier", vivier=vivier)


@app.route("/recrutement/missions-terminees")
def recrutement_missions():
    rid = current_user.restaurant_id if current_user.is_authenticated else ""
    missions = []
    if rid:
        try:
            missions = [dict(r) for r in at.get_all("recrutement_missions", rid, sort=["-Date_Embauche"])]
        except Exception as e:
            print(f"[RECRUTEMENT] Missions error: {e}")
    return render_template("base.html", page="missions_terminees", missions=missions)


@app.route("/recrutement/nouvelle-recherche", methods=["POST"])
@login_required
def recrutement_nouvelle_recherche():
    data = request.get_json()
    profil = data.get("profil", "")
    restaurant = data.get("restaurant", "")
    try:
        import os
        brevo_key = os.environ.get("BREVO_API_KEY", "")
        if brevo_key:
            import requests as _req
            body = (f"Restaurant: {restaurant}\n"
                    f"Profil recherché: {profil}\n\n"
                    f"Envoyé depuis l'app Pauco — nouvelle recherche recrutement.")
            _req.post("https://api.brevo.com/v3/smtp/email", headers={
                "api-key": brevo_key, "Content-Type": "application/json"
            }, json={
                "sender": {"name": "Pauco App", "email": "contact@paucoandco.com"},
                "to": [{"email": "paul@paucoandco.com", "name": "Paul"}],
                "subject": f"Recrutement — {profil} — {restaurant}",
                "textContent": body,
            }, timeout=10)
    except Exception as e:
        print(f"[RECRUTEMENT] Email error: {e}")
    return jsonify({"ok": True})


def _is_ferme(db, ds):
    """Verifie si une date est un jour de fermeture.
    Exceptions (jour_semaine=-2) overrident une récurrence pour cette date."""
    # Exception explicite pour cette date → ouvert malgré récurrence
    exc = db.execute("SELECT id FROM fermetures WHERE restaurant_id=? AND date=? AND jour_semaine=-2", (_rid(), ds,)).fetchone()
    if exc:
        return False
    # Fermeture simple (ponctuelle)
    row = db.execute("SELECT id FROM fermetures WHERE restaurant_id=? AND date=? AND recurrence=0 AND jour_semaine!=-2", (_rid(), ds,)).fetchone()
    if row:
        return True
    # Fermeture recurrente par jour de semaine
    try:
        from datetime import date as _d
        d = _d.fromisoformat(ds)
        dow = d.weekday()  # 0=lundi
        row2 = db.execute("SELECT id FROM fermetures WHERE restaurant_id=? AND recurrence=1 AND jour_semaine=?", (_rid(), dow,)).fetchone()
        if row2:
            return True
    except Exception:
        pass
    return False


def _get_semaine_ca(db):
    """CA des 7 derniers jours pour le graphique."""
    result = []
    JOURS_COURTS = ["Lun","Mar","Mer","Jeu","Ven","Sam","Dim"]
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        ds = d.isoformat()
        row = db.execute("SELECT ca FROM ca_jour WHERE restaurant_id=? AND date=?", (_rid(), ds,)).fetchone()
        result.append({"label": JOURS_COURTS[d.weekday()], "ca": round(row["ca"], 0) if row else 0})
    return result


# ---------------------------------------------------------------------------
#  YouSign contract generation
# ---------------------------------------------------------------------------

def _create_yousign_contract(user_airtable_id, user_rec):
    """Create a YouSign signature request from template. Non-blocking."""
    yousign_key = os.environ.get("YOUSIGN_API_KEY", "")
    if not yousign_key:
        return
    email = user_rec.get("Email", "")
    prenom = user_rec.get("Prenom", "") or ""
    nom = user_rec.get("Nom", "") or ""
    if not email:
        return
    try:
        import requests as _rq
        base = "https://api-sandbox.yousign.app"
        headers = {"Authorization": f"Bearer {yousign_key}", "Content-Type": "application/json"}
        resp = _rq.post(f"{base}/v3/signature_requests/from_template", timeout=15,
            headers=headers,
            json={
                "template_id": "9656ed76-e4c2-4827-a3c2-7dbc8d048fff",
                "signers": [{
                    "info": {"first_name": prenom or "Client", "last_name": nom or "Pauco", "email": email},
                    "signature_level": "electronic_signature",
                    "delivery_mode": "none",
                }],
                "external_id": user_airtable_id,
            })
        if resp.status_code in (200, 201):
            data = resp.json()
            # Get the signing link from the signer
            signers = data.get("signers", [])
            if signers:
                signer_id = signers[0].get("id", "")
                sr_id = data.get("id", "")
                # Activate the signature request
                _rq.post(f"{base}/v3/signature_requests/{sr_id}/activate", headers=headers, timeout=10)
                # Get signing link
                link_resp = _rq.get(f"{base}/v3/signature_requests/{sr_id}/signers/{signer_id}", headers=headers, timeout=10)
                if link_resp.status_code == 200:
                    signing_url = link_resp.json().get("signature_link", "")
                    if signing_url:
                        at.update("utilisateurs", user_airtable_id, {"yousign_url": signing_url})
                        print(f"[YOUSIGN] Contract created for {email}: {signing_url[:60]}...")
                        return
            print(f"[YOUSIGN] Created but no signing link for {email}")
        else:
            print(f"[YOUSIGN] API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[YOUSIGN] Error: {e}")


# ---------------------------------------------------------------------------
#  Demarrage (onboarding checklist)
# ---------------------------------------------------------------------------

_ONBOARDING_STEPS = [
    {"key": "contrat", "label": "Signer votre contrat fondateur", "type": "link"},
    {"key": "gmb", "label": "Donner l'acc\u00e8s Google My Business", "type": "link", "url": "https://business.google.com",
     "sub": "Ajoutez paul@paucoandco.com comme gestionnaire \u2014 indispensable pour que Pauco r\u00e9ponde automatiquement \u00e0 vos avis Google"},
    {"key": "infos", "label": "Renseigner les infos de votre restaurant", "type": "link", "url": "/gestion/reglages/restaurant",
     "sub": "Nom, ville, adresse, t\u00e9l\u00e9phone"},
    {"key": "equipe", "label": "Ajouter votre \u00e9quipe", "type": "link", "url": "/rh/effectifs",
     "sub": "Cr\u00e9ez les profils de vos employ\u00e9s"},
    {"key": "depenses", "label": "Ajouter vos d\u00e9penses r\u00e9currentes", "type": "link", "url": "/depenses",
     "sub": "Loyer, assurances, abonnements..."},
    {"key": "roles", "label": "Configurer les postes et p\u00f4les", "type": "link", "url": "/rh/postes",
     "sub": "Salle, cuisine, bar"},
    {"key": "fiches", "label": "Cr\u00e9er vos fiches techniques", "type": "link", "url": "/gestion/fiches-techniques",
     "sub": "Recettes, co\u00fbts mati\u00e8re, marges"},
    {"key": "fournisseurs", "label": "Cr\u00e9er vos fournisseurs", "type": "link", "url": "/gestion/fournisseurs",
     "sub": "Noms, contacts, cat\u00e9gories"},
    {"key": "ca", "label": "Saisir votre premier CA", "type": "link", "url": "/recettes",
     "sub": "CA restaurant, bar, couverts"},
]


@app.route("/gestion/demarrage")
@login_required
def demarrage():
    user_rec = {}
    try:
        user_rec = at.get_one("utilisateurs", current_user.id) or {}
    except Exception:
        pass
    steps_json = user_rec.get("Onboarding_Steps", "") or "{}"
    import json
    try:
        completed = json.loads(steps_json)
    except Exception:
        completed = {}
    yousign_url = user_rec.get("yousign_url", "") or ""
    steps = []
    first_todo = None
    for s in _ONBOARDING_STEPS:
        step = dict(s)
        step["done"] = completed.get(s["key"], False)
        if s["key"] == "contrat":
            step["url"] = yousign_url if yousign_url else ""
            step["no_link"] = not yousign_url
        if not step["done"] and first_todo is None:
            first_todo = s["key"]
        step["active"] = (s["key"] == first_todo)
        steps.append(step)
    done_count = sum(1 for s in steps if s["done"])
    return render_template("base.html", page="demarrage",
                           steps=steps, done_count=done_count, total_steps=len(steps))


@app.route("/gestion/demarrage/toggle", methods=["POST"])
@login_required
def demarrage_toggle():
    import json
    data = request.get_json()
    key = data.get("key", "")
    done = data.get("done", False)
    user_rec = {}
    try:
        user_rec = at.get_one("utilisateurs", current_user.id) or {}
    except Exception:
        pass
    steps_json = user_rec.get("Onboarding_Steps", "") or "{}"
    try:
        completed = json.loads(steps_json)
    except Exception:
        completed = {}
    completed[key] = bool(done)
    try:
        at.update("utilisateurs", current_user.id, {"Onboarding_Steps": json.dumps(completed)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    done_count = sum(1 for v in completed.values() if v)
    return jsonify({"ok": True, "done_count": done_count, "all_done": done_count >= len(_ONBOARDING_STEPS)})


# ---------------------------------------------------------------------------
#  Poste creation inline (AJAX)
# ---------------------------------------------------------------------------

@app.route("/rh/postes/create-inline", methods=["POST"])
@login_required
def postes_create_inline():
    db = get_db()
    data = request.get_json()
    nom = data.get("nom", "").strip()
    pole = data.get("pole", "Salle")
    if not nom:
        return jsonify({"ok": False, "error": "Nom requis"}), 400
    if pole not in ("Salle", "Cuisine", "Bar"):
        pole = "Salle"
    sync.write_poste(db, nom, pole)
    return jsonify({"ok": True, "nom": nom, "pole": pole})


@app.route("/api/fournisseurs/create-inline", methods=["POST"])
@login_required
def fournisseurs_create_inline():
    db = get_db()
    data = request.get_json()
    nom = data.get("nom", "").strip()
    ftype = data.get("type", "Autre")
    if not nom:
        return jsonify({"ok": False, "error": "Nom requis"}), 400
    if ftype not in ("Food", "Boisson", "Autre"):
        ftype = "Autre"
    sync.write_fournisseur(db, nom, ftype)
    return jsonify({"ok": True, "nom": nom, "type": ftype})


def _jours_feries(year):
    """Return dict {iso_date: label} for French public holidays in a given year."""
    from datetime import timedelta
    # Easter (anonymous Gregorian algorithm)
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    feries = {
        date(year, 1, 1).isoformat(): "Jour de l'an",
        (easter + timedelta(days=1)).isoformat(): "Lundi de P\u00e2ques",
        date(year, 5, 1).isoformat(): "F\u00eate du Travail",
        date(year, 5, 8).isoformat(): "Victoire 1945",
        (easter + timedelta(days=39)).isoformat(): "Ascension",
        (easter + timedelta(days=50)).isoformat(): "Lundi de Pentec\u00f4te",
        date(year, 7, 14).isoformat(): "F\u00eate nationale",
        date(year, 8, 15).isoformat(): "Assomption",
        date(year, 11, 1).isoformat(): "Toussaint",
        date(year, 11, 11).isoformat(): "Armistice",
        date(year, 12, 25).isoformat(): "No\u00ebl",
    }
    return feries


@app.route("/")
def accueil():
    db = get_db()
    mois = _mois_courant()
    stats = _stats_mois(db, mois)

    # Meilleur jour du mois
    best = db.execute("SELECT date, ca FROM ca_jour WHERE restaurant_id=? AND date LIKE ? ORDER BY ca DESC LIMIT 1", (_rid(), mois + "%",)).fetchone()
    if best:
        bd = best["date"]  # "2026-03-27"
        best_day = {"date": f"{int(bd[8:10])} {MOIS_NOMS[int(bd[5:7])-1].lower()}", "ca": best["ca"]}
    else:
        best_day = None

    resume = {
        "ca_total": stats["ca_total"],
        "nb_jours": stats["nb_jours"],
        "ticket_moyen": stats["ticket_moyen"],
        "best_day": best_day,
    }

    # CA saisi aujourd'hui ?
    today_str = date.today().isoformat()
    today_row = db.execute("SELECT ca FROM ca_jour WHERE restaurant_id=? AND date = ?", (_rid(), today_str,)).fetchone()
    ca_saisi = today_row is not None
    ca_montant = today_row["ca"] if today_row else 0

    # Planning cette semaine : employés avec/sans créneaux
    from datetime import timedelta
    today = date.today()
    lundi = today - timedelta(days=today.weekday())
    dimanche = lundi + timedelta(days=6)
    nb_creneaux = db.execute("SELECT COUNT(*) as c FROM planning WHERE restaurant_id=? AND date >= ? AND date <= ? AND heure_debut NOT IN ('R','CP')",
                              (_rid(), lundi.isoformat(), dimanche.isoformat())).fetchone()["c"]
    nb_emp_actifs = db.execute("SELECT COUNT(*) as c FROM employes WHERE restaurant_id=? AND statut='Actif'", (_rid(),)).fetchone()["c"]
    emp_avec_planning = db.execute("""SELECT COUNT(DISTINCT employe_id) as c FROM planning
                                      WHERE restaurant_id=? AND date >= ? AND date <= ? AND heure_debut NOT IN ('R','CP')""",
                                   (_rid(), lundi.isoformat(), dimanche.isoformat())).fetchone()["c"]
    emp_sans_planning = nb_emp_actifs - emp_avec_planning

    # Date label
    JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    date_label = f"{JOURS[today.weekday()]} {today.day} {MOIS_NOMS[today.month-1].lower()} {today.year}"

    # Citation aleatoire
    import random
    citations = [
        "La cuisine, c'est l'art de transformer instantanement en joie des produits charges d'histoire. — Guy Savoy",
        "Un restaurant est un lieu de bonheur. La cuisine doit rendre les gens heureux. — Alain Ducasse",
        "Le secret d'un bon restaurant n'est pas dans les recettes, mais dans la constance. — Joel Robuchon",
        "La premiere condition pour bien manger, c'est de savoir ce que l'on mange. — Auguste Escoffier",
    ]
    citation = random.choice(citations)

    # Onboarding steps
    has_postes = db.execute("SELECT COUNT(*) as c FROM postes WHERE restaurant_id=?", (_rid(),)).fetchone()["c"] > 0
    has_employes = db.execute("SELECT COUNT(*) as c FROM employes WHERE restaurant_id=?", (_rid(),)).fetchone()["c"] > 0
    has_depenses = (db.execute("SELECT COUNT(*) as c FROM depenses_fixes WHERE restaurant_id=?", (_rid(),)).fetchone()["c"] + db.execute("SELECT COUNT(*) as c FROM depenses_variables WHERE restaurant_id=?", (_rid(),)).fetchone()["c"]) > 0
    has_fiches = db.execute("SELECT COUNT(*) as c FROM fiches_techniques WHERE restaurant_id=?", (_rid(),)).fetchone()["c"] > 0
    all_steps = [has_postes, has_employes, has_depenses, has_fiches]
    onboarding_done = all(all_steps)
    onboarding = {
        "done": onboarding_done,
        "steps": [
            {"label": "Ajoutez vos postes de travail", "url": "/rh/postes", "ok": has_postes},
            {"label": "Ajoutez vos employés", "url": "/rh/effectifs", "ok": has_employes},
            {"label": "Renseignez vos charges fixes (loyer, abonnements...)", "url": "/depenses", "ok": has_depenses},
            {"label": "Créez votre première recette", "url": "/gestion/fiches-techniques", "ok": has_fiches},
        ],
        "pct": round(sum(all_steps) / len(all_steps) * 100),
    }

    # Alertes ratios
    alertes = _compute_alertes(db, mois)

    # Evenements des 7 prochains jours
    today_iso = date.today().isoformat()
    dans_7j = (date.today() + timedelta(days=7)).isoformat()
    evenements_raw = db.execute("SELECT * FROM evenements WHERE restaurant_id=? AND date >= ? AND date <= ? ORDER BY date",
                                (_rid(), today_iso, dans_7j)).fetchall()
    evenements = [dict(e) for e in evenements_raw]

    # Anniversaires des 7 prochains jours
    JOURS_NOMS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    all_emps = db.execute("SELECT prenom, nom, date_naissance FROM employes WHERE restaurant_id=? AND statut='Actif'", (_rid(),)).fetchall()
    for emp in all_emps:
        dn = emp["date_naissance"] if "date_naissance" in emp.keys() else ""
        if not dn or len(dn) < 10:
            continue
        try:
            b_month, b_day = int(dn[5:7]), int(dn[8:10])
            b_year = int(dn[:4])
        except (ValueError, IndexError):
            continue
        # Check if birthday falls in the next 7 days
        for offset in range(8):
            d = date.today() + timedelta(days=offset)
            if d.month == b_month and d.day == b_day:
                age = d.year - b_year
                jour_nom = JOURS_NOMS[d.weekday()]
                evenements.append({
                    "id": 0,
                    "titre": f"\U0001f382 Anniversaire de {emp['prenom']} {emp['nom']} \u2014 {jour_nom} {d.day} {MOIS_NOMS[d.month-1].lower()} ({age} ans)",
                    "date": d.isoformat(),
                    "couleur": "#EC4899",
                })
                break
            # Feb 29 birthday: show on March 1 in non-leap years
            if b_month == 2 and b_day == 29 and d.month == 3 and d.day == 1:
                import calendar
                if not calendar.isleap(d.year):
                    age = d.year - b_year
                    jour_nom = JOURS_NOMS[d.weekday()]
                    evenements.append({
                        "id": 0,
                        "titre": f"\U0001f382 Anniversaire de {emp['prenom']} {emp['nom']} \u2014 {jour_nom} 1er mars ({age} ans)",
                        "date": d.isoformat(),
                        "couleur": "#EC4899",
                    })
                    break
    # Jours fériés des 7 prochains jours
    feries = _jours_feries(today.year)
    if today.month == 12:
        feries.update(_jours_feries(today.year + 1))
    for d_iso, label in feries.items():
        if today_iso <= d_iso <= dans_7j:
            evenements.append({"id": 0, "titre": label, "date": d_iso, "couleur": "#DC2626"})

    evenements.sort(key=lambda x: x.get("date", ""))

    # Meteo loaded async via /api/meteo (non-blocking)
    meteo = None
    meteo_aujourdhui = None

    # Recap hier
    hier = (date.today() - timedelta(days=1)).isoformat()
    hier_row = db.execute("SELECT * FROM ca_jour WHERE restaurant_id=? AND date = ?", (_rid(), hier,)).fetchone()
    recap_hier = None
    if hier_row:
        recap_hier = {
            "ca_restaurant": hier_row["ca_restaurant"] or 0,
            "ca_bar": hier_row["ca_bar"] or 0,
            "ca": hier_row["ca"] or 0,
            "couverts": (hier_row["couverts_midi"] or 0) + (hier_row["couverts_soir"] or 0),
            "ticket_moyen": hier_row["ticket_moyen"] or 0,
            "commentaire": hier_row["commentaire"] or "",
        }

    # Messages non lus
    nb_messages_non_lus = db.execute("SELECT COUNT(*) as c FROM messages WHERE restaurant_id=? AND lu = 0", (_rid(),)).fetchone()["c"]

    return render_template("base.html", page="accueil",
        resume=resume, ca_saisi_aujourd=ca_saisi, ca_aujourd_montant=ca_montant,
        nb_creneaux_semaine=nb_creneaux, emp_sans_planning=emp_sans_planning,
        nb_emp_actifs=nb_emp_actifs, date_label=date_label, citation=citation,
        onboarding=onboarding, alertes=alertes, evenements=evenements, meteo=meteo,
        meteo_aujourdhui=meteo_aujourdhui, recap_hier=recap_hier,
        demain_ferme=_is_ferme(db, (date.today() + timedelta(days=1)).isoformat()),
        nb_messages_non_lus=nb_messages_non_lus,
        semaine_ca=_get_semaine_ca(db))


@app.route("/gestion/tableau-de-bord")
def dashboard():
    mois = request.args.get("mois", _mois_courant())
    db = get_db()
    stats = _stats_mois(db, mois)

    # Depenses
    dep = _depenses_mois(db, mois)
    total_fixes = dep["total_fixes"]
    total_variables = dep["total_variables"]
    total_depenses = dep["total"]

    # Ratios
    ca = stats["ca_total"]
    food_cost = round(dep["food"] / ca * 100, 1) if ca > 0 else 0
    beverage_cost = round(dep["beverage"] / ca * 100, 1) if ca > 0 else 0
    labour_pct = round(dep["personnel"] / ca * 100, 1) if ca > 0 else 0
    prime_cost = round((dep["food"] + dep["beverage"] + dep["personnel"]) / ca * 100, 1) if ca > 0 else 0
    resultat_net = round((ca - total_depenses) / ca * 100, 1) if ca > 0 else 0

    # Seuil de rentabilite
    seuil = _seuil_rentabilite(db, mois)

    # Meilleur mois historique
    historique = _meilleur_mois_historique(db, mois)

    # 12 derniers mois pour graphiques
    mois_list = _get_mois_list()
    chart_labels = []
    chart_ca = []
    chart_tm = []
    for m in reversed(mois_list):
        s = _stats_mois(db, m["value"])
        chart_labels.append(m["label"][:3])
        chart_ca.append(s["ca_total"])
        chart_tm.append(s["ticket_moyen"])

    # Alertes ratios
    alertes = _compute_alertes(db, mois)

    return render_template("base.html", page="dashboard",
        mois=mois, mois_label=_mois_label(mois), mois_list=mois_list,
        stats=stats, total_depenses=round(total_depenses, 2),
        total_fixes=total_fixes, total_variables=total_variables,
        food_cost=food_cost, beverage_cost=beverage_cost, labour_pct=labour_pct,
        prime_cost=prime_cost, resultat_net=resultat_net,
        seuil=seuil, historique=historique,
        chart_labels=chart_labels, chart_ca=chart_ca, chart_tm=chart_tm,
        alertes=alertes,
        jours_restants_mois=__import__('calendar').monthrange(date.today().year, date.today().month)[1] - date.today().day)


@app.route("/recettes")
def recettes():
    mois = request.args.get("mois", _mois_courant())
    db = get_db()
    import calendar
    try:
        y, m = int(mois.split("-")[0]), int(mois.split("-")[1])
    except (ValueError, IndexError):
        y, m = date.today().year, date.today().month
    nb_days = calendar.monthrange(y, m)[1]

    # All entries for the month
    rows = db.execute("SELECT * FROM ca_jour WHERE restaurant_id=? AND date LIKE ? ORDER BY date", (_rid(), mois + "%",)).fetchall()
    entries_map = {r["date"]: dict(r) for r in rows}

    # Build list of days with data only
    jours = []
    for r in rows:
        jours.append({
            "date": r["date"],
            "ca_restaurant": r["ca_restaurant"] or 0,
            "ca_bar": r["ca_bar"] or 0,
            "ca": r["ca"] or 0,
            "couverts": (r["couverts_midi"] or 0) + (r["couverts_soir"] or 0),
            "commentaire": r["commentaire"] or "",
        })

    # Stats
    stats = _stats_mois(db, mois)
    ca_mois = stats["ca_total"]
    nb_jours_saisis = stats["nb_jours"]
    ca_moyen = round(ca_mois / nb_jours_saisis, 2) if nb_jours_saisis > 0 else 0

    # Previous month for comparison
    pm = m - 1
    py = y
    if pm <= 0:
        pm = 12
        py -= 1
    mois_prec = f"{py:04d}-{pm:02d}"
    stats_prec = _stats_mois(db, mois_prec)
    ca_prec = stats_prec["ca_total"]
    evo_pct = round((ca_mois - ca_prec) / ca_prec * 100, 1) if ca_prec > 0 else 0

    # CA annuel
    ca_annuel_row = db.execute("SELECT SUM(ca) as t FROM ca_jour WHERE restaurant_id=? AND date LIKE ?", (_rid(), f"{y}%",)).fetchone()
    ca_annuel = round(ca_annuel_row["t"], 2) if ca_annuel_row["t"] else 0

    return render_template("base.html", page="recettes",
        mois=mois, mois_label=_mois_label(mois), mois_list=_get_mois_list(),
        jours=jours, ca_mois=ca_mois, ca_moyen=ca_moyen,
        evo_pct=evo_pct, ca_prec=ca_prec, ca_annuel=ca_annuel,
        nb_jours_saisis=nb_jours_saisis, today=date.today().isoformat())


@app.route("/saisie-rapide")
def saisie_rapide():
    return render_template("base.html", page="saisie_rapide", today=date.today().isoformat())


@app.route("/recettes/save", methods=["POST"])
def recettes_save():
    db = get_db()
    data = request.get_json()
    d = data.get("date", "")
    ca_resto = float(data.get("ca_restaurant", 0) or 0)
    ca_bar_val = float(data.get("ca_bar", 0) or 0)
    ca = round(ca_resto + ca_bar_val, 2)
    midi = int(data.get("couverts_midi", 0) or 0)
    soir = int(data.get("couverts_soir", 0) or 0)
    total_couv = midi + soir
    tm = round(ca_resto / total_couv, 2) if total_couv > 0 else 0
    comm = data.get("commentaire", "")
    sync.write_ca_jour(db, d, ca, ca_resto, ca_bar_val, midi, soir, tm, comm)
    return jsonify({"ok": True})


@app.route("/recettes/export")
def recettes_export():
    db = get_db()
    mois = request.args.get("mois", _mois_courant())
    fmt = request.args.get("format", "csv")
    rows = db.execute("SELECT * FROM ca_jour WHERE restaurant_id=? AND date LIKE ? ORDER BY date", (_rid(), mois + "%",)).fetchall()
    data = []
    for r in rows:
        data.append({
            "Date": r["date"],
            "CA Restaurant": round(r["ca_restaurant"] or 0, 2),
            "CA Bar": round(r["ca_bar"] or 0, 2),
            "CA Total": round(r["ca"] or 0, 2),
            "Couverts": (r["couverts_midi"] or 0) + (r["couverts_soir"] or 0),
            "Ticket Moyen": round(r["ticket_moyen"] or 0, 2),
            "Note": r["commentaire"] or "",
        })

    if fmt == "csv":
        import csv
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Date", "CA Restaurant", "CA Bar", "CA Total", "Couverts", "Ticket Moyen", "Note"])
        for d in data:
            writer.writerow([d["Date"], d["CA Restaurant"], d["CA Bar"], d["CA Total"], d["Couverts"], d["Ticket Moyen"], d["Note"]])
        resp = make_response(output.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = f"attachment; filename=recettes-{mois}.csv"
        return resp

    elif fmt == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Recettes"
        ws.append(["Date", "CA Restaurant", "CA Bar", "CA Total", "Couverts", "Ticket Moyen", "Note"])
        for d in data:
            ws.append([d["Date"], d["CA Restaurant"], d["CA Bar"], d["CA Total"], d["Couverts"], d["Ticket Moyen"], d["Note"]])
        # Total row
        ws.append(["TOTAL", sum(d["CA Restaurant"] for d in data), sum(d["CA Bar"] for d in data),
                   sum(d["CA Total"] for d in data), sum(d["Couverts"] for d in data), "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = make_response(buf.getvalue())
        resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        resp.headers["Content-Disposition"] = f"attachment; filename=recettes-{mois}.xlsx"
        return resp

    elif fmt == "pdf":
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
        styles = getSampleStyleSheet()
        elements = []
        elements.append(Paragraph("Pauco — Recettes", styles["Title"]))
        elements.append(Paragraph(_mois_label(mois), styles["Normal"]))
        elements.append(Spacer(1, 12))
        header = ["Date", "CA Restaurant", "CA Bar", "CA Total", "Couverts", "TM", "Note"]
        tdata = [header]
        for d in data:
            tdata.append([d["Date"], f'{d["CA Restaurant"]:.2f}', f'{d["CA Bar"]:.2f}',
                         f'{d["CA Total"]:.2f}', str(d["Couverts"]), f'{d["Ticket Moyen"]:.2f}', d["Note"][:30]])
        tdata.append(["TOTAL", f'{sum(d["CA Restaurant"] for d in data):.2f}',
                      f'{sum(d["CA Bar"] for d in data):.2f}', f'{sum(d["CA Total"] for d in data):.2f}',
                      str(sum(d["Couverts"] for d in data)), "", ""])
        t = Table(tdata, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.06, 0.12, 0.08)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.Color(0.8, 0.8, 0.8)),
            ("BACKGROUND", (0, -1), (-1, -1), colors.Color(0.95, 0.95, 0.95)),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 12))
        elements.append(Paragraph(f"Genere le {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["Normal"]))
        doc.build(elements)
        buf.seek(0)
        resp = make_response(buf.getvalue())
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f"attachment; filename=recettes-{mois}.pdf"
        return resp

        return "Format non supporte", 400


# ---------------------------------------------------------------------------
#  Dépenses CRUD — moved to modules/routes_depenses.py
# ---------------------------------------------------------------------------

def _ratios_for_mois_list(db, mois_values):
    """Calcule les ratios pour une liste de mois (format 'YYYY-MM')."""
    ratios_data = []
    for mv in mois_values:
        stats = _stats_mois(db, mv)
        ca = stats["ca_total"]
        ca_resto = stats["ca_restaurant"] if stats["ca_restaurant"] else ca
        dep = _depenses_mois(db, mv)
        total_dep = dep["total"]

        food = round(dep["food"] / ca * 100, 1) if ca > 0 else 0
        bev = round(dep["beverage"] / ca * 100, 1) if ca > 0 else 0
        labour = round(dep["personnel"] / ca * 100, 1) if ca > 0 else 0
        prime = round((dep["food"] + dep["beverage"] + dep["personnel"]) / ca * 100, 1) if ca > 0 else 0
        net = round((ca - total_dep) / ca * 100, 1) if ca > 0 else 0
        tm = stats["ticket_moyen"]

        ratios_data.append({
            "mois": _mois_label(mv),
            "ca": ca,
            "food_cost": food,
            "beverage_cost": bev,
            "labour_cost": labour,
            "prime_cost": prime,
            "resultat_net": net,
            "ticket_moyen": tm,
        })
    return ratios_data


def _get_available_years(db):
    """Retourne les annees distinctes presentes dans les donnees."""
    years = set()
    for row in db.execute("SELECT DISTINCT substr(date, 1, 4) as y FROM ca_jour WHERE restaurant_id=?", (_rid(),)).fetchall():
        years.add(row["y"])
    for row in db.execute("SELECT DISTINCT substr(mois, 1, 4) as y FROM depenses_fixes WHERE restaurant_id=?", (_rid(),)).fetchall():
        years.add(row["y"])
    for row in db.execute("SELECT DISTINCT substr(mois, 1, 4) as y FROM depenses_variables WHERE restaurant_id=?", (_rid(),)).fetchall():
        years.add(row["y"])
    years.add(str(date.today().year))
    return sorted(years, reverse=True)


@app.route("/ratios")
def ratios():
    db = get_db()
    annees = _get_available_years(db)
    annee_courante = str(date.today().year)
    mois = _mois_courant()
    seuil = _seuil_rentabilite(db, mois)
    today = date.today()
    jours_restants = __import__('calendar').monthrange(today.year, today.month)[1] - today.day
    return render_template("base.html", page="ratios", annees=annees, annee_courante=annee_courante,
                           seuil=seuil, jours_restants_mois=jours_restants)


@app.route("/ratios/data")
def ratios_data():
    db = get_db()
    annee = request.args.get("annee")
    de = request.args.get("de")
    a = request.args.get("a")

    last6 = request.args.get("last6")

    mois_values = []
    if last6:
        # 6 derniers mois
        today = date.today()
        for i in range(5, -1, -1):
            m = today.month - i
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            mois_values.append(f"{y:04d}-{m:02d}")
    elif annee:
        for m in range(1, 13):
            mois_values.append(f"{annee}-{m:02d}")
    elif de and a:
        try:
            y1, m1 = int(de.split("-")[0]), int(de.split("-")[1])
            y2, m2 = int(a.split("-")[0]), int(a.split("-")[1])
            cy, cm = y1, m1
            while (cy, cm) <= (y2, m2):
                mois_values.append(f"{cy:04d}-{cm:02d}")
                cm += 1
                if cm > 12:
                    cm = 1
                    cy += 1
        except (ValueError, IndexError):
            return jsonify({"error": "Format de date invalide"}), 400
    else:
        annee = str(date.today().year)
        for m in range(1, 13):
            mois_values.append(f"{annee}-{m:02d}")

    data = _ratios_for_mois_list(db, mois_values)
    has_data = any(r["ca"] > 0 for r in data)
    return jsonify({"ratios": data, "has_data": has_data})


@app.route("/ratios/exemples")
def ratios_exemples():
    return render_template("base.html", page="ratios_exemples")


@app.route("/ratios/exemples/mes-ratios")
def mes_ratios_api():
    """Retourne les ratios moyens des 3 derniers mois du gerant."""
    db = get_db()
    today = date.today()
    mois_list = []
    for i in range(3):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        mois_list.append(f"{y:04d}-{m:02d}")

    total_ca = 0
    total_dep_var = 0
    total_dep_fix = 0
    total_food = 0
    total_bev = 0
    total_personnel = 0
    nb_mois_data = 0
    for mv in mois_list:
        stats = _stats_mois(db, mv)
        if stats["ca_total"] > 0:
            nb_mois_data += 1
            total_ca += stats["ca_total"]
            dep = _depenses_mois(db, mv)
            total_dep_var += dep["total_variables"]
            total_dep_fix += dep["total_fixes"]
            total_food += dep["food"]
            total_bev += dep["beverage"]
            total_personnel += dep["personnel"]

    if nb_mois_data == 0 or total_ca == 0:
        return jsonify({"has_data": False})

    food_cost = round(total_food / total_ca * 100, 1)
    beverage_cost = round(total_bev / total_ca * 100, 1)
    labour_cost = round(total_personnel / total_ca * 100, 1)
    prime_cost = round((total_food + total_bev + total_personnel) / total_ca * 100, 1)
    resultat_net = round((total_ca - total_dep_var - total_dep_fix) / total_ca * 100, 1)

    return jsonify({
        "has_data": True,
        "food_cost": food_cost,
        "beverage_cost": beverage_cost,
        "labour_cost": labour_cost,
        "prime_cost": prime_cost,
        "resultat_net": resultat_net,
        "nb_mois": nb_mois_data,
    })


# ---------------------------------------------------------------------------
#  RH — Effectifs
# ---------------------------------------------------------------------------

def _get_postes_grouped(db):
    """Retourne (poles_ordered, grouped_dict) — poles dans l'ordre, postes par pole."""
    rows = db.execute("SELECT * FROM postes WHERE restaurant_id=? ORDER BY pole, nom", (_rid(),)).fetchall()
    grouped = {}
    for r in rows:
        pole = r["pole"]
        if pole not in grouped:
            grouped[pole] = []
        grouped[pole].append(dict(r))
    # Charger l'ordre sauvegarde depuis settings
    rid = getattr(current_user, "restaurant_id", "") if current_user.is_authenticated else ""
    saved_order = []
    if rid:
        import json as _json
        settings = at.get_all("settings", rid)
        for s in settings:
            if s.get("Cle") == "poles_order":
                try:
                    saved_order = _json.loads(s.get("Valeur") or "[]")
                except Exception:
                    pass
                break
    if saved_order:
        poles_ordered = [p for p in saved_order if p in grouped]
        for p in sorted(grouped.keys()):
            if p not in poles_ordered:
                poles_ordered.append(p)
    else:
        _DEFAULT_POLES = ["Salle", "Cuisine", "Bar"]
        poles_ordered = [p for p in _DEFAULT_POLES if p in grouped]
        for p in sorted(grouped.keys()):
            if p not in poles_ordered:
                poles_ordered.append(p)
    return poles_ordered, grouped


@app.route("/rh/postes", methods=["GET", "POST"])
def rh_postes():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "add":
            nom = request.form.get("nom", "").strip()
            pole = request.form.get("pole", "Salle")
            if nom:
                sync.write_poste(db, nom, pole)
        elif action == "delete":
            pid = int(request.form.get("id", 0) or 0)
            sync.delete_poste(db, pid)
        db.commit()
        return redirect(url_for("rh_postes"))

    poles_ordered, postes_grouped = _get_postes_grouped(db)
    return render_template("base.html", page="postes",
                           poles_ordered=poles_ordered, postes_grouped=postes_grouped)


@app.route("/rh/postes/category", methods=["POST"])
@login_required
def rh_postes_category():
    """Gestion des categories (poles) : ajout, renommage, suppression."""
    rid = current_user.restaurant_id
    data = request.get_json()
    action = data.get("action", "")
    db = get_db()

    if action == "add_pole":
        nom = data.get("nom", "").strip()
        if not nom:
            return jsonify({"ok": False, "error": "Nom requis"}), 400
        # Rien a creer en base — le pole existe des qu'un poste l'utilise
        # On cree un poste placeholder invisible pour materialiser le pole
        # Non — on n'a pas besoin de placeholder, le pole sera vide dans la UI
        return jsonify({"ok": True})

    elif action == "rename_pole":
        old_name = data.get("old_name", "").strip()
        new_name = data.get("new_name", "").strip()
        if not old_name or not new_name:
            return jsonify({"ok": False, "error": "Noms requis"}), 400
        # Renommer dans SQLite
        db.execute("UPDATE postes SET pole=? WHERE pole=?", (new_name, old_name))
        db.commit()
        # Renommer dans Airtable
        if rid and not sync._is_demo():
            try:
                postes = at.get_all("postes", rid, formula=f"{{Pole}}='{old_name}'")
                for p in postes:
                    at.update("postes", p["id"], {"Pole": new_name})
            except Exception as e:
                print(f"[POSTES] Rename pole Airtable error: {e}")
        return jsonify({"ok": True})

    elif action == "delete_pole":
        pole_name = data.get("pole", "").strip()
        if not pole_name:
            return jsonify({"ok": False, "error": "Pole requis"}), 400
        # Supprimer tous les postes de ce pole
        rows = db.execute("SELECT id FROM postes WHERE restaurant_id=? AND pole=?", (_rid(), pole_name,)).fetchall()
        for r in rows:
            sync.delete_poste(db, r["id"])
        db.commit()
        return jsonify({"ok": True})

    elif action == "move_pole":
        pole_name = data.get("pole", "").strip()
        direction = data.get("direction", "")
        # L'ordre est cosmétique — on le gère via un champ settings
        # Pour simplifier, on stocke l'ordre dans les settings Airtable
        import json as _json
        settings = at.get_all("settings", rid)
        order_rec = None
        current_order = []
        for s in settings:
            if s.get("Cle") == "poles_order":
                order_rec = s
                try:
                    current_order = _json.loads(s.get("Valeur") or "[]")
                except Exception:
                    pass
                break
        if not current_order:
            _, grouped = _get_postes_grouped(db)
            current_order = list(grouped.keys())
        if pole_name in current_order:
            idx = current_order.index(pole_name)
            if direction == "up" and idx > 0:
                current_order[idx], current_order[idx-1] = current_order[idx-1], current_order[idx]
            elif direction == "down" and idx < len(current_order) - 1:
                current_order[idx], current_order[idx+1] = current_order[idx+1], current_order[idx]
        val = _json.dumps(current_order, ensure_ascii=False)
        if order_rec:
            at.update("settings", order_rec["id"], {"Valeur": val})
        else:
            at.create("settings", {"Restaurant_ID": rid, "Cle": "poles_order", "Valeur": val})
        at.invalidate_cache("settings")
        return jsonify({"ok": True})

    return jsonify({"ok": False}), 400


@app.route("/rh/effectifs", methods=["GET", "POST"])
def rh_effectifs():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            prenom = request.form.get("prenom", "").strip()
            nom = request.form.get("nom", "").strip()
            poste = request.form.get("poste", "Salle")
            contrat = request.form.get("type_contrat", "CDI")
            date_deb = request.form.get("date_debut", "") or datetime.now().strftime("%Y-%m-%d")
            if not prenom or not nom:
                return redirect(url_for("rh_effectifs"))
            sync.write_employe(db, prenom, nom, poste, contrat, date_deb,
                               request.form.get("date_fin", ""),
                               float(request.form.get("salaire_brut", 0) or 0),
                               float(request.form.get("heures_semaine", 35) or 35),
                               request.form.get("phone", ""),
                               request.form.get("date_naissance", ""))
        elif action == "update":
            eid = int(request.form.get("id", 0))
            if not eid:
                return redirect(url_for("rh_effectifs"))
            db.execute("""UPDATE employes SET prenom=?, nom=?, poste=?, type_contrat=?, date_debut=?, date_fin=?, salaire_brut=?, heures_semaine=?, phone=?, date_naissance=?
                          WHERE id=?""",
                       (request.form.get("prenom", ""), request.form.get("nom", ""),
                        request.form.get("poste", "Salle"), request.form.get("type_contrat", "CDI"),
                        request.form.get("date_debut", ""),
                        request.form.get("date_fin", ""),
                        float(request.form.get("salaire_brut", 0) or 0),
                        float(request.form.get("heures_semaine", 35) or 35),
                        request.form.get("phone", ""),
                        request.form.get("date_naissance", ""), eid))
        elif action in ("deactivate", "activate", "archive"):
            eid = int(request.form.get("id", 0) or 0)
            statut_map = {"deactivate": "Inactif", "activate": "Actif", "archive": "Archive"}
            new_statut = statut_map[action]
            emp = db.execute("SELECT prenom, nom FROM employes WHERE restaurant_id=? AND id=?", (_rid(), eid)).fetchone()
            db.execute("UPDATE employes SET statut=? WHERE id=?", (new_statut, eid))
            rid = _rid()
            if emp and rid and not getattr(current_user, "demo_mode", False):
                try:
                    at_rec = at.find_first("employes",
                        f"AND({{Restaurant_ID}}='{rid}',{{Prénom}}='{emp['prenom']}',{{Nom}}='{emp['nom']}')")
                    if at_rec:
                        at.update_employe(at_rec["id"], statut=new_statut)
                        at.invalidate_cache("employes")
                except Exception as e:
                    print(f"[SYNC] Status change error: {e}")
        elif action == "add_poste":
            nom = request.form.get("poste_nom", "").strip()
            pole = request.form.get("pole", "Salle")
            if nom:
                sync.write_poste(db, nom, pole)
        elif action == "delete_poste":
            pid = int(request.form.get("poste_id", 0) or 0)
            sync.delete_poste(db, pid)
        db.commit()
        return redirect(url_for("rh_effectifs"))

    # Réconciliation Actif depuis Airtable (source de vérité) — la table SQLite locale peut
    # être stale si le champ Actif a été modifié dans Airtable depuis le dernier sync.
    rid = _rid()
    actif_names = None
    if rid and not getattr(current_user, "demo_mode", False):
        try:
            at.invalidate_cache("employes", rid)
            airtable_actifs = at.get_employes(rid, actif_only=True)
            actif_names = {
                ((e.get("Prénom") or e.get("Prenom") or "").strip().lower(),
                 (e.get("Nom") or "").strip().lower())
                for e in airtable_actifs
            }
        except Exception as ex:
            print(f"[EFFECTIFS] Airtable Actif sync error: {ex}")

    all_emp = db.execute("SELECT * FROM employes WHERE restaurant_id=? ORDER BY ordre, nom, prenom", (_rid(),)).fetchall()
    # Build poste->pole map from postes table
    poste_pole_map = {}
    for p in db.execute("SELECT nom, pole FROM postes WHERE restaurant_id=?", (_rid(),)).fetchall():
        poste_pole_map[p["nom"]] = p["pole"]
    employes = []
    archives_emp = []
    for e in all_emp:
        d = dict(e)
        d["pole"] = poste_pole_map.get(d["poste"]) or sync._infer_pole(d["poste"])
        d["cp_solde"] = _solde_cp(db, d["id"], d.get("cp_acquis", 0), d.get("date_debut", ""))
        d["compteur_total"] = _compteur_total(db, d["id"], d.get("heures_semaine", 35), d.get("date_debut", ""),
                                              d.get("solde_initial", 0), d.get("date_debut_compteur", ""))
        # Override statut depuis Airtable si réconciliation possible
        if actif_names is not None:
            key = ((d.get("prenom") or "").strip().lower(), (d.get("nom") or "").strip().lower())
            is_actif_at = key in actif_names
            new_statut = "Actif" if is_actif_at else "Inactif"
            if d.get("statut") != new_statut:
                try:
                    db.execute("UPDATE employes SET statut=? WHERE id=?", (new_statut, d["id"]))
                except Exception:
                    pass
                d["statut"] = new_statut
        if d["statut"] in ("Archive", "Inactif"):
            archives_emp.append(d)
        else:
            employes.append(d)
    db.commit()
    poles_ordered, postes = _get_postes_grouped(db)
    return render_template("base.html", page="effectifs", employes=employes, archives_emp=archives_emp, postes_grouped=postes, poles_ordered=poles_ordered)


# ---------------------------------------------------------------------------
#  RH — Fiches Employes
# ---------------------------------------------------------------------------

@app.route("/rh/employes/<int:id>")
def employe_fiche(id):
    db = get_db()
    emp = db.execute("SELECT * FROM employes WHERE restaurant_id=? AND id=?", (_rid(), id,)).fetchone()
    if not emp:
        return redirect(url_for("rh_effectifs"))
    emp_dict = dict(emp)
    poste_pole_map = {p["nom"]: p["pole"] for p in db.execute("SELECT nom, pole FROM postes WHERE restaurant_id=?", (_rid(),)).fetchall()}
    emp_dict["pole"] = poste_pole_map.get(emp_dict["poste"]) or sync._infer_pole(emp_dict["poste"])
    emp_dict["cp_solde"] = _solde_cp(db, emp_dict["id"], emp_dict.get("cp_acquis", 0), emp_dict.get("date_debut", ""))
    emp_dict["compteur_total"] = _compteur_total(db, emp_dict["id"], emp_dict.get("heures_semaine", 35),
                                                  emp_dict.get("date_debut", ""), emp_dict.get("solde_initial", 0),
                                                  emp_dict.get("date_debut_compteur", ""))
    # Resolve photo URL
    if emp_dict.get("photo_url"):
        try:
            from modules.r2_client import get_file_url, is_configured
            emp_dict["photo_url"] = get_file_url(emp_dict["photo_url"]) if is_configured() else ""
        except Exception:
            emp_dict["photo_url"] = ""
    # Stable key survives Airtable sync re-IDs (employes table is cleared/re-inserted on sync)
    emp_key = f"{(emp_dict.get('prenom') or '').strip().lower()}|{(emp_dict.get('nom') or '').strip().lower()}"
    # Fix orphan documents (uploaded without restaurant_id) and backfill emp_key
    db.execute("UPDATE employe_documents SET restaurant_id=? WHERE employe_id=? AND (restaurant_id='' OR restaurant_id IS NULL)", (_rid(), id))
    db.execute("UPDATE employe_documents SET emp_key=? WHERE restaurant_id=? AND employe_id=? AND (emp_key='' OR emp_key IS NULL)", (emp_key, _rid(), id))
    # Re-attach docs that became orphaned by a previous sync (employe_id changed but emp_key matches)
    db.execute("UPDATE employe_documents SET employe_id=? WHERE restaurant_id=? AND emp_key=? AND employe_id<>?", (id, _rid(), emp_key, id))
    db.commit()
    docs = db.execute("SELECT * FROM employe_documents WHERE restaurant_id=? AND (employe_id=? OR emp_key=?) ORDER BY uploaded_at DESC", (_rid(), id, emp_key)).fetchall()
    # Source of truth = Airtable (survit aux redéploiements Railway qui wipe SQLite).
    # On merge avec SQLite (cache local) au cas où Airtable aurait un retard.
    rid_for_at = _rid()
    at_docs = []
    if rid_for_at and not getattr(current_user, "demo_mode", False):
        at_docs = at.get_employe_documents(rid_for_at, emp_dict.get("prenom", ""), emp_dict.get("nom", ""))
    # Index par r2_key pour dédup
    by_key = {}
    for d in at_docs:
        if isinstance(d, dict) and d.get("r2_key"):
            by_key[d["r2_key"]] = {"nom": d.get("nom", ""), "r2_key": d["r2_key"], "uploaded_at": d.get("uploaded_at", "")}
    for d in docs:
        if d["r2_key"] not in by_key:
            by_key[d["r2_key"]] = {"nom": d["nom"], "r2_key": d["r2_key"], "uploaded_at": d["uploaded_at"]}
    # Si Airtable a des docs absents en local, on backfill SQLite
    for k, dd in by_key.items():
        exists = db.execute("SELECT 1 FROM employe_documents WHERE restaurant_id=? AND r2_key=?", (_rid(), k)).fetchone()
        if not exists:
            db.execute("INSERT INTO employe_documents (restaurant_id, employe_id, nom, r2_key, uploaded_at, emp_key) VALUES (?,?,?,?,?,?)",
                       (_rid(), id, dd["nom"], k, dd["uploaded_at"], emp_key))
    db.commit()
    docs_list = []
    for dd in sorted(by_key.values(), key=lambda x: x.get("uploaded_at", ""), reverse=True):
        try:
            from modules.r2_client import get_file_url, is_configured
            dd["url"] = get_file_url(dd["r2_key"]) if is_configured() else ""
        except Exception:
            dd["url"] = ""
        # Pour le template (delete) on a besoin d'un id sqlite
        row = db.execute("SELECT id FROM employe_documents WHERE restaurant_id=? AND r2_key=?", (_rid(), dd["r2_key"])).fetchone()
        dd["id"] = row["id"] if row else 0
        docs_list.append(dd)
    poles_ordered, postes = _get_postes_grouped(db)
    return render_template("base.html", page="employe_fiche", emp=emp_dict, documents=docs_list, postes_grouped=postes, poles_ordered=poles_ordered)


@app.route("/rh/employes/<int:id>/update", methods=["POST"])
def employe_update(id):
    db = get_db()
    # Read old prenom+nom before update (for Airtable lookup)
    old = db.execute("SELECT prenom, nom FROM employes WHERE restaurant_id=? AND id=?", (_rid(), id)).fetchone()
    prenom = request.form.get("prenom", "")
    nom = request.form.get("nom", "")
    poste = request.form.get("poste", "Salle")
    type_contrat = request.form.get("type_contrat", "CDI")
    date_debut = request.form.get("date_debut", "")
    date_fin = request.form.get("date_fin", "")
    salaire = float(request.form.get("salaire_brut", 0) or 0)
    heures = float(request.form.get("heures_semaine", 35) or 35)
    phone = request.form.get("phone", "")
    date_naissance = request.form.get("date_naissance", "")
    email = request.form.get("email", "")
    adresse = request.form.get("adresse", "")
    numero_secu = request.form.get("numero_secu", "")
    iban = request.form.get("iban", "")
    solde_initial = float(request.form.get("solde_initial", 0) or 0)
    date_debut_compteur = request.form.get("date_debut_compteur", "")
    cp_acquis = float(request.form.get("cp_acquis", 0) or 0)

    db.execute("""UPDATE employes SET prenom=?, nom=?, poste=?, type_contrat=?, date_debut=?,
                  date_fin=?, salaire_brut=?, heures_semaine=?, phone=?, date_naissance=?,
                  email=?, adresse=?, numero_secu=?, iban=?,
                  solde_initial=?, date_debut_compteur=?, cp_acquis=? WHERE id=?""",
               (prenom, nom, poste, type_contrat, date_debut, date_fin,
                salaire, heures, phone, date_naissance, email, adresse,
                numero_secu, iban, solde_initial, date_debut_compteur, cp_acquis, id))
    db.commit()
    # Sync to Airtable
    rid = _rid()
    if rid and not getattr(current_user, "demo_mode", False):
        try:
            old_prenom = old["prenom"] if old else prenom
            old_nom = old["nom"] if old else nom
            at_rec = at.find_first("employes",
                f"AND({{Restaurant_ID}}='{rid}',{{Prénom}}='{old_prenom}',{{Nom}}='{old_nom}')")
            if at_rec:
                print(f"[SYNC] Updating employe {at_rec['id']}: cp_acquis={cp_acquis}, solde_initial={solde_initial}, date_debut_compteur={date_debut_compteur}")
                result = at.update_employe(at_rec["id"],
                    prenom=prenom, nom=nom, poste=poste, type_contrat=type_contrat,
                    date_debut=date_debut, date_fin=date_fin, salaire_brut=salaire,
                    heures_semaine=heures, phone=phone, date_naissance=date_naissance,
                    email=email, adresse=adresse, numero_secu=numero_secu, iban=iban,
                    cp_acquis=cp_acquis, solde_initial=solde_initial,
                    date_debut_compteur=date_debut_compteur)
                print(f"[SYNC] Airtable response: CP_acquis={result.get('CP_acquis')}, Solde_Initial={result.get('Solde_Initial')}, Date_Debut_Compteur={result.get('Date_Debut_Compteur')}")
                at.invalidate_cache("employes")
            else:
                print(f"[SYNC] Employe {old_prenom} {old_nom} not found in Airtable for rid={rid}")
        except Exception as e:
            print(f"[SYNC] Airtable update error (employe {id}): {e}")
            import traceback
            traceback.print_exc()
    return redirect(url_for("employe_fiche", id=id))


@app.route("/rh/employes/<int:id>/upload", methods=["POST"])
def employe_upload(id):
    db = get_db()
    f = request.files.get("document")
    if not f or not f.filename:
        return redirect(url_for("employe_fiche", id=id))
    try:
        from modules.r2_client import upload_file, is_configured
        if not is_configured():
            return redirect(url_for("employe_fiche", id=id))
        rid = getattr(current_user, "restaurant_id", "default")
        folder = f"employes/{rid}/{id}"
        import secrets
        safe_name = secrets.token_hex(4) + "_" + f.filename.replace(" ", "_")
        r2_key = upload_file(f, folder, safe_name)
        emp_row = db.execute("SELECT prenom, nom FROM employes WHERE restaurant_id=? AND id=?", (_rid(), id)).fetchone()
        emp_key = f"{(emp_row['prenom'] if emp_row else '').strip().lower()}|{(emp_row['nom'] if emp_row else '').strip().lower()}"
        uploaded_at = date.today().isoformat()
        db.execute("INSERT INTO employe_documents (restaurant_id, employe_id, nom, r2_key, uploaded_at, emp_key) VALUES (?,?,?,?,?,?)",
                   (_rid(), id, f.filename, r2_key, uploaded_at, emp_key))
        db.commit()
        print(f"[R2] Upload OK: {r2_key} for employe {id}")
        # Persistance Airtable (source de vérité — Railway wipe filesystem à chaque deploy)
        rid_at = _rid()
        if emp_row and rid_at and not getattr(current_user, "demo_mode", False):
            try:
                existing = at.get_employe_documents(rid_at, emp_row["prenom"], emp_row["nom"])
                existing.append({"nom": f.filename, "r2_key": r2_key, "uploaded_at": uploaded_at})
                at.set_employe_documents(rid_at, emp_row["prenom"], emp_row["nom"], existing)
            except Exception as ee:
                print(f"[AIRTABLE] persist documents error: {ee}")
    except Exception as e:
        print(f"[R2] Upload error: {e}")
        import traceback
        traceback.print_exc()
    return redirect(url_for("employe_fiche", id=id))


@app.route("/rh/employes/<int:id>/document/<int:doc_id>/delete")
def employe_doc_delete(id, doc_id):
    db = get_db()
    doc = db.execute("SELECT r2_key FROM employe_documents WHERE restaurant_id=? AND id=? AND employe_id=?", (_rid(), doc_id, id)).fetchone()
    if doc:
        try:
            from modules.r2_client import delete_file, is_configured
            if is_configured():
                delete_file(doc["r2_key"])
        except Exception as e:
            print(f"[R2] Delete error: {e}")
        db.execute("DELETE FROM employe_documents WHERE id=?", (doc_id,))
        db.commit()
        # Mise à jour Airtable
        rid_at = _rid()
        emp_row = db.execute("SELECT prenom, nom FROM employes WHERE restaurant_id=? AND id=?", (rid_at, id)).fetchone()
        if emp_row and rid_at and not getattr(current_user, "demo_mode", False):
            try:
                existing = at.get_employe_documents(rid_at, emp_row["prenom"], emp_row["nom"])
                pruned = [d for d in existing if isinstance(d, dict) and d.get("r2_key") != doc["r2_key"]]
                at.set_employe_documents(rid_at, emp_row["prenom"], emp_row["nom"], pruned)
            except Exception as ee:
                print(f"[AIRTABLE] prune documents error: {ee}")
    return redirect(url_for("employe_fiche", id=id))


@app.route("/rh/employes/<int:id>/archive", methods=["POST"])
def employe_archive(id):
    db = get_db()
    action = request.form.get("action", "archive")
    new_statut = "Actif" if action == "reactivate" else "Archive"
    emp = db.execute("SELECT prenom, nom FROM employes WHERE restaurant_id=? AND id=?", (_rid(), id)).fetchone()
    db.execute("UPDATE employes SET statut=? WHERE id=?", (new_statut, id))
    db.commit()
    # Sync to Airtable
    rid = _rid()
    if emp and rid and not getattr(current_user, "demo_mode", False):
        try:
            at_rec = at.find_first("employes",
                f"AND({{Restaurant_ID}}='{rid}',{{Prénom}}='{emp['prenom']}',{{Nom}}='{emp['nom']}')")
            if at_rec:
                at.update_employe(at_rec["id"], statut=new_statut)
                at.invalidate_cache("employes")
                print(f"[SYNC] Employe {emp['prenom']} {emp['nom']} -> {new_statut} in Airtable")
        except Exception as e:
            print(f"[SYNC] Archive error: {e}")
    return redirect(url_for("rh_effectifs"))


@app.route("/rh/employes/<int:id>/photo", methods=["POST"])
def employe_photo(id):
    db = get_db()
    f = request.files.get("photo")
    if not f or not f.filename:
        return redirect(url_for("employe_fiche", id=id))
    try:
        from modules.r2_client import upload_file, is_configured
        if not is_configured():
            return redirect(url_for("employe_fiche", id=id))
        rid = getattr(current_user, "restaurant_id", "default")
        import secrets
        ext = f.filename.rsplit(".", 1)[-1] if "." in f.filename else "jpg"
        key = upload_file(f, f"employes/{rid}/{id}/photo", f"avatar.{ext}")
        from modules.r2_client import get_file_url
        db.execute("UPDATE employes SET photo_url=? WHERE id=?", (key, id))
        db.commit()
    except Exception as e:
        print(f"[R2] Photo upload error: {e}")
    return redirect(url_for("employe_fiche", id=id))


# ---------------------------------------------------------------------------
#  RH — Planning
# ---------------------------------------------------------------------------

@app.route("/rh/planning")
def rh_planning():
    db = get_db()
    _ensure_default_absences(_rid())
    # Semaine courante ou navigee
    week_offset = int(request.args.get("week", 0))
    today = date.today()
    # Lundi de la semaine
    lundi = today - __import__("datetime").timedelta(days=today.weekday()) + __import__("datetime").timedelta(weeks=week_offset)
    jours = []
    JOURS_NOMS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    for i in range(7):
        d = lundi + __import__("datetime").timedelta(days=i)
        jours.append({"date": d.isoformat(), "label": f"{JOURS_NOMS[i]} {d.day:02d}/{d.month:02d}"})

    # Staff role: read-only, own shifts only
    is_staff = getattr(current_user, "role", "") == "Staff"
    staff_emp_id = None

    all_emp = db.execute("SELECT * FROM employes WHERE restaurant_id=? AND statut NOT IN ('Inactif','Archive') ORDER BY ordre, nom, prenom", (_rid(),)).fetchall()
    poste_pole_map = {p["nom"]: p["pole"] for p in db.execute("SELECT nom, pole FROM postes WHERE restaurant_id=?", (_rid(),)).fetchall()}

    # Match Staff user to their employee record
    if is_staff:
        user_prenom = getattr(current_user, "prenom", "") or ""
        user_nom = getattr(current_user, "nom", "") or ""
        user_email = current_user.email or ""
        for e in all_emp:
            if (e["prenom"].lower() == user_prenom.lower() and e["nom"].lower() == user_nom.lower()):
                staff_emp_id = e["id"]
                break
            if e.get("email", "").lower() == user_email.lower() and user_email:
                staff_emp_id = e["id"]
                break

    employes = []
    for e in all_emp:
        if is_staff and staff_emp_id is not None and e["id"] != staff_emp_id:
            continue
        if is_staff and staff_emp_id is None:
            continue  # No matching employee — show empty planning
        d = dict(e)
        d["pole"] = poste_pole_map.get(d["poste"]) or sync._infer_pole(d["poste"])
        d["cp_solde"] = _solde_cp(db, d["id"], d.get("cp_acquis", 0), d.get("date_debut", ""))
        employes.append(d)

    # Creneaux de la semaine
    date_debut = jours[0]["date"]
    date_fin = jours[6]["date"]
    if is_staff:
        if staff_emp_id is not None:
            creneaux = db.execute("""SELECT p.*, e.prenom, e.nom as emp_nom FROM planning p
                                     JOIN employes e ON p.employe_id = e.id
                                     WHERE p.restaurant_id=? AND p.date >= ? AND p.date <= ? AND p.employe_id = ? AND e.statut NOT IN ('Inactif','Archive')
                                     ORDER BY p.date, p.heure_debut""", (_rid(), date_debut, date_fin, staff_emp_id)).fetchall()
        else:
            creneaux = []
    else:
        creneaux = db.execute("""SELECT p.*, e.prenom, e.nom as emp_nom FROM planning p
                                 JOIN employes e ON p.employe_id = e.id
                                 WHERE p.restaurant_id=? AND p.date >= ? AND p.date <= ? AND e.statut NOT IN ('Inactif','Archive')
                                 ORDER BY p.date, p.heure_debut""", (_rid(), date_debut, date_fin)).fetchall()

    # Couleurs par défaut par pôle (fallback si le shift n'a pas de couleur)
    _POLE_FALLBACK = {"Salle": "#2D6A4A", "Bar": "#1D4ED8", "Cuisine": "#D97706"}

    # Build shift lookup: exact + closest match
    all_shifts = db.execute("SELECT id, nom, heure_debut, heure_fin, couleur, pole FROM shifts WHERE restaurant_id=?", (_rid(),)).fetchall()
    shift_lookup = {}
    shifts_list_for_match = []
    for sh in all_shifts:
        if sh["heure_debut"] and sh["heure_fin"]:
            # Utiliser la couleur du shift, fallback sur la couleur du pôle
            couleur = sh["couleur"] or _POLE_FALLBACK.get(sh["pole"], "#6B7280")
            shift_lookup[(sh["heure_debut"], sh["heure_fin"])] = (sh["nom"], couleur, sh["id"], sh["pole"])
            entry = dict(sh)
            entry["couleur"] = couleur
            shifts_list_for_match.append(entry)

    def _to_min(t):
        try:
            p = t.split(":")
            return int(p[0]) * 60 + int(p[1])
        except Exception:
            return -1

    def _match_shift(hd, hf):
        """Match exact d'abord, puis le shift le plus proche (max 60min par borne)."""
        exact = shift_lookup.get((hd, hf))
        if exact:
            return {"shift_nom": exact[0], "shift_couleur": exact[1], "shift_id": exact[2]}
        hd_min, hf_min = _to_min(hd), _to_min(hf)
        if hd_min < 0 or hf_min < 0:
            return None
        best, best_diff = None, 121  # max 60min par borne = 120 total
        for sh in shifts_list_for_match:
            d_start = abs(_to_min(sh["heure_debut"]) - hd_min)
            d_end = abs(_to_min(sh["heure_fin"]) - hf_min)
            if d_start > 60 or d_end > 60:
                continue  # chaque borne max 60min d'écart
            diff = d_start + d_end
            if diff < best_diff:
                best_diff = diff
                best = sh
        if best:
            return {"shift_nom": best["nom"], "shift_couleur": best["couleur"], "shift_id": best["id"]}
        return None

    # Lookup shift_id -> couleur réelle du shift (pas du pôle)
    shift_id_color = {}
    for sh in all_shifts:
        shift_id_color[sh["id"]] = sh["couleur"] or _POLE_FALLBACK.get(sh["pole"], "#6B7280")

    # Codes d'absence connus (pour ne pas tenter de matcher les absences comme des shifts)
    absence_codes = {r["code"] for r in db.execute("SELECT code FROM absence_types WHERE restaurant_id=?", (_rid(),)).fetchall()}
    absence_codes.update({"R", "CP"})  # Toujours inclure R et CP

    # Organiser par employe_id -> date -> list, auto-match shifts
    planning_map = {}
    for c in creneaux:
        eid = c["employe_id"]
        if eid not in planning_map:
            planning_map[eid] = {}
        d = c["date"]
        if d not in planning_map[eid]:
            planning_map[eid][d] = []
        entry = dict(c)
        hd = entry.get("heure_debut", "")
        hf = entry.get("heure_fin", "")
        if hd not in absence_codes:
            if not entry.get("shift_nom"):
                # Auto-fill shift_nom/shift_couleur if missing
                match = _match_shift(hd, hf)
                if match:
                    entry["shift_nom"] = match["shift_nom"]
                    entry["shift_couleur"] = match["shift_couleur"]
                    entry["shift_id"] = match["shift_id"]
            elif entry.get("shift_id"):
                # Rafraîchir la couleur depuis le shift actuel (l'utilisateur a pu la changer)
                real_col = shift_id_color.get(entry["shift_id"])
                if real_col:
                    entry["shift_couleur"] = real_col
        planning_map[eid][d].append(entry)

    # Dedup absences: un seul "Repos"/"CP"/etc. par employé-jour.
    # Si au moins un vrai service existe ce jour-là, on retire toutes les absences.
    for eid_map in planning_map.values():
        for day_key, entries in eid_map.items():
            has_real_shift = any(
                e.get("heure_debut") and e.get("heure_debut") not in absence_codes
                and e.get("heure_debut") != e.get("heure_fin")
                for e in entries
            )
            if has_real_shift:
                eid_map[day_key] = [e for e in entries if e.get("heure_debut") not in absence_codes]
            else:
                seen_abs = set()
                deduped = []
                for e in entries:
                    hd = e.get("heure_debut", "")
                    if hd in absence_codes:
                        if hd in seen_abs:
                            continue
                        seen_abs.add(hd)
                    deduped.append(e)
                eid_map[day_key] = deduped

    # Totaux heures par employe et par jour
    def _hours(h_debut, h_fin):
        try:
            hd = int(h_debut.split(":")[0]) + int(h_debut.split(":")[1]) / 60
            hf = int(h_fin.split(":")[0]) + int(h_fin.split(":")[1]) / 60
            return max(hf - hd, 0)
        except Exception:
            return 0

    def _row_hours(row):
        """Heures travaillées d'un créneau, repas déduit si Repas=True."""
        try:
            base = _hours(row["heure_debut"], row["heure_fin"])
        except Exception:
            base = 0
        try:
            if row["repas"]:
                base = max(base - (int(row["duree_repas"] or 0) / 60.0), 0)
        except (KeyError, IndexError):
            pass
        return base

    totaux_emp = {}
    totaux_jour = {j["date"]: 0 for j in jours}
    for c in creneaux:
        h = _row_hours(c)
        totaux_emp[c["employe_id"]] = totaux_emp.get(c["employe_id"], 0) + h
        totaux_jour[c["date"]] = totaux_jour.get(c["date"], 0) + h

    semaine_label = f"{jours[0]['date'][8:]}/{jours[0]['date'][5:7]} — {jours[6]['date'][8:]}/{jours[6]['date'][5:7]}/{jours[6]['date'][:4]}"

    # Compteur cumule du mois : heures planifiees vs contractuelles
    # Mois de reference = mois du lundi de la semaine affichee
    mois_ref = lundi.strftime("%Y-%m")
    premier_jour_mois = f"{mois_ref}-01"
    # Dernier jour = dimanche de la semaine affichee (pas au-dela)
    dimanche = lundi + __import__("datetime").timedelta(days=6)
    dernier_jour_cumul = dimanche.isoformat()

    # Nombre de semaines ecoulees dans le mois (du 1er du mois au dimanche courant)
    from datetime import timedelta as _td
    premier_mois = __import__("datetime").date(lundi.year, lundi.month, 1) if lundi.month == int(mois_ref.split("-")[1]) else __import__("datetime").date(int(mois_ref.split("-")[0]), int(mois_ref.split("-")[1]), 1)
    nb_jours_mois = (dimanche - premier_mois).days + 1
    nb_semaines_mois = max(nb_jours_mois / 7, 1)

    recap_emp = []
    for emp in employes:
        eid = emp["id"]
        heures_contrat = emp["heures_semaine"]
        heures_semaine = round(totaux_emp.get(eid, 0), 1)
        solde_initial = emp.get("solde_initial") or 0

        # Date de référence compteur = date_debut_compteur ou date_debut
        ref_start_str = emp.get("date_debut_compteur") or emp.get("date_debut") or "2026-03-01"
        try:
            ref_start_date = date.fromisoformat(ref_start_str[:10])
        except (ValueError, TypeError):
            ref_start_date = date.today().replace(day=1)

        # FIX 2 — Écart semaine : proratiser si employé créé en milieu de semaine
        if ref_start_date > lundi:
            jours_dans_semaine = (dimanche - ref_start_date).days + 1
            contrat_semaine_prorata = round(heures_contrat * jours_dans_semaine / 7, 1)
        else:
            contrat_semaine_prorata = heures_contrat
        ecart_semaine = round(heures_semaine - contrat_semaine_prorata, 1)

        # FIX 1 — Solde mois : depuis MAX(ref_start, premier_jour_mois)
        debut_compteur_mois = max(ref_start_date, premier_mois)
        cumul_rows = db.execute("""SELECT heure_debut, heure_fin, repas, duree_repas FROM planning
                                   WHERE restaurant_id=? AND employe_id = ? AND date >= ? AND date <= ?""",
                                (_rid(), eid, debut_compteur_mois.isoformat(), dernier_jour_cumul)).fetchall()
        heures_cumul_mois = round(sum(_row_hours(r) for r in cumul_rows), 1)
        nb_jours_compteur_mois = (dimanche - debut_compteur_mois).days + 1
        nb_sem_mois = max(nb_jours_compteur_mois / 7, 0.1)
        contrat_cumul_mois = round(heures_contrat * nb_sem_mois, 1)
        solde_mois = round(heures_cumul_mois - contrat_cumul_mois, 1)

        # FIX 1 — Solde total : depuis ref_start (date création Pauco)
        all_rows = db.execute("""SELECT heure_debut, heure_fin, repas, duree_repas FROM planning
                                 WHERE restaurant_id=? AND employe_id = ? AND date >= ? AND date <= ?""",
                              (_rid(), eid, ref_start_date.isoformat(), dernier_jour_cumul)).fetchall()
        heures_total = round(sum(_row_hours(r) for r in all_rows), 1)
        nb_jours_total = max((dimanche - ref_start_date).days + 1, 1)
        nb_sem_total = max(nb_jours_total / 7, 0.1)
        contrat_total = round(heures_contrat * nb_sem_total, 1)
        solde_total = round(heures_total - contrat_total + solde_initial, 1)

        recap_emp.append({
            "id": eid,
            "prenom": emp["prenom"],
            "nom": emp["nom"],
            "heures_contrat": heures_contrat,
            "heures_semaine": heures_semaine,
            "ecart_semaine": ecart_semaine,
            "solde_mois": solde_mois,
            "solde_total": solde_total,
        })

    poles_ordered, postes = _get_postes_grouped(db)

    # Vue mensuelle
    vue = request.args.get("vue", "semaine")
    month_offset = int(request.args.get("month", 0))
    from datetime import timedelta as _td2
    import calendar
    mois_vue = today.month + month_offset
    annee_vue = today.year
    while mois_vue <= 0: mois_vue += 12; annee_vue -= 1
    while mois_vue > 12: mois_vue -= 12; annee_vue += 1
    mois_vue_str = f"{annee_vue:04d}-{mois_vue:02d}"
    nb_days = calendar.monthrange(annee_vue, mois_vue)[1]
    month_label = _mois_label(mois_vue_str)

    # Calendrier mensuel : par jour, liste des creneaux
    month_creneaux = {}
    if is_staff and staff_emp_id is not None:
        month_rows = db.execute("""SELECT p.*, e.prenom, e.nom as emp_nom, e.poste FROM planning p
                                   JOIN employes e ON p.employe_id = e.id
                                   WHERE p.restaurant_id=? AND p.date >= ? AND p.date <= ? AND p.employe_id = ? AND e.statut NOT IN ('Inactif','Archive')
                                   ORDER BY p.date, p.heure_debut""",
                                (_rid(), f"{mois_vue_str}-01", f"{mois_vue_str}-{nb_days}", staff_emp_id)).fetchall()
    elif is_staff:
        month_rows = []
    else:
        month_rows = db.execute("""SELECT p.*, e.prenom, e.nom as emp_nom, e.poste FROM planning p
                                   JOIN employes e ON p.employe_id = e.id
                                   WHERE p.restaurant_id=? AND p.date >= ? AND p.date <= ? AND e.statut NOT IN ('Inactif','Archive')
                                   ORDER BY p.date, p.heure_debut""",
                                (_rid(), f"{mois_vue_str}-01", f"{mois_vue_str}-{nb_days}")).fetchall()
    month_emp_hours = {}
    for c in month_rows:
        d = int(c["date"].split("-")[2])
        if d not in month_creneaux:
            month_creneaux[d] = []
        entry = dict(c)
        # Auto-fill shift_nom/shift_couleur if missing (same as weekly view)
        hd = entry.get("heure_debut", "")
        hf = entry.get("heure_fin", "")
        if hd not in absence_codes:
            if not entry.get("shift_nom"):
                match = _match_shift(hd, hf)
                if match:
                    entry["shift_nom"] = match["shift_nom"]
                    entry["shift_couleur"] = match["shift_couleur"]
                    entry["shift_id"] = match["shift_id"]
            elif entry.get("shift_id"):
                pole_col = shift_id_color.get(entry["shift_id"])
                if pole_col:
                    entry["shift_couleur"] = pole_col
        month_creneaux[d].append(entry)
        h = _hours(c["heure_debut"], c["heure_fin"])
        month_emp_hours[c["employe_id"]] = month_emp_hours.get(c["employe_id"], 0) + h

    # Anniversaires du mois
    month_birthdays = {}  # day -> [prenom, ...]
    for emp in employes:
        dn = emp.get("date_naissance", "") or ""
        if dn and len(dn) >= 10:
            try:
                bm, bd = int(dn[5:7]), int(dn[8:10])
                if bm == mois_vue:
                    month_birthdays.setdefault(bd, []).append(emp["prenom"])
            except (ValueError, IndexError):
                pass

    # Semaines du mois (pour le calendrier)
    cal_weeks = calendar.monthcalendar(annee_vue, mois_vue)

    # Congés de la semaine
    conges_semaine = db.execute("""SELECT c.*, e.prenom FROM conges c JOIN employes e ON c.employe_id=e.id
                                   WHERE c.restaurant_id=? AND c.date_debut <= ? AND c.date_fin >= ?""",
                                (_rid(), jours[-1]["date"], jours[0]["date"])).fetchall()
    conges_map = {}
    for c in conges_semaine:
        eid = c["employe_id"]
        if eid not in conges_map:
            conges_map[eid] = []
        conges_map[eid].append(dict(c))

    # Jours de fermeture de la semaine
    fermeture_dates = set()
    for j in jours:
        if _is_ferme(db, j["date"]):
            fermeture_dates.add(j["date"])

    absence_types = db.execute("SELECT * FROM absence_types WHERE restaurant_id=? ORDER BY id", (_rid(),)).fetchall()
    # Build absence lookup for calendar rendering
    absence_lookup = {}
    for _at_row in absence_types:
        absence_lookup[_at_row["code"]] = {"nom": _at_row["nom"], "couleur": _at_row["couleur"], "code": _at_row["code"]}

    # Enrich planning entries with absence type colors
    for eid_map in planning_map.values():
        for d_list in eid_map.values():
            for entry in d_list:
                hd = entry.get("heure_debut", "")
                if hd in absence_lookup and hd not in ("R", "CP"):
                    ainfo = absence_lookup[hd]
                    if not entry.get("shift_nom"):
                        entry["shift_nom"] = ainfo["nom"]
                    if not entry.get("shift_couleur"):
                        entry["shift_couleur"] = ainfo["couleur"]

    return render_template("base.html", page="planning",
        jours=jours, employes=employes, planning_map=planning_map,
        totaux_emp=totaux_emp, totaux_jour=totaux_jour,
        week_offset=week_offset, semaine_label=semaine_label,
        recap_emp=recap_emp, solde_mois_map={r["id"]:r["solde_mois"] for r in recap_emp},
        solde_global_map={r["id"]:r["solde_total"] for r in recap_emp},
        mois_ref_label=_mois_label(mois_ref),
        postes_grouped=postes, poles_ordered=poles_ordered, conges_map=conges_map,
        absence_types=[dict(a) for a in absence_types],
        shifts_list=db.execute("SELECT * FROM shifts WHERE restaurant_id=? ORDER BY pole, nom", (_rid(),)).fetchall(),
        vue=vue, month_offset=month_offset, month_label=month_label,
        cal_weeks=cal_weeks, month_creneaux=month_creneaux,
        month_emp_hours=month_emp_hours, nb_days=nb_days,
        fermeture_dates=fermeture_dates, month_birthdays=month_birthdays,
        is_staff=is_staff)


_ABSENCE_COULEUR = "#718096"
_DEFAULT_ABSENCES = [
    ("CP",  "Congés payés",         _ABSENCE_COULEUR, 1),
    ("RH",  "Repos hebdomadaire",   _ABSENCE_COULEUR, 0),
    ("AI",  "Absence injustifiée",  _ABSENCE_COULEUR, 0),
    ("AM",  "Arrêt maladie",        _ABSENCE_COULEUR, 1),
    ("AT",  "Accident du travail",  _ABSENCE_COULEUR, 1),
    ("RTT", "RTT",                  _ABSENCE_COULEUR, 1),
    ("CSS", "Congé sans solde",     _ABSENCE_COULEUR, 0),
]


def _ensure_default_absences(rid):
    """Garantit la présence des absences par défaut pour un restaurant.
    Crée celles qui manquent et force la couleur grise sur toutes les existantes."""
    if not rid:
        return
    db = get_db()
    existing_codes = {r["code"] for r in db.execute(
        "SELECT code FROM absence_types WHERE restaurant_id=?", (rid,)
    ).fetchall()}
    for code, nom, couleur, rem in _DEFAULT_ABSENCES:
        if code not in existing_codes:
            try:
                db.execute(
                    "INSERT INTO absence_types (restaurant_id, code, nom, couleur, remuneree) VALUES (?,?,?,?,?)",
                    (rid, code, nom, couleur, rem)
                )
            except Exception as e:
                print(f"[ABSENCES SEED] {rid}/{code}: {e}")
    # Force la couleur grise unique sur toutes les absences du restaurant
    db.execute("UPDATE absence_types SET couleur=? WHERE restaurant_id=?", (_ABSENCE_COULEUR, rid))
    db.commit()


@app.route("/rh/shifts", methods=["GET", "POST"])
def rh_shifts():
    db = get_db()
    _ensure_default_absences(_rid())
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "add":
            pole = request.form.get("pole", "Salle")
            couleur = request.form.get("couleur", "#2D6A4A")
            sync.write_shift(db, request.form.get("nom", ""), pole,
                             request.form.get("heure_debut", ""), request.form.get("heure_fin", ""),
                             couleur)
        elif action == "update":
            sid = int(request.form.get("id", 0) or 0)
            db.execute("""UPDATE shifts SET nom=?, heure_debut=?, heure_fin=?, couleur=? WHERE id=?""",
                       (request.form.get("nom", ""), request.form.get("heure_debut", ""),
                        request.form.get("heure_fin", ""), request.form.get("couleur", "#2D6A4A"), sid))
            db.commit()
            sync.update_shift(db, sid)
        elif action == "delete":
            sync.delete_shift(db, int(request.form.get("id", 0) or 0))
        elif action == "add_absence":
            code = request.form.get("code", "").strip().upper()
            nom = request.form.get("nom", "").strip()
            couleur = request.form.get("couleur", "#6B7280")
            remuneree = 1 if request.form.get("remuneree") else 0
            if code and nom:
                rid = _rid()
                # Évite les doublons par (restaurant_id, code)
                exists = db.execute("SELECT id FROM absence_types WHERE restaurant_id=? AND code=?", (rid, code)).fetchone()
                if not exists:
                    db.execute("INSERT INTO absence_types (restaurant_id, code, nom, couleur, remuneree) VALUES (?,?,?,?,?)",
                               (rid, code, nom, couleur, remuneree))
        elif action == "delete_absence":
            aid = int(request.form.get("id", 0) or 0)
            # Don't delete built-in R and CP
            row = db.execute("SELECT code FROM absence_types WHERE restaurant_id=? AND id=?", (_rid(), aid,)).fetchone()
            if row and row["code"] not in ("R", "CP"):
                db.execute("DELETE FROM absence_types WHERE restaurant_id=? AND id=?", (_rid(), aid))
        db.commit()
        return redirect(url_for("rh_shifts"))
    shifts = db.execute("SELECT * FROM shifts WHERE restaurant_id=? ORDER BY pole, nom", (_rid(),)).fetchall()
    shifts_grouped = {"Salle": [], "Bar": [], "Cuisine": []}
    for s in shifts:
        p = s["pole"]
        if p not in shifts_grouped:
            shifts_grouped[p] = []
        shifts_grouped[p].append(dict(s))
    absence_types = db.execute("SELECT * FROM absence_types WHERE restaurant_id=? ORDER BY id", (_rid(),)).fetchall()
    return render_template("base.html", page="shifts", shifts_grouped=shifts_grouped,
                           absence_types=[dict(a) for a in absence_types])


@app.route("/rh/shifts/quick-add", methods=["POST"])
def rh_shifts_quick_add():
    """Create a shift on the fly from the planning modal (JSON API)."""
    db = get_db()
    data = request.get_json()
    nom = (data.get("nom") or "").strip()
    pole = data.get("pole", "Salle")
    heure_debut = data.get("heure_debut", "")
    heure_fin = data.get("heure_fin", "")
    couleur = data.get("couleur", "#2D6A4A")
    if not nom:
        return jsonify({"ok": False, "error": "Nom requis"}), 400
    sync.write_shift(db, nom, pole, heure_debut, heure_fin, couleur)
    row = db.execute("SELECT * FROM shifts WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (_rid(),)).fetchone()
    return jsonify({"ok": True, "shift": dict(row)})


@app.route("/api/planning/dupliquer", methods=["POST"])
@login_required
def api_planning_dupliquer():
    """Duplique les shifts d'une semaine source vers une ou plusieurs semaines cibles."""
    data = request.get_json() or {}
    src_lundi = (data.get("semaine_source") or "").strip()
    targets = data.get("semaines_cibles") or []
    ecraser = bool(data.get("ecraser"))
    if not src_lundi or not isinstance(targets, list) or not targets:
        return jsonify({"ok": False, "error": "semaine_source et semaines_cibles requis"}), 400
    try:
        src_d = datetime.strptime(src_lundi, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"ok": False, "error": "format date invalide"}), 400
    src_fin = src_d + timedelta(days=6)
    db = get_db()
    rid = _rid()
    src_rows = db.execute(
        "SELECT * FROM planning WHERE restaurant_id=? AND date >= ? AND date <= ? ORDER BY date",
        (rid, src_d.isoformat(), src_fin.isoformat())
    ).fetchall()
    if not src_rows:
        return jsonify({"ok": False, "error": "Aucun shift sur la semaine source"}), 400

    total_crees = 0
    semaines_ok = []
    for tgt_lundi in targets:
        try:
            tgt_d = datetime.strptime(tgt_lundi, "%Y-%m-%d").date()
        except Exception:
            continue
        tgt_fin = tgt_d + timedelta(days=6)
        delta_days = (tgt_d - src_d).days

        # Vérifie les conflits éventuels
        existing = db.execute(
            "SELECT id FROM planning WHERE restaurant_id=? AND date >= ? AND date <= ?",
            (rid, tgt_d.isoformat(), tgt_fin.isoformat())
        ).fetchall()
        if existing and not ecraser:
            # Skip cette semaine cible — il y a déjà des shifts et l'utilisateur n'a pas demandé l'écrasement
            continue
        if existing and ecraser:
            for ex in existing:
                try:
                    ex_row = db.execute("SELECT * FROM planning WHERE id=?", (ex["id"],)).fetchone()
                    if ex_row:
                        sync.delete_planning(db, ex["id"]) if hasattr(sync, "delete_planning") else db.execute("DELETE FROM planning WHERE id=?", (ex["id"],))
                except Exception as e:
                    print(f"[DUP-PLANNING] delete existing {ex['id']}: {e}")
            db.commit()

        # Duplication des shifts
        for src in src_rows:
            try:
                src_date = datetime.strptime(src["date"], "%Y-%m-%d").date()
                new_date = (src_date + timedelta(days=delta_days)).isoformat()
                sync.write_planning(
                    db,
                    src["employe_id"],
                    new_date,
                    src["heure_debut"] or "",
                    src["heure_fin"] or "",
                    src["poste"] or "",
                    shift_id=src["shift_id"] if "shift_id" in src.keys() else 0,
                    shift_nom=src["shift_nom"] if "shift_nom" in src.keys() else "",
                    shift_couleur=src["shift_couleur"] if "shift_couleur" in src.keys() else "",
                    repas=bool(src["repas"]) if "repas" in src.keys() else False,
                    duree_repas=int(src["duree_repas"] or 0) if "duree_repas" in src.keys() else 0,
                )
                total_crees += 1
            except Exception as e:
                print(f"[DUP-PLANNING] insert {src['id']}→{tgt_lundi}: {e}")
        semaines_ok.append(tgt_lundi)

    return jsonify({"ok": True, "shifts_crees": total_crees, "semaines": semaines_ok})


@app.route("/api/planning/reorder-employes", methods=["POST"])
@login_required
def api_planning_reorder_employes():
    """Met à jour l'ordre des employés d'un pôle (drag & drop).
    Payload: {pole: 'Salle', ordre: [emp_id_local, ...]}
    Sync local SQLite + Airtable Ordre."""
    data = request.get_json() or {}
    ids = data.get("ordre") or []
    pole = (data.get("pole") or "").strip()
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "ordre requis"}), 400
    db = get_db()
    rid = _rid()
    updated = 0
    for idx, emp_id in enumerate(ids, start=1):
        try:
            row = db.execute("SELECT prenom, nom FROM employes WHERE restaurant_id=? AND id=?", (rid, int(emp_id))).fetchone()
            if not row:
                continue
            db.execute("UPDATE employes SET ordre=? WHERE restaurant_id=? AND id=?", (idx, rid, int(emp_id)))
            updated += 1
            # Sync Airtable Ordre
            if rid and not getattr(current_user, "demo_mode", False):
                try:
                    prenom_esc = (row["prenom"] or "").replace("'", "\\'")
                    nom_esc = (row["nom"] or "").replace("'", "\\'")
                    at_rec = at.find_first_nocache("employes",
                        f"AND({{Restaurant_ID}}='{rid}',{{Prénom}}='{prenom_esc}',{{Nom}}='{nom_esc}')")
                    if at_rec:
                        at.update("employes", at_rec["id"], {"Ordre": idx})
                except Exception as e:
                    print(f"[PLANNING-REORDER] Airtable sync {emp_id}: {e}")
        except Exception as e:
            print(f"[PLANNING-REORDER] {emp_id}: {e}")
    db.commit()
    try:
        at.invalidate_cache("employes", rid)
    except Exception:
        pass
    return jsonify({"ok": True, "updated": updated, "pole": pole})


@app.route("/rh/planning/reorder/<int:emp_id>/<direction>")
def planning_reorder(emp_id, direction):
    db = get_db()
    week = request.args.get("week", "0")
    emp = db.execute("SELECT * FROM employes WHERE restaurant_id=? AND id=?", (_rid(), emp_id,)).fetchone()
    if not emp:
        return redirect(url_for("rh_planning", week=week))
    current_ordre = emp["ordre"] if emp["ordre"] else 0
    if direction == "up":
        new_ordre = current_ordre - 1
    else:
        new_ordre = current_ordre + 1
    db.execute("UPDATE employes SET ordre=? WHERE id=?", (new_ordre, emp_id))
    db.commit()
    return redirect(url_for("rh_planning", week=week))


def _staff_403():
    """Return 403 response if current user is Staff."""
    if getattr(current_user, "role", "") == "Staff":
        return render_template("base.html", page="403"), 403
    return None


def _delete_planning_from_airtable(db, rid, row):
    """Delete a planning entry from Airtable matching the SQLite row data."""
    try:
        emp = db.execute("SELECT prenom, nom FROM employes WHERE id=?", (row["employe_id"],)).fetchone()
        if not emp:
            print(f"[PLANNING-DEL] Employee {row['employe_id']} not found in SQLite")
            return
        print(f"[PLANNING-DEL] looking up rid={rid} emp={emp['prenom']} {emp['nom']} date={row['date']} hd={row['heure_debut']!r} hf={row['heure_fin']!r}")
        at.invalidate_cache("employes")
        emp_at_list = at.get_all("employes", restaurant_id=rid)
        at_emp_id = None
        for e in emp_at_list:
            if e.get("Prénom", e.get("Prenom", "")) == emp["prenom"] and e.get("Nom", "") == emp["nom"]:
                at_emp_id = e["id"]
                break
        if not at_emp_id:
            print(f"[PLANNING-DEL] Employee {emp['prenom']} {emp['nom']} not found in Airtable")
            return
        print(f"[PLANNING-DEL] Airtable Employé_ID={at_emp_id}")
        at.invalidate_cache("planning")
        # Match robuste : DATETIME_FORMAT pour le champ Date (Airtable stocke ISO
        # mais le filtre formula compare la valeur affichée → format européen
        # par défaut sur cette table). On force YYYY-MM-DD côté formule.
        date_iso = row["date"]
        # 1) Match exact (employé + date + heures)
        entries = at.get_all("planning", restaurant_id=rid,
            formula=f"AND({{Employé_ID}}='{at_emp_id}',DATETIME_FORMAT({{Date}},'YYYY-MM-DD')='{date_iso}',{{Shift_début}}='{row['heure_debut']}',{{Shift_fin}}='{row['heure_fin']}')")
        print(f"[PLANNING-DEL] exact match → {len(entries)} entries")
        if not entries:
            entries = at.get_all("planning", restaurant_id=rid,
                formula=f"AND({{Employé_ID}}='{at_emp_id}',DATETIME_FORMAT({{Date}},'YYYY-MM-DD')='{date_iso}')")
            print(f"[PLANNING-DEL] fallback emp+date → {len(entries)} entries")
        for ent in entries:
            try:
                at.delete("planning", ent["id"])
                print(f"[PLANNING-DEL] Deleted Airtable record {ent['id']}")
            except Exception as de:
                print(f"[PLANNING-DEL] delete error on {ent['id']}: {de}")
        at.invalidate_cache("planning")
        if not entries:
            print(f"[PLANNING-DEL] No Airtable match for {emp['prenom']} on {date_iso}")
    except Exception as e:
        print(f"[PLANNING-DEL] Error: {e}")
        import traceback
        traceback.print_exc()


@app.route("/rh/planning/add", methods=["POST"])
def planning_add():
    blocked = _staff_403()
    if blocked:
        return blocked
    db = get_db()
    week = request.form.get("week", "0")
    # Safe parsing — default values for optional fields
    try:
        shift_id = int(request.form.get("shift_id") or 0)
    except (ValueError, TypeError):
        shift_id = 0
    try:
        emp_id = int(request.form.get("employe_id") or 0)
    except (ValueError, TypeError):
        return redirect(url_for("rh_planning", week=week))
    slot_date = request.form.get("date", "")
    heure_debut = request.form.get("heure_debut", "09:00")
    heure_fin = request.form.get("heure_fin", "17:00")
    if not emp_id or not slot_date:
        return redirect(url_for("rh_planning", week=week))
    # Poste hérité de la fiche employé (plus dans le modal)
    emp_row = db.execute("SELECT poste FROM employes WHERE restaurant_id=? AND id=?", (_rid(), emp_id)).fetchone()
    poste = (emp_row["poste"] if emp_row and emp_row["poste"] else "Salle")
    shift_nom = ""
    shift_couleur = ""
    if shift_id:
        sh = db.execute("SELECT nom, couleur FROM shifts WHERE restaurant_id=? AND id=?", (_rid(), shift_id,)).fetchone()
        if sh:
            shift_nom = sh["nom"] or ""
            shift_couleur = sh["couleur"] or ""
    # If editing an existing slot, delete old from SQLite + Airtable
    slot_id = request.form.get("slot_id", "")
    if slot_id:
        try:
            old_row = db.execute("SELECT employe_id, date, heure_debut, heure_fin FROM planning WHERE id=?", (int(slot_id),)).fetchone()
            db.execute("DELETE FROM planning WHERE id=?", (int(slot_id),))
            rid = _rid()
            if old_row and rid and not getattr(current_user, "demo_mode", False):
                _delete_planning_from_airtable(db, rid, old_row)
        except (ValueError, TypeError):
            pass
    repas = 1 if request.form.get("repas") in ("on", "1", "true") else 0
    try:
        duree_repas = int(request.form.get("duree_repas", 30) or 30)
    except (ValueError, TypeError):
        duree_repas = 30
    if not repas:
        duree_repas = 0
    try:
        sync.write_planning(db, emp_id, slot_date, heure_debut, heure_fin,
                             poste, shift_id, shift_nom, shift_couleur,
                             repas=bool(repas), duree_repas=duree_repas)
    except Exception as e:
        print(f"[PLANNING-ADD] error: {e}")
        import traceback
        traceback.print_exc()
    return redirect(url_for("rh_planning", week=week))


@app.route("/rh/planning/delete/<int:id>")
def planning_delete(id):
    blocked = _staff_403()
    if blocked:
        return blocked
    db = get_db()
    week = request.args.get("week", "0")
    row = db.execute("SELECT employe_id, date, heure_debut, heure_fin FROM planning WHERE id=?", (id,)).fetchone()
    db.execute("DELETE FROM planning WHERE id=?", (id,))
    db.commit()
    rid = _rid()
    if row and rid and not getattr(current_user, "demo_mode", False):
        _delete_planning_from_airtable(db, rid, row)
    return redirect(url_for("rh_planning", week=week))


@app.route("/rh/planning/repos", methods=["POST"])
def planning_repos():
    blocked = _staff_403()
    if blocked:
        return blocked
    db = get_db()
    week = request.form.get("week", "0")
    eid = int(request.form.get("employe_id", 0) or 0)
    d = request.form.get("date", "")
    print(f"[PLANNING-REPOS] reçu form: eid={eid} date={d!r} rid={_rid()}")
    db.execute("DELETE FROM planning WHERE restaurant_id=? AND employe_id=? AND date=?", (_rid(), eid, d))
    db.commit()
    sync.write_planning(db, eid, d, "R", "R", "Repos", 0, "Repos", "#9CA3AF")
    return redirect(url_for("rh_planning", week=week))


@app.route("/rh/planning/cp", methods=["POST"])
def planning_cp():
    blocked = _staff_403()
    if blocked:
        return blocked
    db = get_db()
    week = request.form.get("week", "0")
    eid = int(request.form.get("employe_id", 0) or 0)
    d = request.form.get("date", "")
    db.execute("DELETE FROM planning WHERE restaurant_id=? AND employe_id=? AND date=?", (_rid(), eid, d))
    db.commit()
    sync.write_planning(db, eid, d, "CP", "CP", "CP", 0, "Conges payes", "#9CA3AF")
    sync.write_conge(db, eid, "Conges payes", d, d, "CP pose depuis planning")
    return redirect(url_for("rh_planning", week=week))


@app.route("/rh/planning/absence", methods=["POST"])
def planning_absence():
    blocked = _staff_403()
    if blocked:
        return blocked
    db = get_db()
    week = request.form.get("week", "0")
    eid = int(request.form.get("employe_id", 0) or 0)
    d = request.form.get("date", "")
    code = request.form.get("absence_code", "R")
    print(f"[PLANNING-ABSENCE] reçu form: eid={eid} date={d!r} code={code!r} rid={_rid()}")
    # Lookup absence type — fallback codé en dur si la table n'a pas l'entrée
    atype = db.execute("SELECT * FROM absence_types WHERE restaurant_id=? AND code=?", (_rid(), code,)).fetchone()
    DEFAULT_ABS = {
        "R":  {"nom": "Repos",                "couleur": "#9CA3AF"},
        "CP": {"nom": "Conges payes",         "couleur": "#3B82F6"},
        "AM": {"nom": "Arret maladie",        "couleur": "#F59E0B"},
        "AI": {"nom": "Absence injustifiee",  "couleur": "#EF4444"},
        "FOR":{"nom": "Formation",            "couleur": "#10B981"},
        "EF": {"nom": "Evenement familial",   "couleur": "#8B5CF6"},
    }
    if atype:
        nom_abs = atype["nom"]
        couleur_abs = atype["couleur"]
    else:
        fb = DEFAULT_ABS.get(code, {"nom": code, "couleur": "#9CA3AF"})
        nom_abs = fb["nom"]
        couleur_abs = fb["couleur"]
        print(f"[PLANNING-ABSENCE] fallback utilisé pour code={code!r} (absence_types vide)")
    db.execute("DELETE FROM planning WHERE restaurant_id=? AND employe_id=? AND date=?", (_rid(), eid, d))
    db.commit()
    sync.write_planning(db, eid, d, code, code, nom_abs, 0, nom_abs, couleur_abs)
    if code == "CP":
        sync.write_conge(db, eid, "Conges payes", d, d, "CP pose depuis planning")
    return redirect(url_for("rh_planning", week=week))


@app.route("/rh/conges", methods=["GET", "POST"])
def rh_conges():
    # Congés intégrés dans Équipe et Planning — redirection
    if request.method == "POST":
        db = get_db()
        action = request.form.get("action", "add")
        if action == "add":
            sync.write_conge(db, int(request.form.get("employe_id", 0) or 0), request.form.get("type_conge", ""),
                             request.form.get("date_debut", ""), request.form.get("date_fin", ""),
                             request.form.get("commentaire", ""))
        elif action == "delete":
            sync.delete_conge(db, int(request.form.get("id", 0) or 0))
        db.commit()
    return redirect(url_for("rh_effectifs"))
    # Compteur congés payés — période 1er juin au 31 mai, 2.5j/mois travaillé
    today = date.today()
    # Période de référence : 1er juin N-1 au 31 mai N (ou en cours)
    if today.month >= 6:
        debut_periode = date(today.year, 6, 1)
    else:
        debut_periode = date(today.year - 1, 6, 1)
    fin_periode = date(debut_periode.year + 1, 5, 31)

    soldes = []
    for emp in employes:
        # Date effective = max(date embauche, début période)
        try:
            date_embauche = date.fromisoformat(emp["date_debut"])
        except Exception:
            date_embauche = debut_periode
        date_eff = max(date_embauche, debut_periode)
        # Mois travaillés depuis date effective jusqu'à aujourd'hui
        mois_travailles = (today.year - date_eff.year) * 12 + today.month - date_eff.month
        if today.day >= date_eff.day:
            mois_travailles += 1
        mois_travailles = max(0, min(mois_travailles, 12))
        acquis = round(mois_travailles * 2.5, 1)
        # Jours pris sur la période
        pris = db.execute("""SELECT SUM(julianday(date_fin) - julianday(date_debut) + 1) as j
                             FROM conges WHERE restaurant_id=? AND employe_id=? AND (type='Congés payés' OR type='Conges payes')
                             AND date_debut >= ? AND date_fin <= ?""",
                          (_rid(), emp["id"], debut_periode.isoformat(), fin_periode.isoformat())).fetchone()
        jours_pris = round(float(pris["j"] or 0), 1)
        soldes.append({"prenom": emp["prenom"], "nom": emp["nom"], "acquis": acquis, "pris": jours_pris, "restant": round(acquis - jours_pris, 1)})
    return render_template("base.html", page="conges", employes=employes, conges_list=[dict(c) for c in conges], soldes_conges=soldes)


# ---------------------------------------------------------------------------
#  Ressources
# ---------------------------------------------------------------------------

@app.route("/ressources/gestion")
def res_gestion():
    return render_template("base.html", page="res_gestion")

@app.route("/ressources/rh")
def res_rh():
    return render_template("base.html", page="res_rh")

@app.route("/ressources/cuisine")
def res_cuisine():
    return render_template("base.html", page="res_cuisine")

@app.route("/ressources/e-reputation")
def res_ereputation():
    return render_template("base.html", page="res_ereputation")

@app.route("/ressources/juridique")
def res_juridique():
    return render_template("base.html", page="res_juridique")

@app.route("/ressources/calendrier")
def res_calendrier():
    db = get_db()
    all_evts = db.execute("SELECT * FROM evenements WHERE restaurant_id=? ORDER BY date", (_rid(),)).fetchall()
    evts_list = [{"id": r["id"], "titre": r["titre"], "date": r["date"],
                  "couleur": r["couleur"] if "couleur" in r.keys() else "#2D6A4A",
                  "note": r["note"] if "note" in r.keys() else "",
                  "type_evt": r["type_evt"] if "type_evt" in r.keys() else ""} for r in all_evts]
    # Anniversaires des employés actifs (année courante + suivante)
    all_emps = db.execute("SELECT prenom, nom, date_naissance FROM employes WHERE restaurant_id=? AND statut='Actif'", (_rid(),)).fetchall()
    today = date.today()
    for emp in all_emps:
        dn = emp["date_naissance"] if "date_naissance" in emp.keys() else ""
        if not dn or len(dn) < 10:
            continue
        try:
            b_month, b_day = int(dn[5:7]), int(dn[8:10])
            b_year = int(dn[:4])
        except (ValueError, IndexError):
            continue
        for yr in (today.year, today.year + 1):
            try:
                d = date(yr, b_month, b_day)
            except ValueError:
                continue
            age = yr - b_year
            evts_list.append({
                "id": 0, "titre": f"\U0001f382 Anniversaire de {emp['prenom']} ({age} ans)",
                "date": d.isoformat(), "couleur": "#8B5CF6", "note": "",
                "type_evt": "Anniversaire \U0001f382",
            })
    evts_list.sort(key=lambda x: x.get("date", ""))
    all_ferm = db.execute("SELECT * FROM fermetures WHERE restaurant_id=? ORDER BY id", (_rid(),)).fetchall()
    ferm_list = [{"id": r["id"], "date": r["date"] or "", "recurrence": r["recurrence"],
                  "jour_semaine": r["jour_semaine"]} for r in all_ferm]
    # Load event types from Airtable types_evenements table
    custom_types = _load_event_types(_rid())
    # Harmoniser couleur anniversaires avec le type "Anniversaire" configuré
    anniv_color = "#F59E0B"  # fallback
    for ct in custom_types:
        if ct["nom"].lower() == "anniversaire":
            anniv_color = ct["couleur"]
            break
    for evt in evts_list:
        if evt.get("id") == 0 and "Anniversaire" in evt.get("type_evt", ""):
            evt["couleur"] = anniv_color
    return render_template("base.html", page="res_calendrier", cal_evenements=evts_list,
                           cal_fermetures=ferm_list, cal_custom_types=custom_types)


# ---------------------------------------------------------------------------
#  Types d'événements — Airtable table types_evenements
# ---------------------------------------------------------------------------

_DEFAULT_EVENT_TYPES = [
    ("Groupe", "#2D6A4A"), ("Mariage", "#EC4899"),
    ("Anniversaire", "#F59E0B"), ("Seminaire", "#1D4ED8"),
]


def _load_event_types(rid):
    """Load event types from Airtable. Seed defaults if empty."""
    try:
        rows = at.get_all("types_evenements", restaurant_id=rid)
        if rows:
            return [{"id": r["id"], "nom": r.get("Nom", ""), "couleur": r.get("Couleur", "#6B7280")} for r in rows]
    except Exception as e:
        print(f"[EVT-TYPES] Load error: {e}")
    # Seed defaults
    try:
        seed_default_event_types(rid)
        rows = at.get_all("types_evenements", restaurant_id=rid)
        return [{"id": r["id"], "nom": r.get("Nom", ""), "couleur": r.get("Couleur", "#6B7280")} for r in rows]
    except Exception as e:
        print(f"[EVT-TYPES] Seed error: {e}")
    return []


def seed_default_event_types(rid):
    """Insert default event types in Airtable for a new restaurant."""
    for nom, couleur in _DEFAULT_EVENT_TYPES:
        at.create("types_evenements", {"Restaurant_ID": rid, "Nom": nom, "Couleur": couleur})
    at.invalidate_cache("types_evenements")


@app.route("/event-types/add", methods=["POST"])
@login_required
def event_type_add():
    if current_user.role not in ("Gerant", "Manager"):
        return redirect(url_for("res_calendrier"))
    nom = request.form.get("nom", "").strip()
    couleur = request.form.get("couleur", "#6B7280")
    if nom:
        try:
            at.create("types_evenements", {"Restaurant_ID": _rid(), "Nom": nom, "Couleur": couleur})
            at.invalidate_cache("types_evenements")
        except Exception as e:
            print(f"[EVT-TYPES] Add error: {e}")
    return redirect(url_for("res_calendrier"))


@app.route("/api/calendrier/types/<path:record_id>", methods=["PUT", "POST"])
@login_required
def event_type_update(record_id):
    if current_user.role not in ("Gerant", "Manager"):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or request.form
    nom = (data.get("nom") or "").strip()
    couleur = (data.get("couleur") or "").strip() or "#6B7280"
    if not nom:
        return jsonify({"ok": False, "error": "nom requis"}), 400
    # Récupère l'ancien nom AVANT update + vérifie l'appartenance au restaurant courant
    old_nom = ""
    try:
        old = at.get_one("types_evenements", record_id)
    except Exception as e:
        print(f"[EVT-TYPES] Read old error: {e}")
        old = None
    if not old or (old.get("Restaurant_ID") or "") != _rid():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    old_nom = (old.get("Nom") or "").strip()
    try:
        at.update("types_evenements", record_id, {"Nom": nom, "Couleur": couleur})
        at.invalidate_cache("types_evenements")
    except Exception as e:
        print(f"[EVT-TYPES] Update error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    # Propagation rename : met à jour type_evt sur tous les events locaux du restaurant
    if old_nom and old_nom != nom:
        try:
            db = get_db()
            db.execute("UPDATE evenements SET type_evt=? WHERE restaurant_id=? AND type_evt=?",
                       (nom, _rid(), old_nom))
            db.commit()
        except Exception as e:
            print(f"[EVT-TYPES] Local rename propagation error: {e}")
    return jsonify({"ok": True, "id": record_id, "nom": nom, "couleur": couleur})


@app.route("/event-types/delete/<path:record_id>")
@login_required
def event_type_delete(record_id):
    if current_user.role not in ("Gerant", "Manager"):
        return redirect(url_for("res_calendrier"))
    # Vérifie l'appartenance avant delete
    try:
        rec = at.get_one("types_evenements", record_id)
    except Exception:
        rec = None
    if not rec or (rec.get("Restaurant_ID") or "") != _rid():
        return redirect(url_for("res_calendrier"))
    try:
        at.delete("types_evenements", record_id)
        at.invalidate_cache("types_evenements")
    except Exception as e:
        print(f"[EVT-TYPES] Delete error: {e}")
    return redirect(url_for("res_calendrier"))


# ---------------------------------------------------------------------------
#  Export PDF mensuel (print CSS via navigateur)
# ---------------------------------------------------------------------------

@app.route("/export-pdf")
def export_pdf():
    mois = request.args.get("mois", _mois_courant())
    db = get_db()
    stats = _stats_mois(db, mois)
    dep = _depenses_mois(db, mois)
    total_depenses = dep["total"]
    ca = stats["ca_total"]

    food_cost = round(dep["food"] / ca * 100, 1) if ca > 0 else 0
    beverage_cost = round(dep["beverage"] / ca * 100, 1) if ca > 0 else 0
    labour_pct = round(dep["personnel"] / ca * 100, 1) if ca > 0 else 0
    prime_cost = round((dep["food"] + dep["beverage"] + dep["personnel"]) / ca * 100, 1) if ca > 0 else 0
    resultat_net = round((ca - total_depenses) / ca * 100, 1) if ca > 0 else 0

    # Depenses par categorie
    fixes = db.execute("SELECT categorie, SUM(montant) as total FROM depenses_fixes WHERE restaurant_id=? AND mois = ? GROUP BY categorie ORDER BY total DESC", (_rid(), mois,)).fetchall()
    variables = db.execute("SELECT categorie, SUM(montant) as total FROM depenses_variables WHERE restaurant_id=? AND mois = ? GROUP BY categorie ORDER BY total DESC", (_rid(), mois,)).fetchall()

    # Mois precedent
    try:
        y, m = int(mois.split("-")[0]), int(mois.split("-")[1])
        pm = m - 1
        py = y
        if pm <= 0:
            pm = 12
            py -= 1
        mois_prec = f"{py:04d}-{pm:02d}"
    except Exception:
        mois_prec = mois
    stats_prec = _stats_mois(db, mois_prec)
    dep_prec = _depenses_mois(db, mois_prec)

    return render_template("export_pdf.html",
        mois_label=_mois_label(mois),
        stats=stats, dep=dep, total_depenses=total_depenses,
        food_cost=food_cost, beverage_cost=beverage_cost, labour_pct=labour_pct,
        prime_cost=prime_cost, resultat_net=resultat_net,
        fixes_cat=fixes, variables_cat=variables,
        stats_prec=stats_prec, dep_prec=dep_prec,
        mois_prec_label=_mois_label(mois_prec),
        date_generation=datetime.now().strftime("%d/%m/%Y %H:%M"))


# ---------------------------------------------------------------------------
#  Alertes ratios
# ---------------------------------------------------------------------------

_RATIO_NORMS = {
    "food_cost": {"min": 25, "max": 35, "label": "Cout matieres", "unit": "%"},
    "beverage_cost": {"min": 15, "max": 25, "label": "Cout boissons", "unit": "%"},
    "labour_cost": {"min": 25, "max": 40, "label": "Cout personnel", "unit": "%"},
    "prime_cost": {"min": 55, "max": 70, "label": "Charges directes", "unit": "%"},
}


def _compute_alertes(db, mois):
    """Calcule les alertes si des ratios depassent les normes.
    - Au-dessus du max → danger (rouge)
    - En dessous du min → warning (orange) — pas forcement mauvais
    - Resultat net < 5% → danger, < 8% → warning
    """
    stats = _stats_mois(db, mois)
    ca = stats["ca_total"]
    dep = _depenses_mois(db, mois)
    ratios = {
        "food_cost": round(dep["food"] / ca * 100, 1) if ca > 0 else 0,
        "beverage_cost": round(dep["beverage"] / ca * 100, 1) if ca > 0 else 0,
        "labour_cost": round(dep["personnel"] / ca * 100, 1) if ca > 0 else 0,
        "prime_cost": round((dep["food"] + dep["beverage"] + dep["personnel"]) / ca * 100, 1) if ca > 0 else 0,
    }
    resultat_net = round((ca - dep["total"]) / ca * 100, 1) if ca > 0 else 0
    alerts = []
    for key, norm in _RATIO_NORMS.items():
        val = ratios[key]
        if val > 0 and val > norm["max"]:
            alerts.append({"label": norm["label"], "value": val, "norm": f"{norm['min']}-{norm['max']}%", "level": "danger"})
        elif val > 0 and val < norm["min"]:
            alerts.append({"label": norm["label"], "value": val, "norm": f"{norm['min']}-{norm['max']}%", "level": "warning"})
    # Resultat net alerts
    if resultat_net < 5:
        alerts.append({"label": "Resultat net", "value": resultat_net, "norm": "> 8%", "level": "danger"})
    elif resultat_net < 8:
        alerts.append({"label": "Resultat net", "value": resultat_net, "norm": "> 8%", "level": "warning"})
    return {"alerts": alerts, "all_ok": len(alerts) == 0, "has_data": ca > 0,
            "food_cost": ratios["food_cost"], "labour_pct": ratios["labour_cost"],
            "prime_cost": ratios["prime_cost"], "resultat_net": resultat_net}


# ---------------------------------------------------------------------------
#  Meteo (Open-Meteo — gratuit, sans cle API)
# ---------------------------------------------------------------------------

_meteo_cache = {}  # keyed by ville: {"data": ..., "data_today": ..., "ts": ...}


def _get_ville_restaurant():
    """Retourne la ville du restaurant de l'utilisateur connecte."""
    try:
        if current_user and current_user.is_authenticated:
            resto = at.get_restaurant(current_user.restaurant_id)
            if resto:
                return resto.get("Ville", "") or ""
    except Exception:
        pass
    return ""


def _get_cache(ville):
    if ville not in _meteo_cache:
        _meteo_cache[ville] = {"data": None, "data_today": None, "ts": 0}
    return _meteo_cache[ville]


def _get_meteo(ville=None):
    ville = ville or _get_ville_restaurant()
    cache = _get_cache(ville)
    now = time.time()
    if cache["data"] and now - cache["ts"] < 1800:  # 30 min cache
        return cache["data"]
    from modules.meteo import get_meteo
    result = get_meteo(ville=ville) if ville else get_meteo()
    cache["data"] = result
    cache["ts"] = now
    return result


def _get_meteo_aujourdhui(ville=None):
    ville = ville or _get_ville_restaurant()
    cache = _get_cache(ville)
    now = time.time()
    if cache["data_today"] and now - cache["ts"] < 1800:
        return cache["data_today"]
    try:
        from modules.meteo import geocode_ville, _WMO_CODES, _WMO_ICONS
        import requests as _rq
        lat, lng = 48.8566, 2.3522
        if ville:
            coords = geocode_ville(ville)
            if coords:
                lat, lng = coords
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lng}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode&timezone=Europe/Paris&forecast_days=2"
        r = _rq.get(url, timeout=5)
        if r.status_code == 200:
            daily = r.json().get("daily", {})
            if daily and len(daily.get("time", [])) >= 1:
                code = daily["weathercode"][0]
                result = {
                    "temp_max": round(daily["temperature_2m_max"][0]),
                    "temp_min": round(daily["temperature_2m_min"][0]),
                    "precipitation": round(daily["precipitation_sum"][0], 1),
                    "description": _WMO_CODES.get(code, ""),
                    "icon": _WMO_ICONS.get(code, ""),
                }
                cache["data_today"] = result
                cache["ts"] = now
                return result
    except Exception:
        pass
    return None


@app.route("/api/meteo")
@login_required
def api_meteo():
    ville = _get_ville_restaurant()
    return jsonify({
        "demain": _get_meteo(ville),
        "aujourdhui": _get_meteo_aujourdhui(ville)
    })


# ---------------------------------------------------------------------------
#  Evenements
# ---------------------------------------------------------------------------

@app.route("/evenements/add", methods=["POST"])
def evenement_add():
    db = get_db()
    titre = request.form.get("titre", "").strip()
    evt_date = request.form.get("date", "")
    note = request.form.get("note", "")
    type_evt = request.form.get("type_evt", "")
    # Couleur déduite du type (jamais saisie manuellement) — gris neutre si aucun type
    couleur = "#6B7280"
    if type_evt:
        for ct in _load_event_types(_rid()):
            if ct.get("nom") == type_evt:
                couleur = ct.get("couleur") or "#6B7280"
                break
    redirect_to = request.form.get("redirect", "accueil")
    # Delete old event if editing
    delete_id = request.form.get("delete_id", "")
    if delete_id:
        sync.delete_evenement(db, int(delete_id))
    if titre and evt_date:
        sync.write_evenement(db, titre, evt_date, couleur, note)
        # Update type_evt if provided
        if type_evt:
            last_id = db.execute("SELECT id FROM evenements WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (_rid(),)).fetchone()
            if last_id:
                db.execute("UPDATE evenements SET type_evt=? WHERE id=?", (type_evt, last_id["id"]))
    if redirect_to == "calendrier":
        return redirect(url_for("res_calendrier"))
    return redirect(url_for("accueil"))


@app.route("/evenements/delete/<int:id>")
def evenement_delete(id):
    db = get_db()
    redirect_to = request.args.get("redirect", "accueil")
    sync.delete_evenement(db, id)
    if redirect_to == "calendrier":
        return redirect(url_for("res_calendrier"))
    return redirect(url_for("accueil"))


@app.route("/fermetures/add", methods=["POST"])
def fermeture_add():
    db = get_db()
    ftype = request.form.get("type", "simple")
    if ftype == "recurrence":
        jour = int(request.form.get("jour_semaine", 0))
        sync.write_fermeture(db, True, jour)
    else:
        fdate = request.form.get("date", "")
        if fdate:
            sync.write_fermeture(db, False, date_str=fdate)
    return redirect(url_for("res_calendrier"))


@app.route("/fermetures/update/<int:id>", methods=["POST"])
@login_required
def fermeture_update(id):
    """Modifie une récurrence de fermeture (jour de la semaine, type, dates de validité)."""
    db = get_db()
    data = request.get_json() or {}
    rid = _rid()
    row = db.execute("SELECT * FROM fermetures WHERE restaurant_id=? AND id=?", (rid, id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "introuvable"}), 404
    try:
        new_jour = int(data.get("jour_semaine", row["jour_semaine"]))
    except Exception:
        new_jour = row["jour_semaine"]
    db.execute("UPDATE fermetures SET jour_semaine=? WHERE id=?", (new_jour, id))
    db.commit()
    # Sync Airtable : retrouver le record et mettre à jour Jour_semaine
    if rid and not sync._is_demo():
        try:
            existing = at.find_first_nocache("fermetures",
                f"AND({{Restaurant_ID}}='{rid}',{{Recurrence}}=1,{{Jour_semaine}}={row['jour_semaine']})")
            if existing:
                at.update("fermetures", existing["id"], {"Jour_semaine": new_jour})
                at.invalidate_cache("fermetures", rid)
        except Exception as e:
            print(f"[FERMETURE-UPDATE] {e}")
    return jsonify({"ok": True})


@app.route("/fermetures/delete/<int:id>")
def fermeture_delete(id):
    db = get_db()
    sync.delete_fermeture(db, id)
    return redirect(url_for("res_calendrier"))


@app.route("/fermetures/exception", methods=["POST"])
def fermeture_exception():
    """Crée une exception pour un jour de récurrence (ouvre ce jour uniquement)."""
    db = get_db()
    fdate = request.form.get("date", "").strip()
    if fdate:
        # jour_semaine=-2 = exception marker
        rid = sync._get_rid()
        db.execute("INSERT INTO fermetures (restaurant_id, date, recurrence, jour_semaine) VALUES (?, ?, 0, -2)", (rid, fdate,))
        db.commit()
        # Sync to Airtable
        if rid and not sync._is_demo():
            try:
                at.create_fermeture(rid, recurrence=False, jour_semaine=-2, date_str=fdate)
            except Exception:
                pass
    return redirect(url_for("res_calendrier"))


@app.route("/evenements/api")
def evenements_api():
    """JSON des evenements pour le calendrier."""
    db = get_db()
    mois = request.args.get("mois", "")
    if mois:
        rows = db.execute("SELECT * FROM evenements WHERE restaurant_id=? AND date LIKE ? ORDER BY date", (_rid(), mois + "%",)).fetchall()
    else:
        rows = db.execute("SELECT * FROM evenements WHERE restaurant_id=? ORDER BY date", (_rid(),)).fetchall()
    return jsonify([{"id": r["id"], "titre": r["titre"], "date": r["date"],
                     "couleur": r["couleur"] if "couleur" in r.keys() else "#2D6A4A",
                     "note": r["note"] if "note" in r.keys() else ""} for r in rows])


# ---------------------------------------------------------------------------
#  Messagerie
# ---------------------------------------------------------------------------

@app.route("/messagerie")
def messagerie():
    db = get_db()
    employes = db.execute("SELECT * FROM employes WHERE restaurant_id=? AND statut='Actif' ORDER BY poste, nom, prenom", (_rid(),)).fetchall()
    # Pôle = source de vérité, calculé depuis la table postes (alimentée par
    # le champ Pôle Airtable). Fallback _infer_pole sur le nom du poste si
    # le poste n'est pas dans la table.
    poste_pole_map = {p["nom"]: p["pole"] for p in db.execute("SELECT nom, pole FROM postes WHERE restaurant_id=?", (_rid(),)).fetchall()}
    emps = []
    for e in employes:
        d = dict(e)
        d["pole"] = poste_pole_map.get(d.get("poste") or "") or sync._infer_pole(d.get("poste") or "")
        emps.append(d)
    return render_template("base.html", page="messagerie", employes=emps)


@app.route("/messagerie/send", methods=["POST"])
def messagerie_send():
    db = get_db()
    destinataires = request.form.get("destinataires", "tous")
    objet = request.form.get("objet", "").strip()
    message = request.form.get("message", "").strip()
    if objet and message:
        sync.write_message(db, "Gerant", destinataires, objet, message, datetime.now().strftime("%d/%m/%Y %H:%M"))
    return redirect(url_for("messagerie"))


@app.route("/messagerie/read/<int:id>")
def messagerie_read(id):
    db = get_db()
    db.execute("UPDATE messages SET lu=1 WHERE id=?", (id,))
    db.commit()
    return redirect(url_for("messagerie"))


@app.route("/messagerie/delete/<int:id>")
def messagerie_delete(id):
    db = get_db()
    db.execute("DELETE FROM messages WHERE id=?", (id,))
    db.commit()
    return redirect(url_for("messagerie"))


# ---------------------------------------------------------------------------
#  Export Planning PDF
# ---------------------------------------------------------------------------

@app.route("/rh/planning/export-pdf")
@login_required
def planning_export_pdf():
    """Genere un PDF du planning de la semaine."""
    try:
        return _planning_export_pdf_impl()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[PLANNING-PDF OUTER] {e}\n{tb}")
        try:
            with open("errors.log", "a", encoding="utf-8") as _lf:
                _lf.write(f"[{datetime.now().isoformat()}] planning_export_pdf OUTER: {e}\n{tb}\n")
        except Exception:
            pass
        return f"Erreur génération PDF planning : {e}", 500


def _planning_export_pdf_impl():
    db = get_db()
    week_offset = int(request.args.get("week", 0) or 0)
    today = date.today()
    lundi = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    JOURS_NOMS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    jours = []
    for i in range(7):
        d = lundi + timedelta(days=i)
        jours.append({"date": d.isoformat(), "label": f"{JOURS_NOMS[i]} {d.day:02d}/{d.month:02d}"})

    employes = [dict(e) for e in db.execute("SELECT * FROM employes WHERE restaurant_id=? AND statut='Actif' ORDER BY ordre, nom, prenom", (_rid(),)).fetchall()]
    date_debut = jours[0]["date"]
    date_fin = jours[6]["date"]
    creneaux = [dict(c) for c in db.execute("""SELECT p.*, e.prenom, e.nom as emp_nom, e.poste as emp_poste FROM planning p
                             JOIN employes e ON p.employe_id = e.id
                             WHERE p.restaurant_id=? AND p.date >= ? AND p.date <= ?
                             ORDER BY p.date, p.heure_debut""", (_rid(), date_debut, date_fin)).fetchall()]

    # Organiser par section
    def _hours(h_debut, h_fin):
        try:
            if not h_debut or not h_fin or ":" not in h_debut or ":" not in h_fin:
                return 0
            hd = int(h_debut.split(":")[0]) + int(h_debut.split(":")[1]) / 60
            hf = int(h_fin.split(":")[0]) + int(h_fin.split(":")[1]) / 60
            return max(hf - hd, 0)
        except Exception:
            return 0

    sections = {"Salle": [], "Bar": [], "Cuisine": []}
    emp_hours = {}
    for emp in employes:
        poste_val = emp["poste"] if emp["poste"] else "Salle"
        try:
            section = sync._infer_pole(poste_val)
        except Exception:
            section = "Salle"
        if section not in sections:
            section = "Salle"
        prenom_val = (emp["prenom"] or "").strip()
        nom_val = (emp["nom"] or "").strip()
        emp_data = {"id": emp["id"], "nom": f"{prenom_val} {nom_val}".strip() or "—", "creneaux": {}}
        total_h = 0
        for j in jours:
            slots = [c for c in creneaux if c["employe_id"] == emp["id"] and c["date"] == j["date"]]
            emp_data["creneaux"][j["date"]] = [f"{s['heure_debut'] or '?'}-{s['heure_fin'] or '?'}" for s in slots]
            total_h += sum(_hours(s["heure_debut"], s["heure_fin"]) for s in slots)
        emp_data["total_heures"] = round(total_h, 1)
        sections[section].append(emp_data)

    semaine_label = f"{jours[0]['label']} au {jours[6]['label']} {jours[6]['date'][:4]}"

    resto_name = current_user.restaurant_name if current_user.is_authenticated else "Mon Restaurant"
    pole_filter = request.args.get("pole", "")  # "", "Salle", "Bar", "Cuisine"

    # Shift color lookup
    shifts_db = db.execute("SELECT id, couleur FROM shifts WHERE restaurant_id=?", (_rid(),)).fetchall()
    shift_colors = {s["id"]: s["couleur"] for s in shifts_db}
    # Map creneaux to their shift colors
    creneau_colors = {}
    for c in creneaux:
        sid = c.get("shift_id") or 0
        creneau_colors[(c["employe_id"], c["date"], c["heure_debut"])] = shift_colors.get(sid, "#2D6A4A")

    # Build sections with color info
    for section_name in list(sections.keys()):
        for emp_data in sections[section_name]:
            emp_data["creneaux_colors"] = {}
            for j in jours:
                slots_c = [c for c in creneaux if c["employe_id"] == emp_data["id"] and c["date"] == j["date"]]
                emp_data["creneaux_colors"][j["date"]] = [
                    creneau_colors.get((emp_data["id"], j["date"], s["heure_debut"]), "#2D6A4A") for s in slots_c
                ]

    # Generate HTML for PDF
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
body{{font-family:Arial,sans-serif;font-size:9px;margin:0;padding:12px}}
h1{{font-size:16px;margin:0 0 2px}}
h2{{font-size:12px;color:#2D6A4A;margin:12px 0 6px;border-bottom:2px solid #2D6A4A;padding-bottom:3px}}
.sub{{font-size:10px;color:#666;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;margin-bottom:8px}}
th,td{{border:1px solid #ddd;padding:4px 5px;text-align:center;font-size:9px}}
th{{background:#F3F4F6;font-weight:700;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
td.name{{text-align:left;font-weight:600;width:120px}}
td.total{{font-weight:700;width:50px;background:#f0fdf4;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.slot{{padding:1px 3px;border-radius:2px;color:#fff;font-size:8px;display:inline-block;margin:1px 0;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.section-page{{page-break-after:always}}
.section-page:last-child{{page-break-after:auto}}
@media print{{@page{{size:A4 landscape;margin:8mm}}}}
</style></head><body>"""

    rendered_sections = [(name, emps) for name, emps in sections.items() if emps and (not pole_filter or name == pole_filter)]
    for idx, (section_name, emps) in enumerate(rendered_sections):
        cls = "section-page" if idx < len(rendered_sections) - 1 else ""
        html += f'<div class="{cls}">'
        html += f'<h1>{resto_name}</h1>'
        html += f'<div class="sub">Planning {section_name} &mdash; semaine du {semaine_label}</div>'
        html += f'<h2>{section_name}</h2><table><tr><th style="text-align:left;width:120px">Employ\u00e9</th>'
        for j in jours:
            html += f'<th>{j["label"]}</th>'
        html += '<th style="width:50px">Total</th></tr>'
        for emp in emps:
            html += f'<tr><td class="name">{emp["nom"]}</td>'
            for j in jours:
                slots = emp["creneaux"].get(j["date"], [])
                colors_list = emp.get("creneaux_colors", {}).get(j["date"], [])
                if slots:
                    cell = ""
                    for si, s in enumerate(slots):
                        c = colors_list[si] if si < len(colors_list) else "#2D6A4A"
                        cell += f'<span class="slot" style="background:{c}">{s}</span>'
                    html += f'<td>{cell}</td>'
                else:
                    html += '<td>-</td>'
            html += f'<td class="total">{emp["total_heures"]}h</td></tr>'
        html += '</table></div>'

    html += f'<div style="font-size:8px;color:#999;margin-top:8px">G\u00e9n\u00e9r\u00e9 le {datetime.now().strftime("%d/%m/%Y %H:%M")}</div></body></html>'

    # Génération PDF via reportlab (présent dans requirements.txt). weasyprint est
    # ignoré côté serveur car il dépend de GTK/cairo non disponibles.
    import io as _io
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT, TA_CENTER

        VERT = colors.HexColor("#152B1E")
        GRIS_CLAIR = colors.HexColor("#F6F7F4")
        GRIS_TRAIT = colors.HexColor("#E5E7E1")

        buf = _io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                                leftMargin=1.2*cm, rightMargin=1.2*cm,
                                topMargin=1.2*cm, bottomMargin=1.2*cm,
                                title=f"Planning {semaine_label} — {resto_name}")
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle("ttl", parent=styles["Normal"], fontName="Helvetica-Bold",
                                     fontSize=16, textColor=VERT, leading=18)
        sub_style = ParagraphStyle("sub", parent=styles["Normal"], fontName="Helvetica",
                                   fontSize=10, textColor=colors.HexColor("#6B7280"))
        section_style = ParagraphStyle("sec", parent=styles["Normal"], fontName="Helvetica-Bold",
                                       fontSize=12, textColor=VERT, spaceBefore=8, spaceAfter=6)

        elements = []
        elements.append(Paragraph(resto_name or "Mon Restaurant", title_style))
        elements.append(Paragraph(f"Planning — semaine du {semaine_label}", sub_style))
        elements.append(Spacer(1, 10))

        rendered = [(name, emps) for name, emps in sections.items()
                    if emps and (not pole_filter or name == pole_filter)]
        if not rendered:
            elements.append(Paragraph("Aucun employé planifié sur cette période.", sub_style))

        for sidx, (section_name, emps) in enumerate(rendered):
            elements.append(Paragraph(section_name, section_style))
            header = ["Employé"] + [j["label"] for j in jours] + ["Total"]
            data = [header]
            for emp in emps:
                row = [emp.get("nom") or ""]
                for j in jours:
                    slots = emp.get("creneaux", {}).get(j["date"], []) or []
                    row.append("\n".join(slots) if slots else "—")
                row.append(f"{emp.get('total_heures', 0)}h")
                data.append(row)
            # Largeur utile en paysage : 27.7cm - 2*1.2cm = ~25.3cm
            usable = 25.3*cm
            col_w = [4.5*cm] + [(usable - 4.5*cm - 2*cm) / 7] * 7 + [2*cm]
            t = Table(data, colWidths=col_w, repeatRows=1)
            style_cmds = [
                ("BACKGROUND", (0, 0), (-1, 0), VERT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("FONTSIZE", (0, 1), (-1, -1), 8),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.3, GRIS_TRAIT),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
            for i in range(1, len(data)):
                if i % 2 == 0:
                    style_cmds.append(("BACKGROUND", (0, i), (-1, i), GRIS_CLAIR))
            t.setStyle(TableStyle(style_cmds))
            elements.append(t)
            if sidx < len(rendered) - 1:
                elements.append(PageBreak())

        elements.append(Spacer(1, 10))
        elements.append(Paragraph(
            f"Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')} via Pauco",
            ParagraphStyle("ftr", parent=styles["Normal"], fontName="Helvetica",
                           fontSize=8, textColor=colors.HexColor("#9CA3AF"), alignment=TA_CENTER)
        ))

        doc.build(elements)
        buf.seek(0)
        resp = make_response(buf.getvalue())
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f"attachment; filename=planning-{date_debut}-{date_fin}.pdf"
        return resp
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[PLANNING-PDF] {e}\n{tb}")
        try:
            with open("errors.log", "a", encoding="utf-8") as _lf:
                _lf.write(f"[{datetime.now().isoformat()}] planning_export_pdf: {e}\n{tb}\n")
        except Exception:
            pass
        return f"Erreur génération PDF : {e}", 500


# ---------------------------------------------------------------------------
#  Feedback (signalement / suggestion)
# ---------------------------------------------------------------------------

@app.route("/api/shooting-request", methods=["POST"])
def shooting_request():
    data = request.get_json()
    prenom = data.get("prenom", "")
    resto = data.get("resto", "")
    email = data.get("email", "")
    tel = data.get("tel", "")
    msg = data.get("msg", "")
    # Email via Brevo
    brevo_key = os.environ.get("BREVO_API_KEY", "")
    if brevo_key:
        try:
            import requests as _rq
            _rq.post("https://api.brevo.com/v3/smtp/email", timeout=10,
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                json={
                    "sender": {"name": "Pauco", "email": "paul@paucoandco.com"},
                    "to": [{"email": "paul@paucoandco.com"}],
                    "subject": f"Shooting photo 799€ — {resto}",
                    "htmlContent": f"<p><strong>Shooting photo 799€</strong></p><p>Prénom: {prenom}<br>Restaurant: {resto}<br>Email: {email}<br>Tél: {tel}<br>Message: {msg or '(aucun)'}</p>",
                })
        except Exception:
            pass
    # Telegram
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "1606156456")
    if tg_token:
        try:
            import requests as _rq
            _rq.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", timeout=5,
                json={"chat_id": tg_chat, "text": f"📸 Shooting photo 799€ — {resto} — {email} — {tel}"})
        except Exception:
            pass
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
#  Webhook Stripe — checkout.session.completed
# ---------------------------------------------------------------------------

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Webhook Stripe — signature désactivée temporairement pour debug."""
    import json as _json
    app.logger.info("=== WEBHOOK STRIPE RECU ===")
    try:
        payload = request.get_data()
        data = _json.loads(payload)
        evt_type = data.get("type", "unknown")
        app.logger.info(f"[STRIPE] Event type: {evt_type}")

        if evt_type == "checkout.session.completed":
            session_obj = data.get("data", {}).get("object", {})
            email = session_obj.get("customer_details", {}).get("email", "inconnu")
            app.logger.info(f"[STRIPE] Checkout completed: {email}")
            try:
                import sys as _sys
                _app_client = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app_client")
                if _app_client not in _sys.path:
                    _sys.path.insert(0, _app_client)
                from stripe_webhook import handle_new_client
                handle_new_client(session_obj)
                app.logger.info(f"[STRIPE] handle_new_client OK pour {email}")
            except ImportError as ie:
                app.logger.error(f"[STRIPE] Import error: {ie}")
                _handle_inline(session_obj)
            except Exception as e:
                app.logger.error(f"[STRIPE] Error: {e}")
                _handle_inline(session_obj)
    except Exception as e:
        app.logger.error(f"[STRIPE] Webhook error: {e}")

    return jsonify({"status": "ok"}), 200


def _handle_inline(session_obj):
    """Fallback si stripe_webhook.py n'est pas importable."""
    import requests as _req
    cd = session_obj.get("customer_details", {}) or {}
    meta = session_obj.get("metadata", {}) or {}
    email = (cd.get("email") or "").strip().lower()
    nom = meta.get("restaurant_name") or cd.get("name") or "Nouveau restaurant"
    ville = meta.get("ville", "")
    stripe_id = session_obj.get("customer", "")
    app.logger.info(f"[STRIPE-INLINE] Fallback pour {nom} ({email})")
    # Telegram
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        try:
            _req.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": f"\U0001f389 Nouveau client (fallback)\n{nom}\n{email}\n{ville}"},
                      timeout=5)
        except Exception:
            pass
    # Airtable Restaurants
    at_key = os.environ.get("AIRTABLE_API_KEY", "")
    if at_key:
        try:
            _req.post("https://api.airtable.com/v0/app37TquPqedRoJ96/tblxhvnwlfLU8bLPU",
                      headers={"Authorization": f"Bearer {at_key}", "Content-Type": "application/json"},
                      json={"fields": {"Nom": nom, "Email": email, "Ville": ville,
                                        "Stripe_ID": stripe_id, "Actif": True,
                                        "Date_inscription": datetime.now().strftime("%Y-%m-%d")}},
                      timeout=10)
            app.logger.info(f"[STRIPE-INLINE] Restaurant créé: {nom}")
        except Exception as e:
            app.logger.error(f"[STRIPE-INLINE] Airtable error: {e}")


@app.route("/feedback", methods=["POST"])
def feedback():
    data = request.get_json()
    fb_type = data.get("type", "probleme")
    message = data.get("message", "").strip()
    page_url = data.get("page", "")
    if not message:
        return jsonify({"ok": False, "error": "Message vide"}), 400

    label = "Probleme" if fb_type == "probleme" else "Suggestion"
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    subject = f"[Pauco Gestion] {label} — {now_str}"
    resto = current_user.restaurant_name if current_user.is_authenticated else "Inconnu"
    body = f"Type : {label}\nPage : {page_url}\nDate : {now_str}\nRestaurant : {resto}\n\nMessage :\n{message}"

    # Try Brevo API
    brevo_key = os.environ.get("BREVO_API_KEY", "")
    if brevo_key:
        try:
            import requests as _rq
            _rq.post("https://api.brevo.com/v3/smtp/email", timeout=10,
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                json={
                    "sender": {"name": "Pauco Gestion", "email": "paul@paucoandco.com"},
                    "to": [{"email": "paul@paucoandco.com", "name": "Paul"}],
                    "subject": subject,
                    "textContent": body,
                })
        except Exception:
            pass

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
#  Keep-alive ping
# ---------------------------------------------------------------------------

@app.route("/ping")
def ping():
    return "ok", 200


@app.route("/diag/airtable")
def diag_airtable():
    """Diagnostic Airtable : présence des env vars + appel test."""
    out = {}
    pat = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_API_KEY") or ""
    base = os.environ.get("AIRTABLE_BASE", "app37TquPqedRoJ96")
    out["AIRTABLE_PAT_present"] = bool(pat)
    out["AIRTABLE_PAT_prefix"] = (pat[:7] + "…") if pat else None
    out["AIRTABLE_BASE"] = base
    if not pat:
        out["error"] = "AIRTABLE_PAT manquant dans les env vars Railway"
        return out, 500
    try:
        import requests
        # Appel meta minimal pour valider PAT + base
        r = requests.get(
            f"https://api.airtable.com/v0/meta/bases/{base}/tables",
            headers={"Authorization": f"Bearer {pat}"},
            timeout=10,
        )
        out["meta_status"] = r.status_code
        if r.status_code != 200:
            out["meta_body"] = r.text[:300]
        else:
            out["tables_count"] = len(r.json().get("tables", []))
        # Lecture employes (1 record) pour valider scopes data
        r2 = requests.get(
            f"https://api.airtable.com/v0/{base}/tblZomXIhuKCqAkSk?maxRecords=1",
            headers={"Authorization": f"Bearer {pat}"},
            timeout=10,
        )
        out["employes_status"] = r2.status_code
        if r2.status_code != 200:
            out["employes_body"] = r2.text[:300]
        else:
            out["employes_sample_count"] = len(r2.json().get("records", []))
    except Exception as e:
        out["exception"] = str(e)
    return out, 200


@app.route("/version")
def version():
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT_SHA") or ""
    branch = os.environ.get("RAILWAY_GIT_BRANCH") or ""
    if not sha:
        try:
            import subprocess
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.abspath(__file__))).decode().strip()
        except Exception:
            sha = "unknown"
    return {"sha": sha, "branch": branch}, 200


@app.route("/healthz")
def healthz():
    return "ok", 200


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  APScheduler — Rapport WhatsApp 9h30 + Backup Airtable 3h00 + Keep-alive 1min
# ---------------------------------------------------------------------------
_scheduler_started = False


def _keep_alive_ping():
    """GET sur l'app toutes les 1 min pour empêcher Railway de la mettre en veille."""
    import requests as _req
    try:
        _req.get("https://app.paucoandco.com/ping", timeout=10)
    except Exception:
        pass


def _run_backup():
    """Wrapper pour le backup Airtable nightly."""
    try:
        import sys as _sys
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _project_root not in _sys.path:
            _sys.path.insert(0, _project_root)
        from backup_airtable import run_backup
        run_backup()
    except Exception as e:
        print(f"[BACKUP] Erreur: {e}")


# ═══════════════════════════════════════════════════════════════════════
#  MODULE HYGIENE — Etiquetage HACCP, Temperatures, Receptions,
#                   Tracabilite viandes, Checklists, PMS
# ═══════════════════════════════════════════════════════════════════════


def _pdf_resto_header(rid):
    """Retourne (nom, sous_titre) pour les en-tetes PDF depuis les infos restaurant Airtable."""
    resto = at.get_restaurant(rid) or {}
    nom = resto.get("Nom") or "Mon Restaurant"
    parts = []
    adresse = resto.get("Adresse") or ""
    ville = resto.get("Ville") or ""
    if adresse:
        parts.append(adresse)
    if ville and ville not in adresse:
        parts.append(ville)
    siret = resto.get("SIRET") or ""
    if siret:
        parts.append(f"SIRET {siret}")
    sous_titre = " - ".join(parts) if parts else ""
    return nom, sous_titre

_ALLERGENES_LIST = [
    "Gluten", "Crustacés", "Oeufs", "Poissons", "Arachides", "Soja",
    "Lait", "Fruits à coque", "Céleri", "Moutarde", "Sésame", "Sulfites",
    "Lupin", "Mollusques",
]

_DLC_DEFAULTS = {
    "Plat cuisiné": 3, "Sauce": 3, "Dessert": 2, "Préparation crue": 1,
    "Soupe/Bouillon": 3, "Pâtisserie": 2, "Autre": 3,
}

_TEMP_NORMES = {
    "Positif (0-4°C)": (0, 4), "Négatif (-18°C)": (-30, -18),
    "Surgélateur (-25°C)": (-35, -25), "Chaud (>63°C)": (63, 100),
}

_CHECKLIST_DEFAULTS = {
    "ouverture": [
        "Vérifier températures frigos/congélateurs",
        "Sortir les produits de décongélation",
        "Nettoyer les plans de travail",
        "Vérifier les DLC des produits",
        "Préparer les postes de travail",
        "Vérifier le stock (ruptures)",
        "Allumer les équipements de cuisson",
        "Vérifier propreté salle + toilettes",
    ],
    "fermeture": [
        "Nettoyer et désinfecter les plans de travail",
        "Ranger les produits au froid (film, date, nom)",
        "Vider les poubelles",
        "Nettoyer les sols",
        "Éteindre les équipements de cuisson",
        "Vérifier la fermeture des chambres froides",
        "Vérifier températures frigos/congélateurs",
        "Fermer les arrivées de gaz",
    ],
}


@app.route("/gestion/hygiene")
@login_required
def hygiene_page():
    return redirect(url_for("hygiene_etiquettes"))


_DLC_CONFIG_SENTINEL = "__DLC_CONFIG__"

def _load_dlc_types(rid):
    """Read DLC types from Etiquettes_HACCP sentinel record (Nom_Plat=__DLC_CONFIG__, JSON in Allergenes)."""
    import json as _json
    types = dict(_DLC_DEFAULTS)
    try:
        recs = at.get_all("etiquettes_haccp", rid)
        for r in recs:
            if r.get("Nom_Plat") == _DLC_CONFIG_SENTINEL:
                try:
                    custom = _json.loads(r.get("Allergenes") or "{}")
                    if isinstance(custom, dict):
                        types = custom  # config is authoritative
                except Exception:
                    pass
                break
    except Exception as e:
        print(f"[DLC-TYPES] load error: {e}")
    return types

def _save_dlc_types(rid, types_dict):
    import json as _json
    val = _json.dumps(types_dict, ensure_ascii=False)
    recs = at.get_all("etiquettes_haccp", rid)
    existing = next((r for r in recs if r.get("Nom_Plat") == _DLC_CONFIG_SENTINEL), None)
    if existing:
        at.update("etiquettes_haccp", existing["id"], {"Allergenes": val})
    else:
        at.create("etiquettes_haccp", {"Restaurant_ID": rid, "Nom_Plat": _DLC_CONFIG_SENTINEL, "Allergenes": val})
    at.invalidate_cache("etiquettes_haccp")


@app.route("/gestion/hygiene/etiquettes")
@login_required
def hygiene_etiquettes():
    rid = current_user.restaurant_id
    all_etiq = at.get_all("etiquettes_haccp", rid, sort=["-Date_Production"])
    etiquettes = [e for e in all_etiq if e.get("Nom_Plat") != _DLC_CONFIG_SENTINEL]
    employes = at.get_employes(rid)
    noms_employes = sorted(set(
        f"{e.get('Prénom', e.get('Prenom', ''))} {e.get('Nom', '')}".strip()
        for e in employes if e.get("Prénom") or e.get("Prenom") or e.get("Nom")
    ))
    dlc_types = _load_dlc_types(rid)
    kiosk = request.args.get("hygiene_kiosk") == "true"
    return render_template("base.html", page="hygiene_etiquettes",
                           etiquettes=etiquettes, employes_noms=noms_employes,
                           allergenes_list=_ALLERGENES_LIST,
                           dlc_defaults=dlc_types, kiosk=kiosk)


@app.route("/gestion/hygiene/etiquettes/save", methods=["POST"])
@login_required
def hygiene_etiquettes_save():
    rid = current_user.restaurant_id
    data = request.get_json()
    action = data.get("action", "create")

    if action == "save_dlc_types":
        _save_dlc_types(rid, data.get("types", {}) or {})
        return jsonify({"ok": True})

    if action == "delete":
        rec_id = data.get("id", "")
        if rec_id:
            at.delete("etiquettes_haccp", rec_id)
        return jsonify({"ok": True})

    from datetime import timedelta
    nom = data.get("nom_plat", "").strip()
    cuisinier = data.get("cuisinier", "").strip()
    allergenes = ", ".join(data.get("allergenes", []))
    temp = data.get("temperature")
    dlc_jours = int(data.get("dlc_jours", 3))
    now = datetime.now()
    dlc = (now + timedelta(days=dlc_jours)).strftime("%Y-%m-%d")
    if not nom:
        return jsonify({"ok": False, "error": "Nom du plat requis"}), 400
    rec = at.create("etiquettes_haccp", {
        "Restaurant_ID": rid, "Nom_Plat": nom, "Cuisinier": cuisinier,
        "Allergenes": allergenes, "Temperature_Stockage": float(str(temp).replace(",", ".")) if temp not in (None, "") else None,
        "DLC": dlc, "DLC_Jours": dlc_jours,
        "Date_Production": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })
    return jsonify({"ok": True, "id": rec.get("id", ""), "dlc": dlc,
                    "date_production": now.strftime("%d/%m/%Y %H:%M")})


@app.route("/api/haccp/types-dlc", methods=["GET"])
@login_required
def api_haccp_types_dlc_get():
    return jsonify({"ok": True, "types": _load_dlc_types(current_user.restaurant_id)})


@app.route("/api/haccp/types-dlc", methods=["POST"])
@login_required
def api_haccp_types_dlc_post():
    rid = current_user.restaurant_id
    data = request.get_json() or {}
    nom = (data.get("nom") or "").strip()
    jours = int(data.get("jours") or 3)
    if not nom:
        return jsonify({"ok": False, "error": "nom requis"}), 400
    types = _load_dlc_types(rid)
    types[nom] = jours
    _save_dlc_types(rid, types)
    return jsonify({"ok": True, "types": types})


@app.route("/api/haccp/types-dlc/<path:nom>", methods=["DELETE"])
@login_required
def api_haccp_types_dlc_delete(nom):
    rid = current_user.restaurant_id
    types = _load_dlc_types(rid)
    if nom in types:
        del types[nom]
        _save_dlc_types(rid, types)
    return jsonify({"ok": True, "types": types})


@app.route("/gestion/hygiene/etiquette/pdf/<record_id>")
@login_required
def hygiene_etiquette_pdf(record_id):
    from io import BytesIO
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    rec = at.get_one("etiquettes_haccp", record_id)
    if not rec or (rec.get("Restaurant_ID") or "") != current_user.restaurant_id:
        return "Not found", 404
    # Format Brother QL 62mm wide label
    label_w = 62 * mm
    label_h = 40 * mm
    buf = BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(label_w, label_h))
    # Header
    c.setFillColor(HexColor("#0F1F14"))
    c.rect(0, label_h - 10*mm, label_w, 10*mm, fill=True, stroke=False)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(label_w/2, label_h - 7*mm, (rec.get("Nom_Plat") or "")[:30])
    # Body
    y = label_h - 14*mm
    c.setFillColor(HexColor("#000000"))
    c.setFont("Helvetica", 6.5)
    c.drawString(2*mm, y, f"Cuisinier: {rec.get('Cuisinier') or '-'}")
    y -= 3.5*mm
    allergenes = rec.get("Allergenes") or ""
    if allergenes:
        c.drawString(2*mm, y, f"Allergènes: {allergenes[:50]}")
        y -= 3.5*mm
    temp = rec.get("Temperature_Stockage")
    if temp is not None:
        c.drawString(2*mm, y, f"Stockage: {temp}°C")
        y -= 3.5*mm
    c.setFont("Helvetica-Bold", 7)
    prod = rec.get("Date_Production", "")[:10]
    dlc = rec.get("DLC", "")
    c.drawString(2*mm, y, f"Prod: {prod}  |  DLC: {dlc}")
    y -= 4*mm
    c.setFont("Helvetica", 5.5)
    c.drawCentredString(label_w/2, 2*mm, "HACCP — Pauco")
    c.save()
    buf.seek(0)
    fname = f"etiquette_{rec.get('Nom_Plat', 'plat').replace(' ', '_')}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=False, download_name=fname)


@app.route("/gestion/hygiene/releves")
@login_required
def hygiene_releves():
    rid = current_user.restaurant_id
    releves = at.get_all("releves_temp", rid, sort=["-Date"])
    equips = at.get_all("equipements_froid", rid)
    employes = at.get_employes(rid)
    noms_employes = sorted(set(
        f"{e.get('Prénom', e.get('Prenom', ''))} {e.get('Nom', '')}".strip()
        for e in employes if e.get("Prénom") or e.get("Prenom") or e.get("Nom")
    ))
    kiosk = request.args.get("hygiene_kiosk") == "true"
    # Documents archivés (table SQLite + URLs présignées R2)
    db = get_db()
    doc_rows = db.execute(
        "SELECT id, periode, type_doc, filename, r2_key, uploaded_at "
        "FROM documents_temperatures WHERE restaurant_id=? ORDER BY uploaded_at DESC",
        (rid,)
    ).fetchall()
    temp_docs = []
    try:
        from modules.r2_client import get_file_url, is_configured
        r2_ok = is_configured()
    except Exception:
        r2_ok = False
        get_file_url = None
    for r in doc_rows:
        url = ""
        if r2_ok and r["r2_key"]:
            try:
                url = get_file_url(r["r2_key"])
            except Exception as e:
                print(f"[TEMP-DOC] presign error {r['r2_key']}: {e}")
        temp_docs.append({
            "id": r["id"], "periode": r["periode"], "type_doc": r["type_doc"],
            "filename": r["filename"], "uploaded_at": r["uploaded_at"] or "", "url": url,
        })
    return render_template("base.html", page="hygiene_releves",
                           releves=releves[:200],
                           equipements=[dict(e) for e in equips],
                           employes_noms=noms_employes,
                           types_froid=_TYPES_FROID,
                           temp_docs=temp_docs,
                           temp_normes=_TEMP_NORMES, kiosk=kiosk,
                           now_date=datetime.now().strftime("%Y-%m-%d"))


@app.route("/gestion/hygiene/releves/signaler", methods=["POST"])
@login_required
def hygiene_releves_signaler():
    """Ajoute une note de signalement sur un relevé sans modifier la valeur originale.
    Les relevés de température sont des documents légaux immuables (PMS)."""
    data = request.get_json() or {}
    rec_id = (data.get("id") or "").strip()
    note = (data.get("note") or "").strip()
    if not rec_id or not note:
        return jsonify({"ok": False, "error": "id et note requis"}), 400
    horodate = datetime.now().strftime("%Y-%m-%d %H:%M")
    operateur = current_user.username if hasattr(current_user, "username") else ""
    suffix = f"[{horodate}] {operateur}: {note}"
    try:
        existing = at.get_one("releves_temp", rec_id) or {}
        prev_note = (existing.get("Note") or "").strip()
        new_note = (prev_note + "\n" + suffix).strip() if prev_note else suffix
        at.update("releves_temp", rec_id, {"Note": new_note})
        at.invalidate_cache("releves_temp")
    except Exception as e:
        print(f"[RELEVE-SIGNAL] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/gestion/hygiene/releves/save", methods=["POST"])
@login_required
def hygiene_releves_save():
    rid = current_user.restaurant_id
    data = request.get_json()
    action = data.get("action", "releve")

    # Gestion des équipements (fusionné depuis /gestion/temperatures/save)
    if action == "add_equip":
        nom = data.get("nom", "").strip()
        type_f = data.get("type", "Positif (0-4°C)")
        freq = data.get("frequence", "1x")
        if not nom:
            return jsonify({"ok": False, "error": "Nom requis"}), 400
        rec = at.create("equipements_froid", {
            "Restaurant_ID": rid, "Nom": nom, "Type": type_f,
            "Actif": True, "Frequence": freq,
        })
        at.invalidate_cache("equipements_froid")
        return jsonify({"ok": True, "id": rec.get("id", "")})
    elif action == "delete_equip":
        rec_id = data.get("id", "")
        if rec_id:
            at.delete("equipements_froid", rec_id)
            at.invalidate_cache("equipements_froid")
        return jsonify({"ok": True})

    # Saisie relevé température
    equipement = data.get("equipement", "")
    temperature = data.get("temperature")
    type_equip = data.get("type_equipement", "Positif (0-4°C)")
    operateur = data.get("operateur", "")
    date_str = data.get("date", "")
    if temperature is None:
        return jsonify({"ok": False, "error": "Température requise"}), 400
    temp_val = float(str(temperature).replace(",", "."))
    norme = _TEMP_NORMES.get(type_equip, (0, 4))
    conforme = norme[0] <= temp_val <= norme[1]
    now = datetime.now()
    if date_str:
        date_val = f"{date_str}T{now.strftime('%H:%M:%S')}.000Z"
    else:
        date_val = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rec = at.create("releves_temp", {
        "Restaurant_ID": rid, "Equipement": equipement,
        "Temperature": temp_val, "Conforme": conforme,
        "Type_Equipement": type_equip, "Operateur": operateur,
        "Date": date_val,
    })
    return jsonify({"ok": True, "conforme": conforme, "id": rec.get("id", "")})


@app.route("/gestion/hygiene/releves/pdf")
@login_required
def hygiene_releves_pdf():
    try:
        from io import BytesIO
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.colors import HexColor
    except ImportError as e:
        return f"reportlab non installe: {e}", 500

    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"

    # Filtre par mois (optionnel, ex: ?mois=2026-04)
    mois = request.args.get("mois", "")
    if not mois:
        mois = datetime.now().strftime("%Y-%m")
    mois_label = mois

    try:
        releves = at.get_all("releves_temp", rid)
    except Exception as e:
        return f"Erreur Airtable: {e}", 500

    # Filtrer par mois cote serveur
    filtered = []
    for r in releves:
        dt = r.get("Date") or ""
        if dt[:7] == mois:
            filtered.append(r)
    # Tri par date decroissante
    filtered.sort(key=lambda x: x.get("Date") or "", reverse=True)

    try:
        buf = BytesIO()
        w, h = landscape(A4)
        c = pdf_canvas.Canvas(buf, pagesize=(w, h))

        # Header
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h-60, w, 60, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(w/2, h-25, f"Releve de temperatures - {resto_name}")
        c.setFont("Helvetica", 10)
        c.drawCentredString(w/2, h-40, f"Periode : {mois_label}  |  {len(filtered)} releve(s)")
        c.setFont("Helvetica", 8)
        c.drawCentredString(w/2, h-52, f"Exporte le {datetime.now().strftime('%d/%m/%Y %H:%M')}  |  Document DDPP")

        # Equipements summary
        equips = at.get_all("equipements_froid", rid)
        y = h - 85
        if equips:
            c.setFillColor(HexColor("#000000"))
            c.setFont("Helvetica-Bold", 9)
            c.drawString(30, y, "Equipements configures :")
            y -= 14
            c.setFont("Helvetica", 8)
            for eq in equips:
                nom_eq = (eq.get("Nom") or "?")
                type_eq = (eq.get("Type") or "?")
                c.drawString(40, y, f"- {nom_eq} ({type_eq})")
                y -= 12
            y -= 8

        # Table header
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(25, y - 2, w - 50, 20, fill=True, stroke=False)
        c.setFillColor(HexColor("#000000"))
        c.setFont("Helvetica-Bold", 8)
        cols = [30, 170, 310, 420, 510, 590, 680]
        headers = ["Date/Heure", "Equipement", "Type", "Temp", "Conforme", "Operateur", "Alerte"]
        for x, t in zip(cols, headers):
            c.drawString(x, y + 4, t)
        y -= 18

        # Table rows
        c.setFont("Helvetica", 8)
        non_conformes = 0
        for r in filtered:
            if y < 40:
                c.showPage()
                c.setFont("Helvetica", 8)
                y = h - 40
            dt = (r.get("Date") or "")[:16].replace("T", " ")
            c.drawString(cols[0], y, str(dt))
            c.drawString(cols[1], y, str(r.get("Equipement") or "")[:22])
            c.drawString(cols[2], y, str(r.get("Type_Equipement") or "")[:16])
            temp = r.get("Temperature")
            c.drawString(cols[3], y, f"{temp} C" if temp is not None else "-")
            conforme = r.get("Conforme")
            conf_text = "OUI" if conforme else "NON"
            c.setFillColor(HexColor("#065F46") if conforme else HexColor("#991B1B"))
            c.drawString(cols[4], y, conf_text)
            c.setFillColor(HexColor("#000000"))
            c.drawString(cols[5], y, str(r.get("Operateur") or "")[:18])
            if not conforme:
                non_conformes += 1
                c.setFillColor(HexColor("#991B1B"))
                c.drawString(cols[6], y, "HORS NORME")
                c.setFillColor(HexColor("#000000"))
            y -= 14

        # Footer summary
        y -= 10
        c.setFont("Helvetica-Bold", 9)
        c.drawString(30, y, f"Total : {len(filtered)} releve(s)  |  {non_conformes} alerte(s) hors norme")

        c.save()
        buf.seek(0)
        fname = f"releves_temperatures_{mois.replace('-', '')}.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Erreur generation PDF: {e}", 500


@app.route("/gestion/hygiene/releves/fiche-semaine")
@login_required
def hygiene_releves_fiche_semaine():
    """Genere une fiche semaine vierge a remplir a la main."""
    try:
        from io import BytesIO
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.colors import HexColor
    except ImportError as e:
        return f"reportlab non installe: {e}", 500

    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"

    # Semaine : lundi de la semaine en cours
    from datetime import timedelta
    today = datetime.now().date()
    lundi = today - timedelta(days=today.weekday())
    dimanche = lundi + timedelta(days=6)
    jours = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    dates_jours = [(lundi + timedelta(days=i)).strftime("%d/%m") for i in range(7)]

    equips = at.get_all("equipements_froid", rid)
    actifs = [e for e in equips if e.get("Actif")]
    if not actifs:
        actifs = [{"Nom": "(aucun equipement)", "Type": ""}]

    try:
        buf = BytesIO()
        w, h = landscape(A4)
        c = pdf_canvas.Canvas(buf, pagesize=(w, h))

        # Header
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h - 55, w, 55, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 15)
        c.drawCentredString(w / 2, h - 22, "RELEVE DES TEMPERATURES - FICHE SEMAINE")
        c.setFont("Helvetica", 10)
        c.drawString(30, h - 42, str(resto_name))
        semaine_label = f"Semaine du {lundi.strftime('%d/%m/%Y')} au {dimanche.strftime('%d/%m/%Y')}"
        c.drawRightString(w - 30, h - 42, semaine_label)

        # Table dimensions
        margin_l = 25
        margin_r = 25
        table_w = w - margin_l - margin_r
        equip_col_w = 120
        day_block_w = (table_w - equip_col_w) / 7
        row_h = max(28, 180 / max(len(actifs), 1))  # adapt row height
        if row_h > 40:
            row_h = 40

        y_start = h - 75

        # Header row: days
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(margin_l, y_start - 30, table_w, 30, fill=True, stroke=False)
        c.setFillColor(HexColor("#0F1F14"))
        c.setFont("Helvetica-Bold", 8)
        c.drawString(margin_l + 4, y_start - 12, "Equipement")
        for i in range(7):
            cx = margin_l + equip_col_w + i * day_block_w
            c.drawCentredString(cx + day_block_w / 2, y_start - 12, f"{jours[i]} {dates_jours[i]}")
            # Sub-headers: Midi / Soir
            c.setFont("Helvetica", 6)
            c.drawCentredString(cx + day_block_w / 4, y_start - 24, "Midi")
            c.drawCentredString(cx + day_block_w * 3 / 4, y_start - 24, "Soir")
            c.setFont("Helvetica-Bold", 8)

        y = y_start - 30

        # Grid lines and equipment rows
        c.setStrokeColor(HexColor("#CCCCCC"))
        c.setFont("Helvetica", 7.5)
        for eq in actifs:
            c.setFillColor(HexColor("#000000"))
            nom_eq = str(eq.get("Nom") or "?")[:18]
            type_eq = str(eq.get("Type") or "")[:12]
            c.drawString(margin_l + 4, y - row_h / 2 - 2, nom_eq)
            c.setFillColor(HexColor("#999999"))
            c.setFont("Helvetica", 5.5)
            c.drawString(margin_l + 4, y - row_h / 2 - 10, type_eq)
            c.setFont("Helvetica", 7.5)

            # Horizontal line
            c.line(margin_l, y - row_h, margin_l + table_w, y - row_h)

            # Vertical lines for each day + midi/soir split
            for i in range(7):
                cx = margin_l + equip_col_w + i * day_block_w
                c.line(cx, y, cx, y - row_h)
                # Midi/Soir divider (dashed)
                c.setDash(1, 2)
                c.line(cx + day_block_w / 2, y, cx + day_block_w / 2, y - row_h)
                c.setDash()

            y -= row_h
            if y < 50:
                break

        # Outer border
        total_h = y_start - 30 - y
        c.rect(margin_l, y, table_w, total_h, fill=False, stroke=True)
        # Equipment column border
        c.line(margin_l + equip_col_w, y_start - 30, margin_l + equip_col_w, y)

        # Footer
        c.setFillColor(HexColor("#666666"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(w / 2, 25, "Norme frigo : 0-4 C  |  Norme congelateur : < -18 C  |  Norme plats chauds : > 63 C")
        c.setFont("Helvetica", 6)
        c.drawCentredString(w / 2, 15, "Chaque case : temperature (C) + initiales operateur  |  Pauco - pauco.fr")

        c.save()
        buf.seek(0)
        fname = f"fiche_temperatures_S{lundi.strftime('%W')}_{lundi.strftime('%Y%m%d')}.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Erreur generation PDF: {e}", 500


@app.route("/gestion/hygiene/receptions")
@login_required
def hygiene_receptions():
    rid = current_user.restaurant_id
    receptions = at.get_all("receptions", rid, sort=["-Date_Reception"])
    # Générer les URLs presignées pour les photos
    from modules.r2_client import get_file_url, is_configured as r2_configured
    r2_ok = r2_configured()
    for r in receptions:
        photo_key = r.get("Photo_Key") or ""
        if photo_key and r2_ok:
            try:
                r["_photo_url"] = get_file_url(photo_key, expires=3600)
            except Exception:
                r["_photo_url"] = ""
        else:
            r["_photo_url"] = ""
    fournisseurs = [dict(f) for f in at.get_all("fournisseurs", rid)]
    kiosk = request.args.get("hygiene_kiosk") == "true"
    return render_template("base.html", page="hygiene_receptions",
                           receptions=receptions[:100], fournisseurs=fournisseurs, kiosk=kiosk)


@app.route("/gestion/hygiene/receptions/save", methods=["POST"])
@login_required
def hygiene_receptions_save():
    rid = current_user.restaurant_id
    fournisseur = request.form.get("fournisseur", "").strip()
    temperature = request.form.get("temperature", "")
    conforme = request.form.get("conforme") == "oui"
    commentaire = request.form.get("commentaire", "")
    photo = request.files.get("photo")
    photo_key = ""
    if photo and photo.filename:
        from modules.r2_client import upload_file, is_configured
        if is_configured():
            import uuid
            ext = photo.filename.rsplit(".", 1)[-1] if "." in photo.filename else "jpg"
            fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
            folder = f"hygiene/{rid}/receptions"
            photo_key = upload_file(photo, folder, fname)
    now = datetime.now()
    rec = at.create("receptions", {
        "Restaurant_ID": rid, "Fournisseur": fournisseur,
        "Temperature": float(str(temperature).replace(",", ".")) if temperature else None,
        "Conforme": conforme, "Photo_Key": photo_key,
        "Commentaire": commentaire,
        "Date_Reception": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })
    return redirect(url_for("hygiene_receptions"))


@app.route("/gestion/hygiene/receptions/delete", methods=["POST"])
@login_required
def hygiene_receptions_delete():
    data = request.get_json()
    rec_id = data.get("id", "")
    if rec_id:
        at.delete("receptions", rec_id)
    return jsonify({"ok": True})


@app.route("/gestion/hygiene/receptions/pdf")
@login_required
def hygiene_receptions_pdf():
    try:
        from io import BytesIO
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.colors import HexColor
    except ImportError as e:
        return f"reportlab non installe: {e}", 500
    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"
    mois = request.args.get("mois", datetime.now().strftime("%Y-%m"))
    try:
        all_recs = at.get_all("receptions", rid)
        filtered = [r for r in all_recs if (r.get("Date_Reception") or "")[:7] == mois]
        filtered.sort(key=lambda x: x.get("Date_Reception") or "", reverse=True)
    except Exception as e:
        return f"Erreur Airtable: {e}", 500
    try:
        buf = BytesIO()
        w, h = landscape(A4)
        c = pdf_canvas.Canvas(buf, pagesize=(w, h))
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h - 55, w, 55, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 15)
        c.drawCentredString(w / 2, h - 22, "RECEPTIONS MARCHANDISES")
        c.setFont("Helvetica", 10)
        c.drawString(30, h - 42, str(resto_name))
        c.drawRightString(w - 30, h - 42, f"Periode : {mois}  |  {len(filtered)} reception(s)")
        y = h - 75
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(25, y - 2, w - 50, 18, fill=True, stroke=False)
        c.setFillColor(HexColor("#000000"))
        c.setFont("Helvetica-Bold", 8)
        cols = [30, 150, 310, 400, 480, 560, 680]
        for x, t in zip(cols, ["Date/Heure", "Fournisseur", "Temp", "Conforme", "Photo", "Commentaire"]):
            c.drawString(x, y + 3, t)
        y -= 18
        c.setFont("Helvetica", 8)
        for r in filtered:
            if y < 40:
                c.showPage()
                c.setFont("Helvetica", 8)
                y = h - 40
            dt = (r.get("Date_Reception") or "")[:16].replace("T", " ")
            temp = r.get("Temperature")
            c.drawString(cols[0], y, str(dt))
            c.drawString(cols[1], y, str(r.get("Fournisseur") or "")[:22])
            c.drawString(cols[2], y, f"{temp} C" if temp is not None else "-")
            conf = "OUI" if r.get("Conforme") else "NON"
            c.setFillColor(HexColor("#065F46") if r.get("Conforme") else HexColor("#991B1B"))
            c.drawString(cols[3], y, conf)
            c.setFillColor(HexColor("#000000"))
            c.drawString(cols[4], y, "Oui" if r.get("Photo_Key") else "-")
            c.drawString(cols[5], y, str(r.get("Commentaire") or "")[:30])
            y -= 13
        c.setFillColor(HexColor("#888888"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(w / 2, 20, "Document extrait du Plan de Maitrise Sanitaire - Pauco")
        c.save()
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"receptions_{mois.replace('-', '')}.pdf")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Erreur generation PDF: {e}", 500


@app.route("/gestion/hygiene/tracabilite")
@login_required
def hygiene_tracabilite():
    rid = current_user.restaurant_id
    viandes = at.get_all("tracabilite_viandes_hygiene", rid, sort=["-Date_Reception"])
    from modules.r2_client import get_file_url, is_configured as r2_configured
    r2_ok = r2_configured()
    for v in viandes:
        pk = v.get("Photo_Key") or ""
        if pk and r2_ok:
            try:
                v["_photo_url"] = get_file_url(pk, expires=3600)
            except Exception:
                v["_photo_url"] = ""
        else:
            v["_photo_url"] = ""
    kiosk = request.args.get("hygiene_kiosk") == "true"
    return render_template("base.html", page="hygiene_tracabilite",
                           viandes=viandes[:100], kiosk=kiosk)


@app.route("/gestion/hygiene/tracabilite/save", methods=["POST"])
@login_required
def hygiene_tracabilite_save():
    rid = current_user.restaurant_id
    fournisseur = request.form.get("fournisseur", "").strip()
    origine = request.form.get("origine_pays", "").strip()
    date_rec = request.form.get("date_reception", "")
    numero_lot = request.form.get("numero_lot", "").strip()
    description = request.form.get("description", "").strip()
    photo = request.files.get("photo")
    photo_key = ""
    if photo and photo.filename:
        from modules.r2_client import upload_file, is_configured
        if is_configured():
            import uuid
            ext = photo.filename.rsplit(".", 1)[-1] if "." in photo.filename else "jpg"
            fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.{ext}"
            folder = f"hygiene/{rid}/tracabilite"
            photo_key = upload_file(photo, folder, fname)
    rec = at.create("tracabilite_viandes_hygiene", {
        "Restaurant_ID": rid, "Fournisseur": fournisseur,
        "Origine_Pays": origine, "Date_Reception": date_rec or datetime.now().strftime("%Y-%m-%d"),
        "Numero_Lot": numero_lot, "Description": description, "Photo_Key": photo_key,
    })
    return redirect(url_for("hygiene_tracabilite"))


@app.route("/gestion/hygiene/tracabilite/delete", methods=["POST"])
@login_required
def hygiene_tracabilite_delete():
    data = request.get_json()
    rec_id = data.get("id", "")
    if rec_id:
        at.delete("tracabilite_viandes_hygiene", rec_id)
    return jsonify({"ok": True})


@app.route("/gestion/hygiene/tracabilite/pdf")
@login_required
def hygiene_tracabilite_pdf():
    try:
        from io import BytesIO
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.colors import HexColor
    except ImportError as e:
        return f"reportlab non installe: {e}", 500
    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"
    mois = request.args.get("mois", datetime.now().strftime("%Y-%m"))
    try:
        all_recs = at.get_all("tracabilite_viandes_hygiene", rid)
        filtered = [v for v in all_recs if (v.get("Date_Reception") or "")[:7] == mois]
        filtered.sort(key=lambda x: x.get("Date_Reception") or "", reverse=True)
    except Exception as e:
        return f"Erreur Airtable: {e}", 500
    try:
        buf = BytesIO()
        w, h = landscape(A4)
        c = pdf_canvas.Canvas(buf, pagesize=(w, h))
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h - 55, w, 55, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 15)
        c.drawCentredString(w / 2, h - 22, "TRACABILITE DES VIANDES")
        c.setFont("Helvetica", 10)
        c.drawString(30, h - 42, str(resto_name))
        c.drawRightString(w - 30, h - 42, f"Periode : {mois}  |  {len(filtered)} enregistrement(s)")
        y = h - 75
        c.setFillColor(HexColor("#E4DDD3"))
        c.rect(25, y - 2, w - 50, 18, fill=True, stroke=False)
        c.setFillColor(HexColor("#000000"))
        c.setFont("Helvetica-Bold", 8)
        cols = [30, 110, 280, 400, 500, 620, 730]
        for x, t in zip(cols, ["Date", "Description", "Fournisseur", "Origine", "N. Lot", "Photo"]):
            c.drawString(x, y + 3, t)
        y -= 18
        c.setFont("Helvetica", 8)
        for v in filtered:
            if y < 40:
                c.showPage()
                c.setFont("Helvetica", 8)
                y = h - 40
            c.drawString(cols[0], y, str(v.get("Date_Reception") or ""))
            c.drawString(cols[1], y, str(v.get("Description") or "")[:24])
            c.drawString(cols[2], y, str(v.get("Fournisseur") or "")[:18])
            c.drawString(cols[3], y, str(v.get("Origine_Pays") or "")[:14])
            c.drawString(cols[4], y, str(v.get("Numero_Lot") or "")[:16])
            c.drawString(cols[5], y, "Oui" if v.get("Photo_Key") else "-")
            y -= 13
        c.setFillColor(HexColor("#888888"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(w / 2, 20, "Document extrait du Plan de Maitrise Sanitaire - Pauco")
        c.save()
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"tracabilite_viandes_{mois.replace('-', '')}.pdf")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Erreur generation PDF: {e}", 500


@app.route("/gestion/hygiene/checklists")
@login_required
def hygiene_checklists():
    rid = current_user.restaurant_id
    checklists = at.get_all("checklists", rid, sort=["-Date"])
    items_ouv = at.get_all("checklist_items", rid, formula="{Type}='ouverture'", sort=["Ordre"])
    items_ferm = at.get_all("checklist_items", rid, formula="{Type}='fermeture'", sort=["Ordre"])
    # Si pas d'items configurés, créer les défauts
    if not items_ouv:
        for i, label in enumerate(_CHECKLIST_DEFAULTS["ouverture"]):
            at.create("checklist_items", {"Restaurant_ID": rid, "Type": "ouverture",
                                          "Label": label, "Ordre": i+1, "Actif": True})
        items_ouv = at.get_all("checklist_items", rid, formula="{Type}='ouverture'", sort=["Ordre"])
    if not items_ferm:
        for i, label in enumerate(_CHECKLIST_DEFAULTS["fermeture"]):
            at.create("checklist_items", {"Restaurant_ID": rid, "Type": "fermeture",
                                          "Label": label, "Ordre": i+1, "Actif": True})
        items_ferm = at.get_all("checklist_items", rid, formula="{Type}='fermeture'", sort=["Ordre"])
    kiosk = request.args.get("hygiene_kiosk") == "true"
    return render_template("base.html", page="hygiene_checklists",
                           checklists=checklists[:60], items_ouv=items_ouv,
                           items_ferm=items_ferm, kiosk=kiosk)


@app.route("/gestion/hygiene/checklists/save", methods=["POST"])
@login_required
def hygiene_checklists_save():
    rid = current_user.restaurant_id
    data = request.get_json()
    action = data.get("action", "")
    if action == "complete":
        import json as _json
        checklist_type = data.get("type", "ouverture")
        signataire = data.get("signataire", "")
        items = data.get("items", [])
        now = datetime.now()
        rec = at.create("checklists", {
            "Restaurant_ID": rid, "Type": checklist_type,
            "Date": now.strftime("%Y-%m-%d"),
            "Signataire": signataire, "Heure": now.strftime("%H:%M"),
            "Items_JSON": _json.dumps(items, ensure_ascii=False),
            "Complet": all(it.get("checked") for it in items),
        })
        return jsonify({"ok": True, "id": rec.get("id", "")})
    elif action == "add_item":
        checklist_type = data.get("type", "ouverture")
        label = data.get("label", "").strip()
        if not label:
            return jsonify({"ok": False, "error": "Label requis"}), 400
        existing = at.get_all("checklist_items", rid, formula=f"{{Type}}='{checklist_type}'")
        ordre = len(existing) + 1
        at.create("checklist_items", {"Restaurant_ID": rid, "Type": checklist_type,
                                       "Label": label, "Ordre": ordre, "Actif": True})
        return jsonify({"ok": True})
    elif action == "delete_item":
        item_id = data.get("item_id", "")
        if item_id:
            at.delete("checklist_items", item_id)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/api/checklists/reorder", methods=["POST"])
@login_required
def api_checklists_reorder():
    """Met à jour le champ Ordre des items checklist selon la liste d'IDs reçue."""
    data = request.get_json() or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "ids requis"}), 400
    rid = current_user.restaurant_id
    try:
        for idx, item_id in enumerate(ids, start=1):
            try:
                at.update("checklist_items", item_id, {"Ordre": idx})
            except Exception as e:
                print(f"[CHECKLIST-REORDER] update {item_id}: {e}")
        at.invalidate_cache("checklist_items", rid)
    except Exception as e:
        print(f"[CHECKLIST-REORDER] {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "count": len(ids)})


@app.route("/gestion/hygiene/checklists/pdf")
@login_required
def hygiene_checklists_pdf():
    """Genere un PDF imprimable pour une checklist ouverture ou fermeture."""
    try:
        from io import BytesIO
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.colors import HexColor
    except ImportError as e:
        return f"reportlab non installe: {e}", 500

    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"
    cl_type = request.args.get("type", "ouverture")
    cl_label = "Ouverture" if cl_type == "ouverture" else "Fermeture"

    items = at.get_all("checklist_items", rid,
                       formula=f"{{Type}}='{cl_type}'", sort=["Ordre"])

    try:
        buf = BytesIO()
        w, h = A4
        c = pdf_canvas.Canvas(buf, pagesize=A4)

        # Header
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h - 70, w, 70, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(w / 2, h - 30, f"Checklist {cl_label}")
        c.setFont("Helvetica", 11)
        c.drawCentredString(w / 2, h - 48, str(resto_name))
        c.setFont("Helvetica", 9)
        c.drawCentredString(w / 2, h - 62, f"Date : {datetime.now().strftime('%d/%m/%Y')}")

        y = h - 100

        # Items with checkbox squares
        c.setFillColor(HexColor("#000000"))
        c.setFont("Helvetica", 12)
        for it in items:
            if y < 120:
                c.showPage()
                c.setFont("Helvetica", 12)
                c.setFillColor(HexColor("#000000"))
                y = h - 50
            label = str(it.get("Label") or "")
            # Draw checkbox square
            c.setStrokeColor(HexColor("#333333"))
            c.rect(50, y - 3, 14, 14, fill=False, stroke=True)
            c.drawString(75, y, label)
            y -= 32

        # Signature section
        y -= 20
        c.setStrokeColor(HexColor("#CCCCCC"))
        c.setFont("Helvetica", 11)
        c.drawString(50, y, "Signataire : ___________________________________")
        y -= 30
        c.drawString(50, y, "Date / Heure : ________________________________")

        # Footer
        c.setFillColor(HexColor("#888888"))
        c.setFont("Helvetica", 7)
        c.drawCentredString(w / 2, 25, "Document a conserver 3 mois - Plan de Maitrise Sanitaire Pauco")

        c.save()
        buf.seek(0)
        fname = f"checklist_{cl_type}_{datetime.now().strftime('%Y%m%d')}.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Erreur generation PDF: {e}", 500


@app.route("/gestion/hygiene/checklists/pdf-serie")
@login_required
def hygiene_checklists_pdf_serie():
    """Genere un PDF multi-pages avec 1 checklist par jour sur une periode."""
    try:
        from io import BytesIO
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.colors import HexColor
    except ImportError as e:
        return f"reportlab non installe: {e}", 500

    rid = current_user.restaurant_id
    resto_name = current_user.restaurant_name or "Mon Restaurant"
    cl_type = request.args.get("type", "ouverture")
    cl_label = "Ouverture" if cl_type == "ouverture" else "Fermeture"
    debut = request.args.get("debut", "")
    fin = request.args.get("fin", "")
    vierge = request.args.get("vierge") == "1"

    if not debut or not fin:
        return "Parametres debut et fin requis", 400

    from datetime import timedelta
    try:
        dt_debut = datetime.strptime(debut, "%Y-%m-%d").date()
        dt_fin = datetime.strptime(fin, "%Y-%m-%d").date()
    except ValueError:
        return "Format de date invalide", 400

    if dt_fin < dt_debut:
        dt_debut, dt_fin = dt_fin, dt_debut
    nb_jours = (dt_fin - dt_debut).days + 1
    if nb_jours > 90:
        return "Maximum 90 jours", 400

    items = at.get_all("checklist_items", rid,
                       formula=f"{{Type}}='{cl_type}'", sort=["Ordre"])

    try:
        buf = BytesIO()
        w, h = A4
        c = pdf_canvas.Canvas(buf, pagesize=A4)

        for i in range(nb_jours):
            jour = dt_debut + timedelta(days=i)
            date_str = "" if vierge else jour.strftime("%d/%m/%Y")

            # Header
            c.setFillColor(HexColor("#0F1F14"))
            c.rect(0, h - 70, w, 70, fill=True, stroke=False)
            c.setFillColor(HexColor("#FFFFFF"))
            c.setFont("Helvetica-Bold", 18)
            c.drawCentredString(w / 2, h - 30, f"Checklist {cl_label}")
            c.setFont("Helvetica", 11)
            c.drawCentredString(w / 2, h - 48, str(resto_name))
            c.setFont("Helvetica", 9)
            if date_str:
                c.drawCentredString(w / 2, h - 62, f"Date : {date_str}")
            else:
                c.drawCentredString(w / 2, h - 62, "Date : ____/____/________")

            y = h - 100

            # Items
            c.setFillColor(HexColor("#000000"))
            c.setFont("Helvetica", 12)
            for it in items:
                if y < 120:
                    # overflow on same page — shouldn't happen with typical checklists
                    break
                label = str(it.get("Label") or "")
                c.setStrokeColor(HexColor("#333333"))
                c.rect(50, y - 3, 14, 14, fill=False, stroke=True)
                c.drawString(75, y, label)
                y -= 32

            # Signature
            y -= 20
            c.setFont("Helvetica", 11)
            c.setFillColor(HexColor("#000000"))
            c.drawString(50, y, "Signataire : ___________________________________")
            y -= 30
            c.drawString(50, y, "Date / Heure : ________________________________")

            # Footer
            c.setFillColor(HexColor("#888888"))
            c.setFont("Helvetica", 7)
            c.drawCentredString(w / 2, 25, "Document a conserver 3 mois - Plan de Maitrise Sanitaire Pauco")

            if i < nb_jours - 1:
                c.showPage()

        c.save()
        buf.seek(0)
        fname = f"checklists_{cl_type}_{debut}_a_{fin}.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Erreur generation PDF: {e}", 500


@app.route("/gestion/hygiene/kit")
@login_required
def hygiene_kit():
    kiosk = request.args.get("hygiene_kiosk") == "true"
    return render_template("base.html", page="hygiene_kit", kiosk=kiosk)


@app.route("/gestion/hygiene/kit/interest", methods=["POST"])
@login_required
def hygiene_kit_interest():
    data = request.get_json()
    nom = data.get("nom", "")
    prenom = data.get("prenom", "")
    telephone = data.get("telephone", "")
    email_contact = data.get("email", "")
    formule = data.get("formule", "")
    message = data.get("message", "")
    restaurant = data.get("restaurant", "")
    try:
        import os
        brevo_key = os.environ.get("BREVO_API_KEY", "")
        if brevo_key:
            import requests as _req
            body = (f"Restaurant: {restaurant}\n"
                    f"Nom: {nom} {prenom}\n"
                    f"Telephone: {telephone}\n"
                    f"Email: {email_contact}\n"
                    f"Formule: {formule}\n"
                    f"Message: {message or '(aucun)'}\n\n"
                    f"Envoye depuis l'app Pauco.")
            _req.post("https://api.brevo.com/v3/smtp/email", headers={
                "api-key": brevo_key, "Content-Type": "application/json"
            }, json={
                "sender": {"name": "Pauco App", "email": "contact@paucoandco.com"},
                "to": [{"email": "paul@paucoandco.com", "name": "Paul"}],
                "subject": f"Kit Hygiene - {restaurant} - {prenom} {nom}",
                "textContent": body,
            }, timeout=10)
    except Exception as e:
        print(f"[KIT] Email error: {e}")
    return jsonify({"ok": True})


@app.route("/gestion/hygiene/pms/pdf")
@login_required
def hygiene_pms_pdf():
    """Genere le Plan de Maitrise Sanitaire PDF complet."""
    try:
        from io import BytesIO
        from reportlab.pdfgen import canvas as pdf_canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.colors import HexColor
    except ImportError as e:
        return f"reportlab non installe: {e}", 500

    rid = current_user.restaurant_id
    resto_name, resto_sub = _pdf_resto_header(rid)

    try:
        from dateutil.relativedelta import relativedelta
        cutoff_3m = (datetime.now() - relativedelta(months=3)).strftime("%Y-%m-%d")
    except ImportError:
        from datetime import timedelta as _td
        cutoff_3m = (datetime.now() - _td(days=90)).strftime("%Y-%m-%d")

    try:
        equips = at.get_all("equipements_froid", rid)
        releves = [r for r in at.get_all("releves_temp", rid)
                   if (r.get("Date") or "") >= cutoff_3m]
        releves.sort(key=lambda x: x.get("Date") or "", reverse=True)
        etiquettes = at.get_all("etiquettes_haccp", rid)
        etiquettes.sort(key=lambda x: x.get("Date_Production") or "", reverse=True)
        receptions = [r for r in at.get_all("receptions", rid)
                      if (r.get("Date_Reception") or "") >= cutoff_3m]
        receptions.sort(key=lambda x: x.get("Date_Reception") or "", reverse=True)
        viandes = [v for v in at.get_all("tracabilite_viandes_hygiene", rid)
                   if (v.get("Date_Reception") or "") >= cutoff_3m]
        viandes.sort(key=lambda x: x.get("Date_Reception") or "", reverse=True)
    except Exception as e:
        return f"Erreur Airtable: {e}", 500

    try:
        buf = BytesIO()
        w, h = A4
        c = pdf_canvas.Canvas(buf, pagesize=A4)

        def _s(val):
            """Safe string for PDF — no None, no crash."""
            return str(val) if val is not None else ""

        def _new_page():
            c.showPage()
            return h - 50

        # ── Page 1 : Couverture ──
        c.setFillColor(HexColor("#0F1F14"))
        c.rect(0, h-120, w, 120, fill=True, stroke=False)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("Helvetica-Bold", 22)
        c.drawCentredString(w/2, h-50, "PLAN DE MAITRISE SANITAIRE")
        c.setFont("Helvetica", 14)
        c.drawCentredString(w/2, h-75, _s(resto_name))
        if resto_sub:
            c.setFont("Helvetica", 9)
            c.drawCentredString(w/2, h-90, _s(resto_sub))
        c.setFont("Helvetica", 10)
        c.drawCentredString(w/2, h-108, f"Genere le {datetime.now().strftime('%d/%m/%Y')}  |  Format DGAL")

        y = h - 160
        c.setFillColor(HexColor("#000000"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, y, "SOMMAIRE")
        y -= 20
        c.setFont("Helvetica", 10)
        sommaire = [
            "1. Presentation de l'etablissement",
            "2. Equipements de froid et suivi des temperatures",
            "3. Etiquetage HACCP et gestion des DLC",
            "4. Reception des marchandises",
            "5. Tracabilite des viandes",
            "6. Plan de nettoyage et desinfection",
            "7. Gestion des dechets",
            "8. Lutte contre les nuisibles",
            "9. Plan HACCP - analyse des dangers",
            "10. Actions correctives",
        ]
        for s in sommaire:
            c.drawString(50, y, s)
            y -= 16

        # ── Page 2 : Equipements et releves ──
        y = _new_page()
        c.setFont("Helvetica-Bold", 14)
        c.drawString(30, y, "2. Equipements de froid et suivi des temperatures")
        y -= 25

        c.setFont("Helvetica-Bold", 10)
        c.drawString(30, y, f"Equipements configures ({len(equips)})")
        y -= 16
        c.setFont("Helvetica", 9)
        if equips:
            for eq in equips:
                c.drawString(40, y, f"- {_s(eq.get('Nom'))}  |  {_s(eq.get('Type'))}  |  {_s(eq.get('Frequence') or '1x')}/jour")
                y -= 13
                if y < 50:
                    y = _new_page()
        else:
            c.drawString(40, y, "Aucun equipement configure")
            y -= 13

        y -= 10
        c.setFont("Helvetica-Bold", 10)
        c.drawString(30, y, f"Releves des 3 derniers mois ({len(releves)} enregistrements)")
        y -= 16
        c.setFont("Helvetica", 8)
        non_conf = sum(1 for r in releves if not r.get("Conforme"))
        if non_conf:
            c.setFillColor(HexColor("#991B1B"))
            c.drawString(30, y, f"/!\\ {non_conf} releve(s) hors norme sur {len(releves)} total")
            c.setFillColor(HexColor("#000000"))
            y -= 14
        for r in releves:
            if y < 50:
                y = _new_page()
                c.setFont("Helvetica", 8)
            dt = (_s(r.get("Date")))[:16].replace("T", " ")
            temp = r.get("Temperature")
            temp_s = f"{temp} C" if temp is not None else "-"
            conf = "OK" if r.get("Conforme") else "HORS NORME"
            c.drawString(40, y, f"{dt}  |  {_s(r.get('Equipement'))[:20]}  |  {temp_s}  |  {conf}  |  {_s(r.get('Operateur'))[:15]}")
            y -= 11

        # ── Page 3 : Etiquettes HACCP ──
        y = _new_page()
        c.setFont("Helvetica-Bold", 14)
        c.drawString(30, y, "3. Etiquetage HACCP et gestion des DLC")
        y -= 25
        c.setFont("Helvetica-Bold", 10)
        c.drawString(30, y, f"Etiquettes generees ({len(etiquettes)})")
        y -= 16
        c.setFont("Helvetica", 8)
        for e in etiquettes[:50]:
            if y < 50:
                y = _new_page()
                c.setFont("Helvetica", 8)
            dt = (_s(e.get("Date_Production")))[:10]
            c.drawString(40, y, f"{dt}  |  {_s(e.get('Nom_Plat'))[:25]}  |  Cuisinier: {_s(e.get('Cuisinier'))[:15]}  |  DLC: {_s(e.get('DLC'))}")
            y -= 11

        # ── Page 4 : Receptions marchandises ──
        y = _new_page()
        c.setFont("Helvetica-Bold", 14)
        c.drawString(30, y, "4. Reception des marchandises")
        y -= 25
        c.setFont("Helvetica-Bold", 10)
        non_conf_rec = sum(1 for r in receptions if not r.get("Conforme"))
        c.drawString(30, y, f"Receptions des 3 derniers mois ({len(receptions)})")
        y -= 16
        c.setFont("Helvetica", 8)
        if non_conf_rec:
            c.setFillColor(HexColor("#991B1B"))
            c.drawString(30, y, f"/!\\ {non_conf_rec} reception(s) non conforme(s)")
            c.setFillColor(HexColor("#000000"))
            y -= 14
        for r in receptions:
            if y < 50:
                y = _new_page()
                c.setFont("Helvetica", 8)
            dt = (_s(r.get("Date_Reception")))[:10]
            temp = r.get("Temperature")
            temp_s = f"{temp} C" if temp is not None else "-"
            conf = "OUI" if r.get("Conforme") else "NON"
            c.drawString(40, y, f"{dt}  |  {_s(r.get('Fournisseur'))[:20]}  |  {temp_s}  |  Conforme: {conf}")
            y -= 11

        # ── Page 5 : Tracabilite viandes ──
        y = _new_page()
        c.setFont("Helvetica-Bold", 14)
        c.drawString(30, y, "5. Tracabilite des viandes")
        y -= 25
        c.setFont("Helvetica-Bold", 10)
        c.drawString(30, y, f"Enregistrements des 3 derniers mois ({len(viandes)})")
        y -= 16
        c.setFont("Helvetica", 8)
        for v in viandes:
            if y < 50:
                y = _new_page()
                c.setFont("Helvetica", 8)
            c.drawString(40, y, f"{_s(v.get('Date_Reception'))}  |  {_s(v.get('Description'))[:20]}  |  "
                                f"{_s(v.get('Fournisseur'))[:15]}  |  Origine: {_s(v.get('Origine_Pays'))}  |  Lot: {_s(v.get('Numero_Lot'))}")
            y -= 11

        c.save()
        buf.seek(0)
        fname = f"PMS_{_s(resto_name).replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.pdf"
        return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=fname)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Erreur generation PDF: {e}", 500


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="Europe/Paris")
        scheduler.add_job(send_all_rapports, "cron", hour=9, minute=30, id="rapport_whatsapp")
        scheduler.add_job(_run_backup, "cron", hour=2, minute=0, id="backup_airtable_nuit")
        scheduler.add_job(_run_backup, "cron", hour=14, minute=0, id="backup_airtable_jour")
        scheduler.add_job(_keep_alive_ping, "interval", minutes=1, id="keep_alive")
        scheduler.start()
        _scheduler_started = True
        print("[SCHEDULER] Rapport WhatsApp planifié chaque jour à 9h30")
        print("[SCHEDULER] Backup Airtable planifié 2x/jour : 2h00 et 14h00")
        print("[SCHEDULER] Keep-alive ping toutes les 1 minute")
    except Exception as e:
        print(f"[SCHEDULER] Erreur démarrage: {e}")


if __name__ == "__main__":
    init_db()
    init_auth_tables()
    start_scheduler()
    sync.preload_demo()
    # Log registered routes for debugging
    print("[STARTUP] Routes enregistrees:")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.rule.startswith("/static"):
            continue
        print(f"  {', '.join(rule.methods - {'HEAD','OPTIONS'}):6s} {rule.rule}")
    app.run(host="0.0.0.0", port=5001, debug=True)
