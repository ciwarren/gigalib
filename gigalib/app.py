import os
import secrets
import sqlite3
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

db = SQLAlchemy()
scheduler = None
_AUTO_SYNC_LOCK = threading.Lock()
_AUTO_SYNC_STATE = {
    "running": False,
    "last_started_at": None,
}


def _run_sync_and_enrich(app):
    from gigalib.enricher import enrich_game
    from gigalib.models import Friend, Game
    from gigalib.platforms import sync_all_platforms
    from gigalib.social import (SocialServiceError,
                                fetch_remote_friend_library,
                                list_remote_friends,
                                sync_remote_social_snapshot,
                                update_remote_presence)

    with app.app_context():
        try:
            sync_all_platforms()
            games = Game.query.order_by(Game.title.asc()).all()
            try:
                sync_remote_social_snapshot(games)
                update_remote_presence()
                list_remote_friends()
                for friend in Friend.query.order_by(Friend.handle.asc()).all():
                    try:
                        fetch_remote_friend_library(friend.id)
                    except SocialServiceError as exc:
                        app.logger.info(
                            "Friend library startup sync skipped for @%s: %s",
                            friend.handle,
                            exc.message,
                        )
                app.logger.info("Social sync completed")
            except SocialServiceError as exc:
                app.logger.info("Social sync skipped: %s", exc.message)

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
            app.logger.info("Scheduled sync+social+enrich completed")
        except Exception as e:
            app.logger.warning(f"Scheduled sync+social+enrich failed: {e}")


def trigger_open_sync(app, min_interval_seconds=300):
    if os.environ.get("GIGALIB_DISABLE_SCHEDULER") == "1":
        return False

    now = datetime.utcnow()
    with _AUTO_SYNC_LOCK:
        if _AUTO_SYNC_STATE["running"]:
            return False
        last_started_at = _AUTO_SYNC_STATE["last_started_at"]
        if (
            last_started_at
            and (now - last_started_at).total_seconds() < min_interval_seconds
        ):
            return False
        _AUTO_SYNC_STATE["running"] = True
        _AUTO_SYNC_STATE["last_started_at"] = now

    def _worker():
        try:
            _run_sync_and_enrich(app)
        finally:
            with _AUTO_SYNC_LOCK:
                _AUTO_SYNC_STATE["running"] = False

    threading.Thread(target=_worker, name="gigalib-open-sync", daemon=True).start()
    return True


def _sync_and_enrich_job():
    """Background job: sync all platforms, social state, and enrich missing games."""
    app = scheduler.app
    _run_sync_and_enrich(app)


def create_app():
    global scheduler
    instance_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance"
    )
    app = Flask(__name__, instance_path=instance_path)
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        # Avoid a shared static fallback key; use ephemeral key when env is missing.
        secret_key = secrets.token_urlsafe(32)
        app.logger.warning(
            "SECRET_KEY not set; using an ephemeral key for this process."
        )
    app.config["SECRET_KEY"] = secret_key
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
    if os.environ.get("GIGALIB_DISABLE_SCHEDULER") != "1" and (
        os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug
    ):
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.app = app
        scheduler.add_job(_sync_and_enrich_job, "interval", hours=1, id="sync_enrich")
        scheduler.add_job(
            _sync_and_enrich_job,
            "date",
            run_date=datetime.utcnow(),
            id="startup_sync_enrich",
            replace_existing=True,
        )
        scheduler.start()
        app.logger.info(
            "Scheduler started: startup sync plus sync+social+enrich every hour"
        )

    return app
