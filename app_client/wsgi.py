"""
WSGI entrypoint — Module Gestion Pauco (Isolated App Client).
Used by: gunicorn wsgi:app on Railway
"""
import os
from app import app, init_db, start_scheduler
from modules.auth import init_auth_tables
from modules import airtable_sync as sync

# Initialize everything on startup
init_db()
init_auth_tables()
start_scheduler()
