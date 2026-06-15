import os
import sqlite3

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()

db = SQLAlchemy()
scheduler = None


def _sync_and_enrich_job():
    """Background job: sync all platforms and enrich missing games."""
    from gigalib.platforms import sync_all_platforms
    from gigalib.enricher import enrich_game
    from gigalib.models import Game

    app = scheduler.app
    with app.app_context():
        try:
            sync_all_platforms()
            unenriched = Game.query.filter(
                (Game.description == None) | (Game.description == "")
            ).limit(50).all()
            for g in unenriched:
                try:
                    enrich_game(g)
                except Exception:
                    continue
            db.session.commit()
            app.logger.info("Scheduled sync+enrich completed")
        except Exception as e:
            app.logger.warning(f"Scheduled sync+enrich failed: {e}")


def create_app():
    global scheduler
    instance_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance")
    app = Flask(__name__, instance_path=instance_path)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-key")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///gigalib.db"

    db.init_app(app)

    from gigalib.routes import main_bp
    app.register_blueprint(main_bp)

    with app.app_context():
        db.create_all()
        # Auto-migrate: add missing columns
        db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
        try:
            conn = sqlite3.connect(app.instance_path + "/" + db_path if not os.path.isabs(db_path) else db_path)
            cols = [row[1] for row in conn.execute("PRAGMA table_info(game)").fetchall()]
            if "is_multiplayer" not in cols:
                conn.execute("ALTER TABLE game ADD COLUMN is_multiplayer BOOLEAN DEFAULT 0")
                conn.commit()
            conn.close()
        except Exception:
            pass

    # Start hourly scheduler (only in the main process, not the reloader)
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.app = app
        scheduler.add_job(_sync_and_enrich_job, "interval", hours=1, id="sync_enrich")
        scheduler.start()
        app.logger.info("Scheduler started: sync+enrich every hour")

    return app
