"""
Backfill : remet tous les numéros de téléphone Airtable au format français
'XX XX XX XX XX'. À lancer une seule fois après déploiement.

Tables / champs traités :
  - restaurants    : Téléphone
  - employes       : Téléphone
  - fournisseurs   : Telephone, Telephone_commercial
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import airtable_client as at
from modules.airtable_client import normaliser_telephone

TARGETS = [
    ("restaurants",  ["Téléphone"]),
    ("employes",     ["Téléphone"]),
    ("fournisseurs", ["Telephone", "Telephone_commercial"]),
]


def run():
    total_checked = 0
    total_updated = 0
    for table, fields in TARGETS:
        try:
            rows = at.get_all(table)
        except Exception as e:
            print(f"[SKIP] {table}: {e}")
            continue
        for r in rows:
            total_checked += 1
            patch = {}
            for f in fields:
                old = r.get(f, "")
                if not old or not isinstance(old, str):
                    continue
                new = normaliser_telephone(old)
                if new and new != old:
                    patch[f] = new
            if patch:
                try:
                    at.update(table, r["id"], patch)
                    total_updated += 1
                    print(f"[OK] {table}/{r['id']}: {patch}")
                except Exception as e:
                    print(f"[ERR] {table}/{r['id']}: {e}")
        at.invalidate_cache(table)
    print(f"\nDone — {total_updated}/{total_checked} records updated.")


if __name__ == "__main__":
    run()
