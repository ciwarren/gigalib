from flask import Blueprint, render_template, jsonify, request
from gigalib.models import Game
from gigalib.platforms import sync_all_platforms
from gigalib.assistant import ask_assistant
from gigalib.enricher import enrich_game
from gigalib import db

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    games = Game.query.all()
    platforms = db.session.query(Game.platform).distinct().all()
    platform_list = [p[0] for p in platforms]
    return render_template("index.html", games=games, platforms=platform_list)


@main_bp.route("/sync", methods=["POST"])
def sync_games():
    """Sync games from all connected platforms."""
    results = sync_all_platforms()
    return jsonify(results)


@main_bp.route("/games")
def get_games():
    """Get all games, optionally filtered by platform."""
    platform = request.args.get("platform")
    if platform:
        games = Game.query.filter_by(platform=platform).all()
    else:
        games = Game.query.all()
    return jsonify([g.to_dict() for g in games])


@main_bp.route("/games/add", methods=["POST"])
def add_game():
    """Manually add a game to the library."""
    data = request.get_json()
    title = data.get("title", "").strip()
    platform = data.get("platform", "").strip()
    genre = data.get("genre", "").strip() or None

    if not title or not platform:
        return jsonify({"error": "Title and platform are required"}), 400

    game = Game(title=title, platform=platform, genre=genre)
    db.session.add(game)
    db.session.commit()
    return jsonify(game.to_dict()), 201


@main_bp.route("/enrich", methods=["POST"])
def enrich():
    """Enrich games missing metadata (capped per request to avoid timeout)."""
    games = Game.query.filter(
        (Game.description == None) | (Game.description == "")
    ).limit(10).all()

    enriched_count = 0
    errors = 0
    for g in games:
        try:
            if enrich_game(g):
                enriched_count += 1
        except Exception:
            errors += 1

    db.session.commit()
    remaining = Game.query.filter(
        (Game.description == None) | (Game.description == "")
    ).count()
    return jsonify({
        "enriched": enriched_count,
        "errors": errors,
        "remaining": remaining,
    })


@main_bp.route("/games/<int:game_id>")
def get_game(game_id):
    """Get a single game's full details."""
    game = Game.query.get_or_404(game_id)
    return jsonify(game.to_dict())


@main_bp.route("/games/<int:game_id>/launch", methods=["POST"])
def launch_game(game_id):
    """Launch a game via its platform protocol."""
    import os as _os

    game = Game.query.get_or_404(game_id)
    launch_url = None

    if game.platform == "steam" and game.app_id:
        launch_url = f"steam://rungameid/{game.app_id}"
    elif game.platform == "ea" and game.app_id:
        if game.app_id.startswith("Origin.OFR"):
            launch_url = f"origin2://game/launch?offerIds={game.app_id}"
        else:
            # GUID-based older Origin games
            launch_url = f"link2ea://launchgame/{game.app_id}"
    elif game.platform == "ubisoft" and game.app_id:
        launch_url = f"uplay://launch/{game.app_id}"

    if not launch_url:
        return jsonify({"error": "No launch URL available for this game"}), 400

    try:
        _os.startfile(launch_url)
        return jsonify({"status": "launched", "url": launch_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@main_bp.route("/assistant", methods=["POST"])
def assistant():
    """Interactive chat with the AI assistant."""
    data = request.get_json()
    message = data.get("message", "")
    history = data.get("history", [])
    games = Game.query.all()
    response = ask_assistant(message, games, history=history)
    return jsonify({"response": response})
