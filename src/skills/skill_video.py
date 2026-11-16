"""
Skill: video — Play web videos in the internal mpv video app.

TARS-AI Built-in Commands Initiative (M4-S3)
Replaces the external-browser skill_browser.py with an internal mpv
VideoApp.  YouTube search + direct URL playback, per-site toggles,
prior-app restore, voice + tap-to-stop.  No followup key — verbatim shim.

This file is an original work authored for the TARS-AI project and is
licensed under the repository's standard license.
"""
import re
import shutil
import threading
from urllib.parse import urlparse

from modules.module_messageQue import queue_message

# ---------------------------------------------------------------------------
# Site → config-key mapping (D18)
# ---------------------------------------------------------------------------

_HOST_SITE_MAP = {
    "www.youtube.com": "site_youtube",
    "youtube.com": "site_youtube",
    "youtu.be": "site_youtube",
    "m.youtube.com": "site_youtube",
    "www.vimeo.com": "site_vimeo",
    "vimeo.com": "site_vimeo",
    "www.dailymotion.com": "site_dailymotion",
    "dailymotion.com": "site_dailymotion",
    "www.twitch.tv": "site_twitch",
    "twitch.tv": "site_twitch",
    "m.twitch.tv": "site_twitch",
    "clips.twitch.tv": "site_twitch",
    "www.cnn.com": "site_us_news",
    "cnn.com": "site_us_news",
    "www.nbcnews.com": "site_us_news",
    "nbcnews.com": "site_us_news",
    "abcnews.go.com": "site_us_news",
    "www.cbsnews.com": "site_us_news",
    "cbsnews.com": "site_us_news",
    "www.foxnews.com": "site_us_news",
    "foxnews.com": "site_us_news",
    "www.tiktok.com": "site_tiktok",
    "tiktok.com": "site_tiktok",
    "www.bbc.co.uk": "site_bbc_iplayer",
    "bbc.co.uk": "site_bbc_iplayer",
    "www.bbc.com": "site_bbc_iplayer",
    "bbc.com": "site_bbc_iplayer",
}

_SITE_LABELS = {
    "site_youtube": "YouTube",
    "site_vimeo": "Vimeo",
    "site_dailymotion": "Dailymotion",
    "site_twitch": "Twitch",
    "site_us_news": "US News",
    "site_tiktok": "TikTok",
    "site_bbc_iplayer": "BBC iPlayer",
}

# ---------------------------------------------------------------------------
# SKILL definition
# ---------------------------------------------------------------------------

