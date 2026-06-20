import os
import sqlite3

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

db = SQLAlchemy()
scheduler = None


def _sync_and_enrich_job():
    """Background job: sync all platforms and enrich missing games."""
    from gigalib.enricher import enrich_game
    from gigalib.models import Game
    from gigalib.platforms import sync_all_platforms

    app = scheduler.app
    with app.app_context():
        try:
            sync_all_platforms()
            unenriched = (
                Game.query.filter((Game.description == None) | (Game.description == ""))
                .limit(50)
                .all()
            )
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
    instance_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance"
    )
    app = Flask(__name__, instance_path=instance_path)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-key")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///gigalib.db"

    db.init_app(app)

    from gigalib.routes import main_bp

    app.register_blueprint(main_bp)

    with app.app_context():
        db.create_all()
        # Auto-migrate: add missing conversation tables and columns
        # Auto-migrate: add missing columns
        db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
        try:
            conn = sqlite3.connect(
                app.instance_path + "/" + db_path
                if not os.path.isabs(db_path)
                else db_path
            )
            cols = [
                row[1] for row in conn.execute("PRAGMA table_info(game)").fetchall()
            ]
            if "is_multiplayer" not in cols:
                conn.execute(
                    "ALTER TABLE game ADD COLUMN is_multiplayer BOOLEAN DEFAULT 0"
                )
                conn.commit()
            if "is_gamepass" not in cols:
                conn.execute(
                    "ALTER TABLE game ADD COLUMN is_gamepass BOOLEAN DEFAULT 0"
                )
                conn.commit()

            conversation_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            # SQLAlchemy's create_all() handles new installs; this is a lightweight guard for old DBs.
            if "conversation" not in conversation_tables:
                conn.execute("""
                    CREATE TABLE conversation (
                        id VARCHAR(36) PRIMARY KEY,
                        title VARCHAR(200) NOT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """)
            if "conversation_message" not in conversation_tables:
                conn.execute("""
                    CREATE TABLE conversation_message (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id VARCHAR(36) NOT NULL,
                        role VARCHAR(20) NOT NULL,
                        content TEXT NOT NULL,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES conversation(id)
                    )
                    """)
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
