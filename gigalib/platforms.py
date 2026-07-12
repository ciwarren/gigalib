import hashlib
import json
import os
import re
import winreg
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
import yaml
from Crypto.Cipher import AES

from gigalib import db
from gigalib.models import Game


def _load_platform_config():
    """Load platform paths from platforms.yaml.

    ``platforms.yaml`` is git-ignored and generated per-machine by
    ``scripts/setup.ps1`` (which seeds it from ``platforms.example.yaml`` and
    then appends locally discovered install paths). We deliberately do *not*
    silently fall back to the example: running with unreviewed defaults would
    hide the fact that setup was never run and lead to confusing sync results.
    """
    root = Path(__file__).parent.parent
    config_path = root / "platforms.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} not found. Run scripts/setup.ps1 to generate it "
            f"from platforms.example.yaml, or copy the example manually and "
            f"edit it to match your install locations."
        )
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


_CONFIG = _load_platform_config()


def _get_paths(platform, key):
    """Get list of Path objects for a platform config key. Skips non-existent paths."""
    raw = _CONFIG.get(platform, {}).get(key, [])
    if isinstance(raw, str):
        raw = [raw]
    return [Path(p) for p in raw if Path(p).exists()]


def _get_installed_steam_appids():
    """Get set of appids currently installed by scanning appmanifest files."""
    steamapps_dirs = []
    for steam_path in _get_paths("steam", "paths"):
        vdf = steam_path / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            text = vdf.read_text(errors="ignore")
            paths = re.findall(r'"path"\s+"([^"]+)"', text)
            for p in paths:
                steamapps_dirs.append(Path(p) / "steamapps")
        else:
            steamapps_dirs.append(steam_path / "steamapps")

    installed = set()
    for d in steamapps_dirs:
        if d.exists():
            for f in d.glob("appmanifest_*.acf"):
                installed.add(f.stem.replace("appmanifest_", ""))
    return installed


# Known EA game image mappings (Origin CDN doesn't have a public pattern)
_EA_IMAGES = {
    "Battlefield 1": "https://media.contentapi.ea.com/content/dam/battlefield/battlefield-1/hero/bf1-hero-large.jpg",
    "Battlefield 2042": "https://media.contentapi.ea.com/content/dam/battlefield/battlefield-2042/common/bf2042-background.jpg",
    "Battlefield 6": "https://media.contentapi.ea.com/content/dam/battlefield/battlefield-2042/common/bf2042-background.jpg",
    "Dead Space": "https://media.contentapi.ea.com/content/dam/eacom/dead-space/common/deadspace-hero-large.jpg",
    "Dead Space (2023)": "https://media.contentapi.ea.com/content/dam/eacom/dead-space/common/deadspace-hero-large.jpg",
    "Dead Space 2": "https://media.contentapi.ea.com/content/dam/eacom/dead-space/common/deadspace-hero-large.jpg",
    "Dead Space 3": "https://media.contentapi.ea.com/content/dam/eacom/dead-space/common/deadspace-hero-large.jpg",
    "Madden NFL 24": "https://media.contentapi.ea.com/content/dam/eacom/madden-nfl/madden-24/common/gameplay-redesign-hero-lg.jpg",
    "Madden NFL 25": "https://media.contentapi.ea.com/content/dam/eacom/madden-nfl/madden-25/common/madden-25-hero-lg.jpg",
    "Madden NFL 26": "https://media.contentapi.ea.com/content/dam/eacom/madden-nfl/madden-25/common/madden-25-hero-lg.jpg",
    "SIM CITY 3000 UNLIMITED": "https://media.contentapi.ea.com/content/dam/eacom/SIMCITY/hero-large.jpg",
}


def _get_ea_image_url(title):
    """Get an image URL for an EA game, falling back to IGDB cover during enrichment."""
    return _EA_IMAGES.get(title, "")


def _hours_from_minutes(minutes):
    if minutes is None:
        return None
    try:
        return round(float(minutes) / 60.0, 1)
    except (TypeError, ValueError):
        return None