SKILL = {
    "name": "video",
    "description": "Play web videos on the device display (YouTube search + URL playback)",
    "required_params": ["action"],
    "config": {
        "site_youtube": {
            "type": "bool",
            "default": True,
            "description": "Allow YouTube playback (search + direct URLs)",
        },
        "site_vimeo": {
            "type": "bool",
            "default": True,
            "description": "Allow Vimeo playback",
        },
        "site_dailymotion": {
            "type": "bool",
            "default": True,
            "description": "Allow Dailymotion playback",
        },
        "site_twitch": {
            "type": "bool",
            "default": True,
            "description": "Allow Twitch clips and VODs (live streams not supported)",
        },
        "site_us_news": {
            "type": "bool",
            "default": False,
            "description": "Allow US news sites (CNN, NBC, ABC, CBS, Fox)",
        },
        "site_tiktok": {
            "type": "bool",
            "default": False,
            "description": "Allow TikTok playback",
        },
        "site_bbc_iplayer": {
            "type": "bool",
            "default": False,
            "description": "Allow BBC iPlayer (UK-only — needs a UK IP)",
        },
    },
    "prompt": """video
    Triggers: play a youtube video, play video, watch a video, search
    youtube, play {query} on youtube, open https://youtube.com/..., stop the
    video, stop playing the video, close the video.
    Actions: play (query OR url), stop.
    Site toggles: the seven [SKILL:video] site_* bools gate playback per
    site; defaults are YouTube/Vimeo/Dailymotion/Twitch ON, the rest OFF.""",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_config_bool(key, parameters):
    """Resolve a site toggle: saved config wins, else the skill default."""
    saved = parameters.get("_config", {}).get(key)
    if saved is not None:
        return saved
    return SKILL["config"][key]["default"]


def _host_to_site(host):
    """Map a hostname to its site_* key, or None for unsupported sites."""
    host = (host or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    # Re-add www. for the lookup — the map has both
    return _HOST_SITE_MAP.get(host) or _HOST_SITE_MAP.get("www." + host)


def _check_mpv():
    """Return error string if mpv is missing, else None."""
    if shutil.which("mpv") is None:
        return "Video playback needs mpv \u2014 rerun Install.sh."
    return None


def _check_ytdlp():
    """Return error string if yt-dlp can't be imported, else None."""
    try:
        import yt_dlp  # noqa: F401
        return None
    except ImportError:
        return "Video playback needs yt-dlp \u2014 install it in the virtual environment."


def _resolve_youtube_search(query):
    """Search YouTube for *query* and return (url, title) or (None, None).

    Uses yt-dlp's built-in ytsearch extractor (extract_flat for speed).
    This is a fresh, original implementation — standard yt-dlp API pattern.

    Returns (url_or_None, title_or_empty_string).
    """
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "default_search": "ytsearch1",
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            entries = info.get("entries") if info else None
            if entries and len(entries) > 0:
                entry = entries[0]
                vid = entry.get("id") or entry.get("url")
                title = entry.get("title") or ""
                if vid:
                    url = f"https://www.youtube.com/watch?v={vid}"
                    return url, title
        return None, ""
    except Exception:
        return None, ""


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

def execute(parameters, context):
    """Dispatch play/stop on the side-effect thread (fire-and-forget)."""
    action = (parameters.get("action") or "").lower().strip()

    # --- guards -----------------------------------------------------------
    from modules.module_main import ui_manager

    if not ui_manager or not getattr(ui_manager, "running", False):
        return "Video playback needs the on-device display."

    mpv_err = _check_mpv()
    if mpv_err:
        return mpv_err

    # --- stop -------------------------------------------------------------
    if action == "stop":
        try:
            from modules.UI.apps.module_app_video import request_stop, is_playing
        except ImportError:
            return "Video playback isn't available on this device."
        if not is_playing():
            return "No video is playing."
        request_stop()
        return "Stopped the video."

    # --- play -------------------------------------------------------------
    if action != "play":
        return "The video command only supports play and stop actions."

    url = (parameters.get("url") or "").strip()
    query = (parameters.get("query") or "").strip()

    # -- yt-dlp guard --
    ytdlp_err = _check_ytdlp()
    if ytdlp_err:
        return ytdlp_err

    # -- URL playback: parse host, check site gate --
    if url:
        try:
            parsed = urlparse(url)
        except Exception:
            return "That URL doesn't look right."
        host = parsed.hostname or ""
        site_key = _host_to_site(host)
        if site_key is None:
            return "That site isn't supported for video playback."

        enabled = _get_config_bool(site_key, parameters)
        if not enabled:
            label = _SITE_LABELS.get(site_key, site_key)
            return (
                f"'{label}' playback is disabled \u2014 "
                "enable it in the video command settings."
            )

        # Twitch live-stream rejection (bare-channel URLs). Canonical clip
        # URLs live on clips.twitch.tv with NO /clip/ segment (audit IB-F6).
        if (site_key == "site_twitch" and "/videos/" not in url
                and "/clip/" not in url and "clips.twitch.tv" not in url):
            return "Live streams aren't supported yet."

        title = parameters.get("description", "") or "Video"

    # -- search: YouTube-only, gated by site_youtube --
    elif query:
        if not _get_config_bool("site_youtube", parameters):
            return (
                "'YouTube' playback is disabled \u2014 "
                "enable it in the video command settings."
            )
        url, title = _resolve_youtube_search(query)
        if not url:
            return "Couldn't find a video for that search. Try a different query."
    else:
        return "Provide a URL to play or a query to search YouTube."

    # -- request playback on the UI thread --
    try:
        from modules.UI.apps.module_app_video import request_play
    except ImportError:
        return "Video playback isn't available on this device."

    request_play(url, title)
    ui_manager.request_app("video")

    return f"Now playing: {title}." if title else "Now playing."