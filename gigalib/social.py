import json
import os
import re
from datetime import datetime, timezone

import requests

from gigalib import db
from gigalib.models import (AccountLink, Friend, RemoteLibrarySnapshot,
                            SocialPrivacySettings)

SNAPSHOT_VERSION = 1
DEFAULT_SERVICE_URL = os.getenv("GIGALIB_SOCIAL_URL", "http://api.gigalib.uk:8081")
REQUEST_TIMEOUT = 8


class SocialServiceError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_account_link():
    return AccountLink.query.order_by(AccountLink.id.asc()).first()


def get_or_create_privacy_settings():
    settings = SocialPrivacySettings.query.order_by(
        SocialPrivacySettings.id.asc()
    ).first()
    if settings:
        return settings

    settings = SocialPrivacySettings()
    db.session.add(settings)
    db.session.commit()
    return settings


def save_local_account(handle, service_url=None):
    account = get_account_link()
    if not account:
        account = AccountLink(created_at=utc_now())
        db.session.add(account)

    account.handle = handle
    account.display_name = handle
    account.service_url = service_url or DEFAULT_SERVICE_URL
    account.updated_at = utc_now()
    db.session.commit()
    return account


def _service_url(account=None, service_url=None):
    base_url = (
        service_url or (account.service_url if account else None) or DEFAULT_SERVICE_URL
    )
    return base_url.rstrip("/")