def _fetch_xbl_playtime_minutes(title_ids):
    """Return ``{title_id: minutes}`` from Xbox Live, or ``{}`` on any failure.

    Isolated behind a lazy import so a missing ``xbox-webapi`` install (or a
    machine that hasn't run the one-time login) never breaks ``sync_xbox()``.
    """
    if not title_ids:
        return {}
    try:
        from gigalib import xbox_stats
    except ImportError:
        return {}
    try:
        return xbox_stats.fetch_playtime_minutes(title_ids)
    except Exception:  # pragma: no cover - defensive
        return {}


def _xbox_title_id(item):
    return str(
        item.get("titleId")
        or item.get("id")
        or item.get("siglId")
        or item.get("productId")
        or ""
    )


def _xbox_title_name(item):
    return (
        item.get("name")
        or item.get("title")
        or item.get("ProductTitle")
        or item.get("LocalizedProperties", [{}])[0].get("ProductTitle")
        or "Unknown Xbox Game"
    )


def _xbox_image_url(item):
    return (
        item.get("displayImage")
        or item.get("displayImageUrl")
        or item.get("displayImageUri")
        or item.get("imageUrl")
        or item.get("thumbnailUrl")
        or ""
    )


def _batch_items(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _microsoft_store_image_url(product):
    localized = (product.get("LocalizedProperties") or [{}])[0]
    images = localized.get("Images") or []
    preferred_purposes = (
        "BoxArt",
        "Poster",
        "BrandedKeyArt",
        "FeaturePromotionalSquareArt",
        "SuperHeroArt",
        "TitledHeroArt",
    )

    for purpose in preferred_purposes:
        for image in images:
            if image.get("ImagePurpose") == purpose and image.get("Uri"):
                uri = image["Uri"]
                return f"https:{uri}" if uri.startswith("//") else uri

    for image in images:
        if image.get("Uri"):
            uri = image["Uri"]
            return f"https:{uri}" if uri.startswith("//") else uri

    return ""


def _hydrate_xbox_catalog_items(items):
    product_ids = [
        _xbox_title_id(item)
        for item in items
        if _xbox_title_id(item) and not _xbox_title_name(item).startswith("Unknown")
    ]
    id_only_items = [
        item
        for item in items
        if _xbox_title_id(item) and _xbox_title_name(item).startswith("Unknown")
    ]
    product_ids.extend(_xbox_title_id(item) for item in id_only_items)
    product_ids = list(dict.fromkeys(product_ids))

    hydrated_by_id = {}
    for batch in _batch_items(product_ids, 50):
        url = "https://displaycatalog.mp.microsoft.com/v7.0/products"
        params = {
            "bigIds": ",".join(batch),
            "market": "US",
            "languages": "en-us",
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            continue

        for product in resp.json().get("Products", []):
            product_id = product.get("ProductId")
            localized = (product.get("LocalizedProperties") or [{}])[0]
            title = (
                localized.get("ProductTitle")
                or localized.get("ShortTitle")
                or localized.get("SortTitle")
            )
            if not product_id or not title:
                continue
            hydrated_by_id[product_id] = {
                "id": product_id,
                "title": title,
                "imageUrl": _microsoft_store_image_url(product),
            }

    hydrated_items = []
    for item in items:
        title_id = _xbox_title_id(item)
        hydrated = hydrated_by_id.get(title_id)
        if hydrated:
            merged = dict(item)
            merged.update({key: value for key, value in hydrated.items() if value})
            hydrated_items.append(merged)
        else:
            hydrated_items.append(item)

    return hydrated_items


def _read_microsoft_game_config(config_path):
    """Extract StoreId / TitleId / display name from a MicrosoftGame.config XML file.

    Returns a dict with normalized identifiers (all lowercase) or None on failure.
    """
    try:
        root = ET.parse(config_path).getroot()
    except (ET.ParseError, OSError):
        return None

    store_id = (root.findtext("StoreId") or "").strip()
    title_id_hex = (root.findtext("TitleId") or "").strip()

    # OpenXBL returns decimal titleIds; MicrosoftGame.config stores hex (e.g. "0x00007D...").
    title_id_dec = ""
    if title_id_hex:
        try:
            title_id_dec = str(int(title_id_hex, 16))
        except ValueError:
            pass

    shell = root.find("ShellVisuals")
    display_name = shell.get("DefaultDisplayName", "").strip() if shell is not None else ""

    return {
        "store_id": store_id,
        "title_id_dec": title_id_dec,
        "title_id_hex": title_id_hex,
        "display_name": display_name,
    }


def _get_installed_xbox_identifiers():
    """Scan configured Xbox install dirs and return a set of lowercased identifiers.

    Each installed game contributes any of: StoreId, decimal TitleId, install
    folder name, and DefaultDisplayName from MicrosoftGame.config. Matching
    against catalog/history responses is done case-insensitively.
    """
    identifiers = set()
    for install_dir in _get_paths("xbox", "install_dirs"):
        for game_dir in install_dir.iterdir():
            if not game_dir.is_dir():
                continue
            # Folder name is a decent fallback title match.
            identifiers.add(game_dir.name.strip().lower())

            for candidate in (
                game_dir / "Content" / "MicrosoftGame.config",
                game_dir / "MicrosoftGame.config",
            ):
                if not candidate.exists():
                    continue
                info = _read_microsoft_game_config(candidate)
                if info:
                    if info["store_id"]:
                        identifiers.add(info["store_id"].lower())
                    if info["title_id_dec"]:
                        identifiers.add(info["title_id_dec"].lower())
                    if info["display_name"]:
                        identifiers.add(info["display_name"].lower())
                break
    return identifiers


def _xbox_is_installed(title_id, title_name, installed_ids):
    if not installed_ids:
        return False
    if title_id and str(title_id).lower() in installed_ids:
        return True
    if title_name and title_name.strip().lower() in installed_ids:
        return True
    return False


_XBOX_COLLECTION_TITLES = {
    "all console games",
}


def _is_xbox_collection_title(title):
    return (title or "").strip().lower() in _XBOX_COLLECTION_TITLES


def _sync_xbox_game_record(
    title_id,
    title_name,
    image_url="",
    last_played="",
    playtime_hours=None,
    is_gamepass=False,
    is_installed=False,
):
    if _is_xbox_collection_title(title_name):
        return False

    existing = Game.query.filter_by(platform="xbox", app_id=title_id).first()

    if not existing:
        db.session.add(
            Game(
                title=title_name,
                platform="xbox",
                app_id=title_id,
                image_url=image_url,
                last_played=last_played,
                playtime_hours=playtime_hours,
                is_gamepass=is_gamepass,
                is_installed=is_installed,
            )
        )
        return True

    if title_name and (existing.title == "Unknown Xbox Game" or not existing.title):
        existing.title = title_name
    if image_url and not existing.image_url:
        existing.image_url = image_url
    if last_played:
        existing.last_played = last_played
    if playtime_hours is not None:
        existing.playtime_hours = playtime_hours
    existing.is_gamepass = is_gamepass or bool(existing.is_gamepass)
    # Always recompute install state from the current local scan so uninstalls
    # are reflected on the next sync.
    existing.is_installed = is_installed
    return False


def sync_all_platforms():
    """Sync games from all configured platforms."""
    results = {}

    if os.getenv("STEAM_API_KEY") and os.getenv("STEAM_USER_ID"):
        results["steam"] = sync_steam()
    else:
        results["steam"] = {"status": "skipped", "reason": "No API key configured"}

    if os.getenv("XBOX_API_KEY"):
        results["xbox"] = sync_xbox()
    else:
        results["xbox"] = {"status": "skipped", "reason": "No API key configured"}

    results["ea"] = sync_ea_local()
    results["ubisoft"] = sync_ubisoft_local()

    return results


def sync_steam():
    """Fetch owned games from Steam Web API."""
    api_key = os.getenv("STEAM_API_KEY")
    steam_id = os.getenv("STEAM_USER_ID")

    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": True,
        "include_played_free_games": True,
        "format": "json",
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        games = data.get("response", {}).get("games", [])
        installed_appids = _get_installed_steam_appids()
        added = 0

        for game in games:
            appid = str(game["appid"])
            existing = Game.query.filter_by(platform="steam", app_id=appid).first()

            if not existing:
                new_game = Game(
                    title=game.get("name", "Unknown"),
                    platform="steam",
                    app_id=appid,
                    image_url=f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
                    playtime_hours=round(game.get("playtime_forever", 0) / 60, 1),
                    is_installed=appid in installed_appids,
                )
                db.session.add(new_game)
                added += 1
            else:
                existing.playtime_hours = round(game.get("playtime_forever", 0) / 60, 1)
                existing.is_installed = appid in installed_appids

        db.session.commit()
        return {"status": "ok", "total": len(games), "added": added}

    except requests.RequestException as e:
        return {"status": "error", "reason": str(e)}


def sync_xbox():
    """Fetch games from Xbox/Microsoft API via OpenXBL or similar service."""
    api_key = os.getenv("XBOX_API_KEY")

    headers = {"X-Authorization": api_key}
    catalog_url = "https://api.xbl.io/v2/gamepass/all"
    history_url = "https://api.xbl.io/v2/titles"

    try:
        catalog_resp = requests.get(catalog_url, headers=headers, timeout=30)
        catalog_resp.raise_for_status()
        catalog_data = catalog_resp.json()

        history_resp = requests.get(history_url, headers=headers, timeout=30)
        history_resp.raise_for_status()
        history_data = history_resp.json()

        catalog_titles = (
            catalog_data.get("content", catalog_data.get("titles", [])) or []
        )
        history_titles = (
            history_data.get("content", {}).get(
                "titles", history_data.get("titles", [])
            )
            or []
        )
        catalog_titles = _hydrate_xbox_catalog_items(catalog_titles)

        installed_ids = _get_installed_xbox_identifiers()

        added = 0
        skipped_catalog = 0
        matched_history = set()

        for item in catalog_titles:
            title_id = _xbox_title_id(item)
            if not title_id:
                continue
            title_name = _xbox_title_name(item)
            if title_name == "Unknown Xbox Game":
                skipped_catalog += 1
                continue
            image_url = _xbox_image_url(item)
            if _sync_xbox_game_record(
                title_id=title_id,
                title_name=title_name,
                image_url=image_url,
                is_gamepass=True,
                is_installed=_xbox_is_installed(title_id, title_name, installed_ids),
            ):
                added += 1

        for title in history_titles:
            if title.get("type") != "Game":
                continue

            title_id = _xbox_title_id(title)
            if not title_id:
                continue

            matched_history.add(title_id)
            last_played = title.get("titleHistory", {}).get("lastTimePlayed", "")
            # OpenXBL free tier's /v2/titles returns a stub stats object
            # ({"sourceVersion": N}) with no minutesPlayed. Real per-title
            # playtime comes from Xbox Live's userstats.xboxlive.com/batch,
            # fetched below via gigalib.xbox_stats after this loop collects
            # the title ids. Leave the raw payload path as a fallback in case
            # a future OpenXBL upgrade restores the field.
            playtime_hours = _hours_from_minutes(
                title.get("stats", {}).get("minutesPlayed")
            )
            image_url = _xbox_image_url(title)
            title_name = _xbox_title_name(title)

            if _sync_xbox_game_record(
                title_id=title_id,
                title_name=title_name,
                image_url=image_url,
                last_played=last_played,
                playtime_hours=playtime_hours,
                is_gamepass=bool((title.get("gamePass") or {}).get("isGamePass")),
                is_installed=_xbox_is_installed(title_id, title_name, installed_ids),
            ):
                added += 1

        # After the catalog + history rows exist, hydrate playtime for every
        # history title via the direct Xbox Live userstats endpoint. This is
        # the only path that actually returns MinutesPlayed today; skipped
        # cleanly if the user hasn't run `python -m gigalib.xbox_stats login`.
        xbl_minutes = _fetch_xbl_playtime_minutes(matched_history)
        for title_id, minutes in xbl_minutes.items():
            row = Game.query.filter_by(platform="xbox", app_id=title_id).first()
            if row is not None:
                row.playtime_hours = round(minutes / 60, 1)

        db.session.commit()
        return {
            "status": "ok",
            "catalog_total": len(catalog_titles),
            "catalog_skipped_unknown": skipped_catalog,
            "history_total": len(history_titles),
            "matched_history": len(matched_history),
            "installed_detected": len(installed_ids),
            "playtime_fetched": len(xbl_minutes),
            "added": added,
        }

    except requests.RequestException as e:
        return {"status": "error", "reason": str(e)}


def _get_ea_content_ids():
    """Extract EA content IDs from InstallData SFT filenames."""
    content_ids = {}
    for ea_path in _get_paths("ea", "install_data"):
        if not ea_path.exists():
            continue
        for game_dir in ea_path.iterdir():
            if not game_dir.is_dir():
                continue
            # Try numeric ID from base-Origin.SFT.50.XXXXXXX
            for sft in game_dir.glob("base-Origin.SFT.*"):
                match = re.search(r"\.(\d+)$", sft.name)
                if match:
                    content_ids[game_dir.name] = ("numeric", match.group(1))
                    break
            # Fallback: GUID-based SFT (older Origin games like Dead Space 2/3)
            if game_dir.name not in content_ids:
                for sft in game_dir.glob("base-*"):
                    guid_match = re.match(
                        r"base-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                        sft.name,
                    )
                    if guid_match:
                        content_ids[game_dir.name] = ("guid", guid_match.group(1))
                        break
    return content_ids


def _decrypt_ea_library():
    """Decrypt the EA Desktop IS file to get the full game library.

    Returns a list of dicts with keys: slug, softwareId, installed, title
    """
    is_file = Path(
        r"C:\ProgramData\EA Desktop"
        r"\530c11479fe252fc5aabc24935b9776d4900eb3ba58fdc271e0d6229413ad40e\IS"
    )
    if not is_file.exists():
        return []

    try:
        # Static key (EA updated format - no machine hash for IS)
        key_input = "allUsersGenericIdISl)%ge7fomILhfj*Qfi+,"
        key = hashlib.sha3_256(key_input.encode("ascii")).digest()
        iv = hashlib.sha3_256(b"allUsersGenericIdIS").digest()[:16]

        data = is_file.read_bytes()
        # Skip first 64 bytes (hash header)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        dec = cipher.decrypt(data[64:])

        # Remove PKCS7 padding
        pad = dec[-1]
        if 1 <= pad <= 16 and all(b == pad for b in dec[-pad:]):
            dec = dec[:-pad]

        parsed = json.loads(dec.decode("utf-8"))
        infos = parsed.get("installInfos", [])

        # Deduplicate by baseSlug, keeping the first entry per slug as the base game
        seen_slugs = {}
        results = []
        for info in infos:
            slug = info.get("baseSlug", "")
            if not slug or slug in seen_slugs:
                continue
            seen_slugs[slug] = True

            sid = info.get("softwareId", "")
            install_path = info.get("baseInstallPath", "")
            status = info.get("detailedState", {}).get("installStatus", 0)
            installed = bool(install_path) or status >= 3

            # Convert slug to title: "battlefield-2042" -> "Battlefield 2042"
            title = slug.replace("-", " ").title()
            # Fix common title patterns
            title = re.sub(r"\bNfl\b", "NFL", title)
            title = re.sub(r"\bNfs\b", "NFS", title)
            title = re.sub(r"\bEa\b", "EA", title)

            results.append(
                {
                    "slug": slug,
                    "softwareId": sid,
                    "installed": installed,
                    "title": title,
                }
            )

        return results
    except Exception:
        return []


def sync_ea_local():
    """Scan EA Desktop InstallData folder and Windows registry for EA games."""
    try:
        added = 0
        games_found = []
        content_ids = _get_ea_content_ids()

        # Build a set of actually-installed games by checking registry Install Dir exists
        verified_installed = set()
        for reg_path in [r"SOFTWARE\WOW6432Node\EA Games", r"SOFTWARE\EA Games"]:
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, reg_path, 0, winreg.KEY_READ
                )
                i = 0
                while True:
                    try:
                        title = winreg.EnumKey(key, i)
                        i += 1
                        # Check if Install Dir actually exists
                        try:
                            subkey = winreg.OpenKey(key, title)
                            install_dir, _ = winreg.QueryValueEx(subkey, "Install Dir")
                            winreg.CloseKey(subkey)
                            if install_dir and Path(install_dir).exists():
                                verified_installed.add(title)
                        except OSError:
                            pass
                        if title not in games_found:
                            games_found.append(title)
                    except OSError:
                        break
                winreg.CloseKey(key)
            except OSError:
                continue

        # Scan InstallData paths (EA keeps these even after uninstall)
        for ea_path in _get_paths("ea", "install_data"):
            for game_dir in ea_path.iterdir():
                if game_dir.is_dir():
                    title = game_dir.name
                    if title not in games_found:
                        games_found.append(title)

        # Scan games_dirs for installed games not in registry
        for games_dir in _get_paths("ea", "games_dirs"):
            for game_dir in games_dir.iterdir():
                if game_dir.is_dir():
                    title = game_dir.name
                    verified_installed.add(title)
                    if title not in games_found:
                        games_found.append(title)

        # Decrypt EA Desktop IS file for the full library (includes uninstalled games)
        is_library = _decrypt_ea_library()
        # Track which games came from IS file (by app_id) for merging
        is_app_ids = {}
        for entry in is_library:
            is_app_ids[entry["softwareId"]] = entry

        # Sync to DB: first process locally-detected games (have accurate titles)
        for title in games_found:
            is_installed = title in verified_installed
            id_info = content_ids.get(title)
            if id_info:
                id_type, id_value = id_info
                if id_type == "numeric":
                    app_id = f"Origin.OFR.50.{id_value}"
                else:
                    app_id = id_value  # GUID directly
            else:
                app_id = title.lower().replace(" ", "-")

            existing = Game.query.filter_by(platform="ea", title=title).first()

            if not existing:
                new_game = Game(
                    title=title,
                    platform="ea",
                    app_id=app_id,
                    is_installed=is_installed,
                    image_url=_get_ea_image_url(title),
                )
                db.session.add(new_game)
                added += 1
            else:
                existing.is_installed = is_installed
                if id_info and existing.app_id != app_id:
                    existing.app_id = app_id

        # Add games from IS library that weren't found via local scanning
        existing_app_ids = {g.app_id for g in Game.query.filter_by(platform="ea").all()}
        existing_titles = {
            g.title.lower() for g in Game.query.filter_by(platform="ea").all()
        }
        for entry in is_library:
            sid = entry["softwareId"]
            if sid in existing_app_ids:
                continue
            # Also check OFR/SFT equivalent (local scan uses OFR, IS file uses SFT)
            alt_id = sid.replace("Origin.SFT.50.", "Origin.OFR.50.")
            if alt_id in existing_app_ids:
                continue
            # Skip if we already have this game by case-insensitive title match
            if entry["title"].lower() in existing_titles:
                continue

            new_game = Game(
                title=entry["title"],
                platform="ea",
                app_id=sid,
                is_installed=entry["installed"],
                image_url=_get_ea_image_url(entry["title"]),
            )
            db.session.add(new_game)
            added += 1

        db.session.commit()

        total = len(games_found) + len(is_library)
        if not games_found and not is_library:
            return {"status": "skipped", "reason": "EA Desktop not found on this PC"}

        return {"status": "ok", "total": total, "added": added}

    except Exception as e:
        return {"status": "error", "reason": str(e)}


def sync_ubisoft_local():
    """Parse Ubisoft Connect configuration cache using UplayDB-inspired binary parsing."""
    config_caches = _get_paths("ubisoft", "config_cache")
    if not config_caches:
        return {"status": "skipped", "reason": "Ubisoft Connect not found on this PC"}

    try:
        # Read from first available config cache
        raw_bytes = config_caches[0].read_bytes()
        text = raw_bytes.decode("utf-8", errors="ignore")

        # UplayDB approach: split by "version: 2.0" markers (each is a product definition)
        # Only keep sections that have start_game: or game_identifier: (actual launchable games)
        sections = re.split(r"version:\s*2\.0", text)

        base_games = []
        for section in sections[1:]:
            has_start_game = "start_game:" in section
            has_game_identifier = "game_identifier:" in section

            if not (has_start_game or has_game_identifier):
                continue

            # Prefer display_name (clean retail name), then root name, then game_identifier
            display_match = re.search(r'display_name:\s*"([^"]+)"', section)
            root_match = re.search(r'root:\s*\n\s*name:\s*"([^"]+)"', section)
            root_match2 = re.search(r"root:\s*\n\s*name:\s+([^\n]+)", section)
            gid_match = re.search(r"game_identifier:\s*([^\n]+)", section)

            # Extract Uplay launch ID from registry path (e.g. Installs\273\InstallDir)
            launch_id_match = re.search(r"Installs\\(\d+)\\InstallDir", section)

            name = None
            if display_match:
                name = display_match.group(1)
            elif root_match:
                name = root_match.group(1)
            elif root_match2:
                name = root_match2.group(1).strip().strip('"')
            elif gid_match:
                name = gid_match.group(1).strip()

            if name:
                name = name.strip().strip("'").strip('"')
                launch_id = launch_id_match.group(1) if launch_id_match else None
                base_games.append((name, launch_id))

        # Deduplicate by name (keep first occurrence with its launch_id)
        seen = {}
        for name, launch_id in base_games:
            if name not in seen:
                seen[name] = launch_id

        # Filter placeholders and test entries
        _skip = {"l1", "GAMENAME", "NAME", "YOURNAME"}
        filtered = {}
        for name, launch_id in seen.items():
            if len(name) <= 2 or name in _skip or "test server" in name.lower():
                continue
            clean_name = re.sub(r"\s*PREORDER\s*$", "", name).strip()
            if clean_name and clean_name not in filtered:
                filtered[clean_name] = launch_id

        # Determine which are installed by checking the games directories
        installed_dirs = set()
        for games_dir in _get_paths("ubisoft", "games_dirs"):
            installed_names = [d.name for d in games_dir.iterdir() if d.is_dir()]
            installed_dirs.update(n.lower() for n in installed_names)
            # Add installed games not found in cache
            for name in installed_names:
                if name not in filtered:
                    filtered[name] = None

        added = 0
        for title, launch_id in filtered.items():
            existing = Game.query.filter_by(platform="ubisoft", title=title).first()

            is_installed = any(
                title.lower() in inst or inst in title.lower()
                for inst in installed_dirs
            )

            if not existing:
                new_game = Game(
                    title=title,
                    platform="ubisoft",
                    app_id=launch_id or title.lower().replace(" ", "-"),
                    is_installed=is_installed,
                )
                db.session.add(new_game)
                added += 1
            else:
                existing.is_installed = is_installed
                if launch_id and existing.app_id != launch_id:
                    existing.app_id = launch_id

        db.session.commit()
        return {"status": "ok", "total": len(filtered), "added": added}

    except Exception as e:
        return {"status": "error", "reason": str(e)}
