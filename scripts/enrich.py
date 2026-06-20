"""
CLI entry point for enrichment tasks.

Usage:
    uv run python gigalib/enrich.py          # Enrich all games missing data
    uv run python gigalib/enrich.py --full   # Re-enrich everything (reset + refetch)
"""

import sys

from gigalib import create_app, db
from gigalib.enricher import enrich_game
from gigalib.models import Game


def main():
    full_mode = "--full" in sys.argv
    app = create_app()

    with app.app_context():
        if full_mode:
            print("Full re-enrichment: resetting all metadata...")
            games = Game.query.all()
            for g in games:
                g.description = None
                g.critic_rating = None
                g.genre = None
                g.tags = None
                g.rating_tier = None
                g.main_story_hours = None
                g.completionist_hours = None
                g.is_multiplayer = False
            db.session.commit()
        else:
            games = Game.query.filter(
                (Game.description == None) | (Game.description == "")
            ).all()

        total = len(games)
        if total == 0:
            print("All games are already enriched. Use --full to re-enrich.")
            return

        print(f"Enriching {total} games...")
        count = 0
        errors = 0

        for i, g in enumerate(games):
            try:
                if enrich_game(g):
                    count += 1
            except Exception:
                errors += 1

            if (i + 1) % 25 == 0:
                db.session.commit()
                print(f"  [{i+1}/{total}] {count} enriched, {errors} errors")

        db.session.commit()
        print(f"\nDone: {count}/{total} enriched ({errors} errors)")


if __name__ == "__main__":
    main()