def _remote_request(method, path, account=None, service_url=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if account and account.access_token:
        headers["Authorization"] = f"Bearer {account.access_token}"
    headers.setdefault("Accept", "application/json")

    try:
        response = requests.request(
            method,
            f"{_service_url(account, service_url)}{path}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise SocialServiceError(f"Social service unavailable: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"error": _clean_remote_error(response.text, response.reason)}

    if response.status_code >= 400:
        raise SocialServiceError(
            payload.get("error") or response.reason,
            status_code=response.status_code,
        )
    return payload


def _clean_remote_error(text, fallback):
    if not text:
        return fallback or "Social service request failed"
    title = re.search(r"<h1>(.*?)</h1>", text, flags=re.IGNORECASE | re.DOTALL)
    detail = re.search(r"<p>(.*?)</p>", text, flags=re.IGNORECASE | re.DOTALL)
    if title:
        parts = [re.sub(r"\s+", " ", title.group(1)).strip()]
        if detail:
            parts.append(re.sub(r"\s+", " ", detail.group(1)).strip())
        return ": ".join(part for part in parts if part)
    return re.sub(r"\s+", " ", text).strip()[:240]


def _require_connected_account():
    account = get_account_link()
    if not account or not account.access_token:
        raise SocialServiceError("Save a social handle before using this feature", 400)
    return account


def _save_remote_account(user, access_token, service_url=None):
    account = save_local_account(user["handle"], service_url=service_url)
    account.remote_user_id = str(user["id"])
    account.access_token = access_token
    account.updated_at = utc_now()
    db.session.commit()
    return account


def register_remote_account(handle, password, service_url=None):
    payload = _remote_request(
        "POST",
        "/v1/auth/register",
        service_url=service_url,
        json={"handle": handle, "password": password},
    )
    return _save_remote_account(payload["user"], payload["access_token"], service_url)


def login_remote_account(handle, password, service_url=None):
    payload = _remote_request(
        "POST",
        "/v1/auth/login",
        service_url=service_url,
        json={"handle": handle, "password": password},
    )
    return _save_remote_account(payload["user"], payload["access_token"], service_url)


def _cache_friend(user, last_library_sync_at=None):
    remote_user_id = str(user["id"])
    friend = Friend.query.filter_by(remote_user_id=remote_user_id).first()
    if not friend:
        friend = Friend(
            remote_user_id=remote_user_id,
            handle=user["handle"],
            display_name=user["handle"],
        )
        db.session.add(friend)

    friend.handle = user["handle"]
    friend.display_name = user["handle"]
    friend.friendship_status = "accepted"
    if last_library_sync_at:
        friend.last_library_sync_at = _parse_iso_datetime(last_library_sync_at)
    friend.updated_at = utc_now()
    db.session.commit()
    return friend


def _parse_iso_datetime(value):
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def list_remote_friends():
    account = _require_connected_account()
    payload = _remote_request("GET", "/v1/friends", account=account)
    friends = []
    for item in payload:
        friend = _cache_friend(item["user"]).to_dict()
        friend["is_online"] = bool(item.get("user", {}).get("is_online"))
        friend["last_seen_at"] = item.get("user", {}).get("last_seen_at")
        friends.append(friend)
    return friends


def update_remote_presence():
    account = _require_connected_account()
    return _remote_request("POST", "/v1/presence", account=account)


def list_remote_messages(friend_id):
    account = _require_connected_account()
    friend = db.session.get(Friend, friend_id)
    if not friend:
        raise SocialServiceError("Friend not found", 404)
    return _remote_request(
        "GET",
        "/v1/messages",
        account=account,
        params={"friend_id": friend.remote_user_id},
    )


def send_remote_message(friend_id, body):
    account = _require_connected_account()
    friend = db.session.get(Friend, friend_id)
    if not friend:
        raise SocialServiceError("Friend not found", 404)
    return _remote_request(
        "POST",
        "/v1/messages",
        account=account,
        json={"friend_id": friend.remote_user_id, "body": body},
    )


def list_remote_friend_requests():
    account = _require_connected_account()
    return _remote_request("GET", "/v1/friend-requests", account=account)


def send_remote_friend_request(handle):
    account = _require_connected_account()
    return _remote_request(
        "POST",
        "/v1/friend-requests",
        account=account,
        json={"handle": handle},
    )


def accept_remote_friend_request(request_id):
    account = _require_connected_account()
    payload = _remote_request(
        "POST",
        f"/v1/friend-requests/{request_id}/accept",
        account=account,
    )
    return _cache_friend(payload["user"]).to_dict()


def decline_remote_friend_request(request_id):
    account = _require_connected_account()
    return _remote_request(
        "POST",
        f"/v1/friend-requests/{request_id}/decline",
        account=account,
    )


def search_remote_users(query):
    account = _require_connected_account()
    return _remote_request(
        "GET", "/v1/users/search", account=account, params={"q": query}
    )


def sync_remote_social_snapshot(games):
    account = _require_connected_account()
    settings = get_or_create_privacy_settings()
    snapshot = build_library_snapshot(games, settings)
    payload = _remote_request(
        "PUT",
        "/v1/library/snapshot",
        account=account,
        json=snapshot,
    )
    account.last_sync_at = utc_now()
    account.updated_at = utc_now()
    db.session.commit()
    payload.update(
        {
            "account": account.to_dict(),
            "mode": "remote",
            "message": "Synced your privacy-filtered library snapshot to the social service.",
        }
    )
    return payload


def fetch_remote_friend_library(friend_id):
    account = _require_connected_account()
    friend = db.session.get(Friend, friend_id)
    if not friend:
        raise SocialServiceError("Friend not found", 404)

    payload = _remote_request(
        "GET",
        f"/v1/friends/{friend.remote_user_id}/library",
        account=account,
    )
    snapshot = payload["snapshot"]
    record = RemoteLibrarySnapshot.query.filter_by(
        remote_user_id=friend.remote_user_id
    ).first()
    if not record:
        record = RemoteLibrarySnapshot(
            remote_user_id=friend.remote_user_id,
            snapshot_json=json.dumps(snapshot, separators=(",", ":")),
        )
        db.session.add(record)
    else:
        record.snapshot_json = json.dumps(snapshot, separators=(",", ":"))
        record.fetched_at = utc_now()
    record.snapshot_version = int(snapshot.get("snapshot_version") or 1)
    record.library_synced_at = _parse_iso_datetime(payload.get("synced_at"))
    friend.last_library_sync_at = record.library_synced_at
    db.session.commit()
    return {
        "friend": friend.to_dict(),
        "snapshot": snapshot,
        "canonical_titles": [
            item.get("canonical_title")
            for item in snapshot.get("games", [])
            if item.get("canonical_title")
        ],
        "display_titles": [
            item.get("display_title")
            for item in snapshot.get("games", [])
            if item.get("display_title")
        ],
    }


def compare_remote_friends(friend_ids):
    account = _require_connected_account()
    friends = Friend.query.filter(Friend.id.in_(friend_ids)).all()
    if not friends:
        raise SocialServiceError("Friend not found", 404)
    return _remote_request(
        "POST",
        "/v1/compare",
        account=account,
        json={"handles": [friend.handle for friend in friends]},
    )


def update_privacy_settings(data):
    settings = get_or_create_privacy_settings()
    settings.share_playtime = bool(data.get("share_playtime", False))
    settings.share_last_played = bool(data.get("share_last_played", False))
    visibility = data.get("visibility") or "friends"
    settings.visibility = (
        visibility if visibility in {"private", "friends", "public"} else "friends"
    )
    settings.updated_at = utc_now()
    db.session.commit()
    return settings


def canonical_title(title):
    text = (title or "").lower()
    text = re.sub(
        r"\b(game of the year|goty|complete|deluxe|ultimate|definitive)\b", " ", text
    )
    text = re.sub(r"\bedition\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def split_tags(tags):
    if not tags:
        return []
    return [tag.strip() for tag in tags.split(",") if tag.strip()]


def _empty_snapshot_game(game):
    return {
        "canonical_title": canonical_title(game.title),
        "display_title": game.title,
        "platforms": [],
        "is_installed": False,
        "is_multiplayer": False,
        "playtime_hours": None,
        "last_played": None,
        "genre": game.genre,
        "tags": split_tags(game.tags),
        "critic_rating": game.critic_rating,
        "rating_tier": game.rating_tier,
        "main_story_hours": game.main_story_hours,
        "completionist_hours": game.completionist_hours,
        "external_ids": {},
    }


def build_library_snapshot(games, privacy_settings=None):
    settings = privacy_settings or get_or_create_privacy_settings()
    generated_at = datetime.now(timezone.utc).isoformat()
    by_title = {}

    for game in games:
        key = canonical_title(game.title)
        if not key:
            continue

        item = by_title.setdefault(key, _empty_snapshot_game(game))
        platform = (game.platform or "").lower()
        if platform and platform not in item["platforms"]:
            item["platforms"].append(platform)
        if game.app_id and platform:
            item["external_ids"][platform] = game.app_id

        item["is_installed"] = item["is_installed"] or bool(game.is_installed)
        item["is_multiplayer"] = item["is_multiplayer"] or bool(game.is_multiplayer)
        if game.critic_rating and not item["critic_rating"]:
            item["critic_rating"] = game.critic_rating
        if game.rating_tier and not item["rating_tier"]:
            item["rating_tier"] = game.rating_tier

        if settings.share_playtime:
            item["playtime_hours"] = round(
                (item["playtime_hours"] or 0) + (game.playtime_hours or 0), 1
            )
        if settings.share_last_played and game.last_played:
            item["last_played"] = game.last_played

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "generated_at": generated_at,
        "privacy": settings.to_dict(),
        "games": sorted(
            by_title.values(), key=lambda item: item["display_title"].lower()
        ),
    }


def sync_local_social_snapshot(games):
    account = get_account_link()
    settings = get_or_create_privacy_settings()
    snapshot = build_library_snapshot(games, settings)
    synced_at = utc_now()

    if account:
        account.last_sync_at = synced_at
        account.updated_at = synced_at
        db.session.commit()

    return {
        "account": account.to_dict() if account else None,
        "snapshot": snapshot,
        "game_count": len(snapshot["games"]),
        "synced_at": synced_at.isoformat(),
        "mode": "local-preview",
        "message": "Generated a privacy-filtered local snapshot. Central sync is not configured yet.",
    }


def social_overview(games):
    account = get_account_link()
    settings = get_or_create_privacy_settings()
    if account and account.access_token:
        try:
            friends_payload = list_remote_friends()
        except SocialServiceError:
            friends_payload = None
    else:
        friends_payload = None
    friends = Friend.query.order_by(Friend.handle.asc()).all()
    snapshot = build_library_snapshot(games, settings)
    return {
        "account": account.to_dict() if account else None,
        "privacy": settings.to_dict(),
        "friends": (
            friends_payload
            if friends_payload is not None
            else [friend.to_dict() for friend in friends]
        ),
        "snapshot_preview": {
            "game_count": len(snapshot["games"]),
            "generated_at": snapshot["generated_at"],
            "sample_games": snapshot["games"][:5],
        },
        "central_configured": bool(
            account and account.service_url and account.access_token
        ),
        "default_service_url": DEFAULT_SERVICE_URL,
    }


def compare_with_friend(friend_id, local_games):
    friend = db.session.get(Friend, friend_id)
    if not friend:
        return None

    remote_snapshot = RemoteLibrarySnapshot.query.filter_by(
        remote_user_id=friend.remote_user_id
    ).first()
    if not remote_snapshot:
        return {
            "friend": friend.to_dict(),
            "shared_games": [],
            "shared_installed_games": [],
            "shared_multiplayer_games": [],
            "your_only_games": [],
            "friend_only_games": [],
            "best_candidates": [],
            "message": "No cached library snapshot is available for this friend yet.",
        }

    local_snapshot = build_library_snapshot(local_games)
    remote = json.loads(remote_snapshot.snapshot_json)
    local_by_title = {item["canonical_title"]: item for item in local_snapshot["games"]}
    friend_by_title = {
        item["canonical_title"]: item for item in remote.get("games", [])
    }
    shared_keys = sorted(set(local_by_title).intersection(friend_by_title))

    shared_games = [local_by_title[key]["display_title"] for key in shared_keys]
    shared_installed = [
        local_by_title[key]["display_title"]
        for key in shared_keys
        if local_by_title[key]["is_installed"]
        and friend_by_title[key].get("is_installed")
    ]
    shared_multiplayer = [
        local_by_title[key]["display_title"]
        for key in shared_keys
        if local_by_title[key]["is_multiplayer"]
        or friend_by_title[key].get("is_multiplayer")
    ]

    best_candidates = []
    for key in shared_keys:
        local_item = local_by_title[key]
        friend_item = friend_by_title[key]
        score = 0
        score += (
            3 if local_item["is_installed"] and friend_item.get("is_installed") else 0
        )
        score += (
            2
            if local_item["is_multiplayer"] or friend_item.get("is_multiplayer")
            else 0
        )
        score += (
            local_item.get("critic_rating") or friend_item.get("critic_rating") or 70
        ) / 50
        best_candidates.append(
            {
                "title": local_item["display_title"],
                "score": round(score, 2),
                "reason": "Both own it"
                + (
                    ", both have it installed"
                    if local_item["is_installed"] and friend_item.get("is_installed")
                    else ""
                )
                + (
                    ", and it looks multiplayer-friendly"
                    if local_item["is_multiplayer"] or friend_item.get("is_multiplayer")
                    else ""
                ),
            }
        )

    best_candidates.sort(key=lambda item: item["score"], reverse=True)
    return {
        "friend": friend.to_dict(),
        "shared_games": shared_games,
        "shared_installed_games": shared_installed,
        "shared_multiplayer_games": shared_multiplayer,
        "your_only_games": [
            local_by_title[key]["display_title"]
            for key in sorted(set(local_by_title).difference(friend_by_title))
        ],
        "friend_only_games": [
            friend_by_title[key]["display_title"]
            for key in sorted(set(friend_by_title).difference(local_by_title))
        ],
        "best_candidates": best_candidates[:5],
        "message": "Comparison generated from cached local and friend snapshots.",
    }


def compare_with_friends(friend_ids, local_games):
    friends = Friend.query.filter(Friend.id.in_(friend_ids)).all()
    if not friends:
        return None

    local_snapshot = build_library_snapshot(local_games)
    library_maps = [
        {
            "name": "You",
            "games": {
                item["canonical_title"]: item for item in local_snapshot["games"]
            },
        }
    ]
    missing = []

    for friend in friends:
        remote_snapshot = RemoteLibrarySnapshot.query.filter_by(
            remote_user_id=friend.remote_user_id
        ).first()
        if not remote_snapshot:
            missing.append(friend.to_dict())
            continue

        remote = json.loads(remote_snapshot.snapshot_json)
        library_maps.append(
            {
                "name": friend.handle,
                "games": {
                    item["canonical_title"]: item for item in remote.get("games", [])
                },
            }
        )

    if len(library_maps) == 1:
        return {
            "friends": [friend.to_dict() for friend in friends],
            "missing_snapshots": missing,
            "shared_games": [],
            "shared_installed_games": [],
            "shared_multiplayer_games": [],
            "best_candidates": [],
            "message": "No cached friend snapshots are available for this group yet.",
        }

    shared_keys = set(library_maps[0]["games"])
    for library in library_maps[1:]:
        shared_keys = shared_keys.intersection(library["games"])

    shared_keys = sorted(shared_keys)
    local_games_by_title = library_maps[0]["games"]

    shared_installed = []
    shared_multiplayer = []
    best_candidates = []
    for key in shared_keys:
        items = [library["games"][key] for library in library_maps]
        display_title = local_games_by_title[key]["display_title"]
        all_installed = all(item.get("is_installed") for item in items)
        any_multiplayer = any(item.get("is_multiplayer") for item in items)

        if all_installed:
            shared_installed.append(display_title)
        if any_multiplayer:
            shared_multiplayer.append(display_title)

        rating = next(
            (item.get("critic_rating") for item in items if item.get("critic_rating")),
            70,
        )
        score = (
            (3 if all_installed else 0) + (2 if any_multiplayer else 0) + rating / 50
        )
        best_candidates.append(
            {
                "title": display_title,
                "score": round(score, 2),
                "reason": "Everyone owns it"
                + (", everyone has it installed" if all_installed else "")
                + (", and it looks multiplayer-friendly" if any_multiplayer else ""),
            }
        )

    best_candidates.sort(key=lambda item: item["score"], reverse=True)
    return {
        "friends": [friend.to_dict() for friend in friends],
        "missing_snapshots": missing,
        "shared_games": [
            local_games_by_title[key]["display_title"] for key in shared_keys
        ],
        "shared_installed_games": shared_installed,
        "shared_multiplayer_games": shared_multiplayer,
        "best_candidates": best_candidates[:5],
        "message": "Group comparison generated from cached local and friend snapshots.",
    }
