# -*- coding: utf-8 -*-
"""Configuration Flask et imports de l'application Pauco."""

import os
from datetime import timedelta
from flask import Flask
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=os.path.join(_BASE_DIR, "templates"),
            static_folder=os.path.join(_BASE_DIR, "static"))
_flask_secret = os.environ.get("FLASK_SECRET_KEY", "")
if not _flask_secret:
    print("[SESSION] WARNING: FLASK_SECRET_KEY non définie — sessions perdues à chaque redémarrage !")
    _flask_secret = os.urandom(32).hex()
app.secret_key = _flask_secret
app.config["JSON_AS_ASCII"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
# SESSION_COOKIE_DOMAIN volontairement NON appliqué.
# Si cette variable est définie sur une valeur différente du host servi
# (ex. staging.paucoandco.com alors qu'on accède via xxx.up.railway.app
# ou via la démo Pauco), le navigateur rejette le cookie de session →
# le user n'est jamais authentifié → /demo redirige en boucle vers
# /login. Le défaut Flask (cookie scopé au host courant) marche partout.
_cookie_domain_env = os.environ.get("SESSION_COOKIE_DOMAIN")
if _cookie_domain_env:
    print(f"[SESSION] SESSION_COOKIE_DOMAIN={_cookie_domain_env!r} ignoré — cookie scopé automatiquement au host courant pour éviter les login loops")

# Auth setup
from modules.auth import login_manager, init_auth_tables, verify_login, create_user, create_reset_token, validate_reset_token, reset_password, complete_onboarding, invalidate_user_cache, user_type_for_restaurant, User
login_manager.init_app(app)

# Airtable sync
from modules import airtable_client as at
from modules import airtable_sync as sync
from modules.rapport_whatsapp import build_rapport, send_whatsapp, send_all_rapports
