#!/usr/bin/env python3
"""Перенос и синхронизация плейлистов Яндекс.Музыки в YouTube Music."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator, Sequence, TypeVar
from urllib.parse import quote_plus, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

try:
    from anyascii import anyascii as _anyascii
except ImportError:  # пакет есть в requirements, но скрипт должен работать и без него
    def _anyascii(value: str) -> str:
        return value

try:
    from tqdm import tqdm
except ImportError:  # прогресс-бар опционален
    tqdm = None  # type: ignore[assignment]

__version__ = "0.2.0"

DEFAULT_MIN_SCORE = 0.72
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_ALTERNATIVES = 3
DEFAULT_CACHE_DIR = Path(".cache/yandex2ytm")
DEFAULT_STATE_DIR = Path(".yandex2ytm")
YANDEX_CHUNK_SIZE = 100
YT_ADD_CHUNK_SIZE = 50
PLAYLIST_URL = "https://music.youtube.com/playlist?list={}"
WATCH_URL = "https://music.youtube.com/watch?v={}"
YT_SEARCH_URL = "https://music.youtube.com/search?q={}"
STATE_VERSION = 2
CACHE_VERSION = 2

T = TypeVar("T")

STATUS_MATCHED = "matched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NOT_FOUND = "not_found"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"
STATUS_DUPLICATE = "duplicate"
STATUS_SKIPPED = "skipped"
ALL_STATUSES = (
    STATUS_MATCHED,
    STATUS_AMBIGUOUS,
    STATUS_NOT_FOUND,
    STATUS_UNAVAILABLE,
    STATUS_ERROR,
    STATUS_DUPLICATE,
    STATUS_SKIPPED,
)

NOISE_WORD_RE = re.compile(
    r"\b(feat(?:uring)?|ft|prod(?:uced)?|remaster(?:ed)?|version|edit|remix|mix|"
    r"live|instrumental|acoustic|demo|bonus|official|audio|video|lyrics?|cover|"
    r"radio|extended|deluxe|explicit|mono|stereo|bootleg|sped\s*up|slowed|"
    r"nightcore)\b",
    re.IGNORECASE,
)
BRACKET_RE = re.compile(r"[\(\[\{]([^)\]\}]*)[\)\]\}]")
COVER_RE = re.compile(
    r"\b(karaoke|cover|tribute|originally\s+performed|lullaby|renditions?|"
    r"8-bit|backing\s+track|in\s+the\s+style\s+of|made\s+famous|parody)\b",
    re.IGNORECASE,
)
VERSION_RE = re.compile(
    r"\b(live|remix|acoustic|instrumental|demo|radio\s+edit|extended|sped\s+up|slowed|nightcore)\b",
    re.IGNORECASE,
)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Ниже этого сходства исполнителя кандидат считается «другим артистом»:
# одного точного названия и длительности недостаточно для совпадения.
ARTIST_FLOOR = 0.72
ARTIST_PENALTY = 0.7
# Слишком далёкое название при точном исполнителе — обычно другая песня.
TITLE_FLOOR = 0.72
TITLE_PENALTY = 0.7


# ---------------------------------------------------------------------------
# Текст и сопоставление
# ---------------------------------------------------------------------------


def _noise_ratio(text: str) -> float:
    tokens = re.findall(r"[^\W_]+", text or "", flags=re.UNICODE)
    if not tokens:
        return 1.0
    noise = sum(1 for token in tokens if NOISE_WORD_RE.fullmatch(token) or token.isdigit())
    return noise / len(tokens)


def _is_feature_phrase(text: str) -> bool:
    first = re.findall(r"[^\W_]+", text or "", flags=re.UNICODE)
    return bool(first) and first[0].lower() in {"feat", "featuring", "ft", "prod", "produced"}


def _is_noise_marker(text: str) -> bool:
    """Пометка версии/записи: «Live at Wembley», «Remastered 2011», «feat. X»."""
    tokens = re.findall(r"[^\W_]+", text or "", flags=re.UNICODE)
    if not tokens:
        return False
    return (
        _is_feature_phrase(text)
        or COVER_RE.search(text) is not None
        or NOISE_WORD_RE.fullmatch(tokens[0]) is not None
    )


def _drop_noise_brackets(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if _is_noise_marker(inner) or _noise_ratio(inner) > 0.5:
            return " "
        return f" {inner} "

    return BRACKET_RE.sub(replace, text)


def _drop_noise_tail(text: str) -> str:
    parts = re.split(r"\s+-\s+", text)
    while len(parts) > 1 and (_is_noise_marker(parts[-1]) or _noise_ratio(parts[-1]) > 0.5):
        parts.pop()
    return " - ".join(parts)


def clean_text(text: str) -> str:
    text = _drop_noise_tail(_drop_noise_brackets(text or ""))
    # В пользовательских плейлистах Яндекса встречается длительность в названии:
    # «Unslept 0:00» — это не часть песни.
    text = re.sub(r"\s+\d{1,2}:\d{2}$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    text = clean_text(text).replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _variants(text: str) -> list[tuple[str, float]]:
    base = normalize(text)
    transliterated = normalize(_anyascii(text or ""))
    variants = [(base, 1.0)]
    if transliterated and transliterated != base:
        variants.append((transliterated, 0.95))
    return [variant for variant in variants if variant[0]]


def _tokens_subset(first: str, second: str) -> bool:
    left = set(first.split())
    right = set(second.split())
    # Для одного слова совпадение считается только точным: иначе
    # «Song» и «Song Part 2» получали бы 0.95 и маскировали чужую версию.
    if min(len(left), len(right)) < 2:
        return False
    return left <= right or right <= left


def _text_similarity(first: str, second: str) -> float:
    best = 0.0
    for left, left_weight in _variants(first):
        for right, right_weight in _variants(second):
            if left == right:
                score = 1.0
            elif _tokens_subset(left, right):
                score = 0.95
            else:
                score = SequenceMatcher(None, left, right).ratio()
            best = max(best, score * min(left_weight, right_weight))
    return best


def _title_score(target_title: str, youtube_title: str) -> float:
    variants = [youtube_title]
    if " - " in youtube_title:
        variants.append(youtube_title.split(" - ", 1)[1])
    return max((_text_similarity(target_title, variant) for variant in variants), default=0.0)


def _candidate_artists(candidate: dict[str, Any]) -> list[str]:
    artists = candidate.get("artists") or []
    names = []
    for artist in artists:
        if isinstance(artist, dict):
            name = artist.get("name")
            if name:
                names.append(str(name))
        elif artist:
            names.append(str(artist))
    return names


def _artists_score(target_artists: Sequence[str], youtube_artists: Sequence[str]) -> float:
    targets = [name for name in target_artists if normalize(name)]
    candidates = [name for name in youtube_artists if normalize(name)]
    if not targets:
        return 0.5
    if not candidates:
        return 0.0
    best_any = max(_text_similarity(target, candidate) for target in targets for candidate in candidates)
    primary = max(_text_similarity(target, candidates[0]) for target in targets)
    return 0.65 * best_any + 0.35 * primary


def _duration_seconds(candidate: dict[str, Any]) -> int | None:
    value = candidate.get("duration_seconds")
    if isinstance(value, (int, float)):
        return int(value)
    value = candidate.get("duration")
    if isinstance(value, str) and ":" in value:
        try:
            parts = [int(part) for part in value.split(":")]
        except ValueError:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    return None


def _duration_score(target_ms: int | None, youtube_seconds: int | None) -> float | None:
    if not target_ms or not youtube_seconds:
        return None
    difference = abs(target_ms / 1000 - youtube_seconds)
    if difference <= 2:
        return 1.0
    if difference <= 5:
        return 0.95
    if difference <= 15:
        return 0.8
    if difference <= 30:
        return 0.6
    return 0.3


def _candidate_album(candidate: dict[str, Any]) -> str:
    album = candidate.get("album")
    if isinstance(album, dict):
        return str(album.get("name") or "")
    return str(album or "")


def _cover_penalty(track: "YandexTrack", candidate: dict[str, Any]) -> float:
    target_text = f"{track.title} {track.album or ''}"
    candidate_text = f"{candidate.get('title') or ''} {_candidate_album(candidate)}"
    if COVER_RE.search(candidate_text) and not COVER_RE.search(target_text):
        return 0.7
    return 1.0


def _version_penalty(track: "YandexTrack", candidate: dict[str, Any]) -> float:
    target_text = f"{track.title} {track.album or ''}"
    candidate_text = f"{candidate.get('title') or ''} {_candidate_album(candidate)}"
    if VERSION_RE.search(candidate_text) and not VERSION_RE.search(target_text):
        return 0.8
    return 1.0


@dataclass
class ScoreParts:
    total: float
    title: float
    artists: float
    duration: float | None
    penalty: float = 1.0


@dataclass
class YandexTrack:
    position: int
    title: str
    artists: list[str]
    duration_ms: int | None = None
    available: bool = True
    track_id: str | None = None
    album_id: str | None = None
    album: str | None = None
    key: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            if self.track_id:
                self.key = f"{self.track_id}:{self.album_id or ''}"
            else:
                self.key = f"pos:{self.position}"


@dataclass
class PlaylistRef:
    owner: str | None = None
    kind: str | None = None
    uuid: str | None = None
    liked: bool = False


@dataclass
class SourcePlaylist:
    key: str
    title: str
    url: str
    track_count: int | None
    shorts: list[Any]


@dataclass
class ScoredCandidate:
    candidate: dict[str, Any]
    parts: ScoreParts


@dataclass
class TrackMatch:
    track: YandexTrack
    status: str
    candidate: dict[str, Any] | None = None
    score: float = 0.0
    note: str = ""
    parts: ScoreParts | None = None
    alternatives: list[ScoredCandidate] = field(default_factory=list)
    decided: bool = False

    @property
    def video_id(self) -> str | None:
        if not self.candidate:
            return None
        value = self.candidate.get("videoId")
        return str(value) if value else None


def parse_playlist_ref(value: str) -> PlaylistRef:
    value = (value or "").strip()
    if not value:
        raise ValueError("пустая ссылка на плейлист")
    if value.casefold() in {"liked", "likes", "favorites", "мне нравится", "любимое"}:
        return PlaylistRef(liked=True)
    if value.isdigit():
        return PlaylistRef(kind=value)
    if re.fullmatch(r"[^/\s:]+:[^/\s]+", value):
        owner, kind = value.rsplit(":", 1)
        return PlaylistRef(owner=owner, kind=kind)
    if "://" not in value:
        value = "https://" + value
    path = urlparse(value).path.rstrip("/")
    match = re.search(r"/users/([^/]+)/playlists/([^/?#]+)", path)
    if match:
        return PlaylistRef(owner=match.group(1), kind=match.group(2))
    match = re.search(r"/playlists/([^/?#]+)", path)
    if match:
        return PlaylistRef(uuid=match.group(1))
    raise ValueError(
        "не удалось распознать ссылку. Ожидается формат "
        "https://music.yandex.ru/users/<user>/playlists/<kind> "
        "или https://music.yandex.ru/playlists/<uuid>"
    )


def ref_key(ref: PlaylistRef) -> str:
    if ref.liked:
        return "liked"
    if ref.uuid:
        return f"uuid:{ref.uuid}"
    if ref.owner:
        return f"{ref.owner}:{ref.kind}"
    return f"kind:{ref.kind}"


def ref_url(ref: PlaylistRef) -> str:
    if ref.liked:
        return "https://music.yandex.ru/collection/liked"
    if ref.uuid:
        return f"https://music.yandex.ru/playlists/{ref.uuid}"
    if ref.owner:
        return f"https://music.yandex.ru/users/{ref.owner}/playlists/{ref.kind}"
    return f"https://music.yandex.ru/users/me/playlists/{ref.kind}"


def build_query(track: YandexTrack) -> str:
    title = clean_text(track.title) or track.title
    artist = track.artists[0] if track.artists else ""
    return f"{artist} {title}".strip()


def score_candidate_parts(track: YandexTrack, candidate: dict[str, Any]) -> ScoreParts:
    title_score = _title_score(track.title, str(candidate.get("title") or ""))
    artists_score = _artists_score(track.artists, _candidate_artists(candidate))
    duration_score = _duration_score(track.duration_ms, _duration_seconds(candidate))
    if duration_score is None:
        base = 0.62 * title_score + 0.38 * artists_score
    else:
        base = 0.55 * title_score + 0.30 * artists_score + 0.15 * duration_score
    penalty = _cover_penalty(track, candidate) * _version_penalty(track, candidate)
    if title_score < TITLE_FLOOR:
        penalty *= TITLE_PENALTY
    if track.artists and artists_score < ARTIST_FLOOR:
        penalty *= ARTIST_PENALTY
    return ScoreParts(base * penalty, title_score, artists_score, duration_score, penalty)


def penalty_note(track: YandexTrack, candidate: dict[str, Any], parts: ScoreParts) -> str:
    if parts.penalty >= 1.0:
        return ""
    reasons = []
    if _cover_penalty(track, candidate) < 1.0:
        reasons.append("кавер")
    if _version_penalty(track, candidate) < 1.0:
        reasons.append("версия")
    if parts.title < TITLE_FLOOR:
        reasons.append("другое название")
    if track.artists and parts.artists < ARTIST_FLOOR:
        reasons.append("другой исполнитель")
    return f"штраф: {', '.join(reasons)} (×{parts.penalty:.2f})"


def score_candidate(track: YandexTrack, candidate: dict[str, Any]) -> float:
    return score_candidate_parts(track, candidate).total


def choose_match(
    track: YandexTrack,
    candidates: list[dict[str, Any]],
    min_score: float,
    alternatives: int = DEFAULT_ALTERNATIVES,
) -> TrackMatch:
    scored = sorted(
        ((score_candidate_parts(track, candidate), candidate) for candidate in candidates),
        key=lambda item: item[0].total,
        reverse=True,
    )
    scored = [item for item in scored if item[1].get("isAvailable") is not False]
    if not scored:
        return TrackMatch(track, STATUS_NOT_FOUND, note="ничего не найдено")
    top = [ScoredCandidate(candidate, parts) for parts, candidate in scored[: max(1, alternatives)]]
    best_parts, best = scored[0]
    best_score = best_parts.total
    runner_up = scored[1][0].total if len(scored) > 1 else 0.0
    note = penalty_note(track, best, best_parts)
    if best_score < min_score:
        reason = f"лучший результат {best_score:.2f} ниже порога {min_score:.2f}"
        return TrackMatch(
            track,
            STATUS_NOT_FOUND,
            best,
            best_score,
            f"{reason}; {note}" if note else reason,
            best_parts,
            top,
        )
    if runner_up >= best_score - 0.05 and best_score < 0.9:
        reason = f"похожие результаты ({best_score:.2f} и {runner_up:.2f})"
        return TrackMatch(
            track,
            STATUS_AMBIGUOUS,
            best,
            best_score,
            f"{reason}; {note}" if note else reason,
            best_parts,
            top,
        )
    return TrackMatch(track, STATUS_MATCHED, best, best_score, note, best_parts, top)


# ---------------------------------------------------------------------------
# Яндекс.Музыка
# ---------------------------------------------------------------------------


def build_yandex_client(token: str | None):
    try:
        from yandex_music import Client
    except ImportError as exc:
        raise RuntimeError(
            "Не установлен пакет yandex-music. Выполните: pip install -r requirements.txt"
        ) from exc
    try:
        return (Client(token) if token else Client()).init()
    except Exception as exc:
        if not token:
            raise RuntimeError(
                "Не удалось получить плейлист без токена. Задайте YANDEX_MUSIC_TOKEN "
                "(токен нужен для приватных плейлистов и своего аккаунта)."
            ) from exc
        raise RuntimeError(f"Не удалось подключиться к Яндекс.Музыке, проверьте токен: {exc}") from exc


def load_source(client, ref: PlaylistRef) -> SourcePlaylist:
    if ref.liked:
        result = client.users_likes_tracks()
        shorts = list(getattr(result, "tracks", None) or [])
        return SourcePlaylist("liked", "Мне нравится", ref_url(ref), len(shorts), shorts)

    if ref.uuid:
        playlist = client.playlist(ref.uuid)
    else:
        playlist = client.users_playlists(ref.kind, ref.owner)
    if isinstance(playlist, list):
        playlist = playlist[0] if playlist else None
    if playlist is None:
        raise RuntimeError("Плейлист не найден или недоступен. Проверьте ссылку и токен.")

    shorts = list(getattr(playlist, "tracks", None) or [])
    track_count = getattr(playlist, "track_count", None)
    if track_count and len(shorts) < track_count:
        try:
            fetched_shorts = playlist.fetch_tracks() or []
            if len(fetched_shorts) > len(shorts):
                shorts = list(fetched_shorts)
        except Exception as exc:
            print(f"  ! не удалось дозагрузить треки плейлиста: {exc}")

    title = str(getattr(playlist, "title", None) or "Плейлист из Яндекс.Музыки")
    uuid = getattr(playlist, "uuid", None)
    key = f"uuid:{uuid}" if uuid else ref_key(ref)
    return SourcePlaylist(key, title, ref_url(ref), track_count or len(shorts), shorts)


def _track_key(track: Any) -> str | None:
    track_id = getattr(track, "id", None)
    if track_id is None:
        return None
    albums = getattr(track, "albums", None) or []
    album_id = getattr(albums[0], "id", None) if albums else None
    return f"{track_id}:{album_id}" if album_id is not None else str(track_id)


def _to_yandex_track(position: int, track: Any) -> YandexTrack:
    title = str(getattr(track, "title", None) or "").strip()
    artists = [
        str(artist.name)
        for artist in (getattr(track, "artists", None) or [])
        if getattr(artist, "name", None)
    ]
    duration_ms = getattr(track, "duration_ms", None)
    if not isinstance(duration_ms, (int, float)):
        duration_ms = None
    available = bool(title) and getattr(track, "available", True) is not False
    track_id = getattr(track, "id", None)
    albums = getattr(track, "albums", None) or []
    album = albums[0] if albums else None
    album_id = getattr(album, "id", None) if album is not None else None
    album_title = getattr(album, "title", None) if album is not None else None
    return YandexTrack(
        position=position,
        title=title,
        artists=artists,
        duration_ms=int(duration_ms) if duration_ms else None,
        available=available,
        track_id=str(track_id) if track_id is not None else None,
        album_id=str(album_id) if album_id is not None else None,
        album=str(album_title) if album_title else None,
    )


def resolve_yandex_tracks(client, source: SourcePlaylist) -> list[YandexTrack]:
    shorts = list(source.shorts)
    resolved: list[YandexTrack | None] = [None] * len(shorts)
    pending: list[tuple[int, str]] = []

    for position, short in enumerate(shorts):
        if short is None:
            continue
        full_track = getattr(short, "track", None)
        if full_track is not None:
            resolved[position] = _to_yandex_track(position + 1, full_track)
            continue
        short_id = getattr(short, "track_id", None)
        if short_id:
            pending.append((position, str(short_id)))
        else:
            resolved[position] = YandexTrack(position + 1, "", [], None, False)

    for chunk in _chunked(pending, YANDEX_CHUNK_SIZE):
        ids = [track_id for _, track_id in chunk]
        try:
            fetched = with_retries(
                lambda ids=ids: client.tracks(ids),
                label=f"загрузка {len(ids)} треков из Яндекс.Музыки",
            )
        except Exception as exc:
            print(f"  ! не удалось загрузить {len(ids)} треков: {exc}")
            fetched = []
        fetched = fetched or []

        by_key: dict[str, Any] = {}
        by_id: dict[str, Any] = {}
        for track in fetched:
            if track is None:
                continue
            key = _track_key(track)
            if key:
                by_key[key] = track
            track_id = getattr(track, "id", None)
            if track_id is not None:
                by_id[str(track_id)] = track

        for position, track_id in chunk:
            track = by_key.get(track_id) or by_id.get(track_id.split(":", 1)[0])
            if track is None:
                resolved[position] = YandexTrack(
                    position + 1,
                    "",
                    [],
                    None,
                    False,
                    track_id=track_id.split(":", 1)[0],
                    key=track_id,
                )
            else:
                resolved[position] = _to_yandex_track(position + 1, track)

    return [track for track in resolved if track is not None]


def yandex_track_url(track: YandexTrack) -> str:
    if track.track_id and track.album_id:
        return f"https://music.yandex.ru/album/{track.album_id}/track/{track.track_id}"
    return ""


# ---------------------------------------------------------------------------
# YouTube Music, кэш и поиск
# ---------------------------------------------------------------------------


def resolve_auth_path(value: str | None) -> str | None:
    if value:
        return value
    env_value = os.environ.get("YTMUSIC_AUTH") or os.environ.get("YTM_AUTH")
    if env_value:
        return env_value
    for candidate in ("oauth.json", "browser.json"):
        if Path(candidate).is_file():
            return candidate
    return None


def _proxy_config(value: str | None) -> dict[str, str] | None:
    proxy = value or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def build_ytmusic(
    auth_path: str | None,
    dry_run: bool,
    proxies: dict[str, str] | None = None,
    language: str = "en",
    location: str = "",
):
    try:
        from ytmusicapi import YTMusic
    except ImportError as exc:
        raise RuntimeError(
            "Не установлен пакет ytmusicapi. Выполните: pip install -r requirements.txt"
        ) from exc
    kwargs: dict[str, Any] = {}
    if proxies:
        kwargs["proxies"] = proxies
    if language:
        kwargs["language"] = language
    if location:
        kwargs["location"] = location
    if auth_path:
        if not Path(auth_path).is_file():
            raise RuntimeError(f"Файл авторизации не найден: {auth_path}")
        if Path(auth_path).name == "browser.json":
            print("  ! browser.json устарел: ytmusicapi рекомендует `ytmusicapi oauth`")
        return YTMusic(auth_path, **kwargs)
    if not dry_run:
        raise RuntimeError(
            "Для создания плейлиста нужна авторизация YouTube Music. "
            "Выполните `ytmusicapi oauth --file oauth.json` и передайте --ytm-auth oauth.json"
        )
    return YTMusic(**kwargs) if kwargs else YTMusic()


def check_ytm_auth(yt) -> None:
    try:
        yt.get_account_info()
    except Exception as exc:
        raise RuntimeError(
            "YouTube Music не принял авторизацию. Обновите её командой "
            "`ytmusicapi oauth --file oauth.json` и передайте --ytm-auth oauth.json"
        ) from exc


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).casefold()
    if any(marker in text for marker in ("401", "403", "invalid_grant", "unauthorized", "forbidden")):
        return False
    if any(
        marker in text
        for marker in (
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "connection",
            "temporarily",
            "rate limit",
            "too many",
        )
    ):
        return True
    try:
        import requests

        return isinstance(exc, (requests.ConnectionError, requests.Timeout))
    except ImportError:
        return False


def with_retries(fn, label: str, attempts: int = 3, base_delay: float = 1.5):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not _is_retryable(exc):
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
            print(f"  ! {label}: {exc}. Повтор через {delay:.1f} с ({attempt}/{attempts})")
            time.sleep(delay)
    raise RuntimeError("повторные попытки исчерпаны")


class SearchCache:
    def __init__(self, directory: Path, refresh: bool = False, offline: bool = False) -> None:
        self.directory = directory
        self.refresh = refresh
        self.offline = offline

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def key(self, query: str, filter_: str, limit: int) -> str:
        payload = json.dumps(
            {"version": CACHE_VERSION, "query": query, "filter": filter_, "limit": limit},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def get(self, query: str, filter_: str, limit: int) -> list[dict[str, Any]] | None:
        if self.refresh:
            return None
        path = self._path(self.key(query, filter_, limit))
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        results = data.get("results")
        return results if isinstance(results, list) else None

    def put(self, query: str, filter_: str, limit: int, results: list[dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "query": query,
            "filter": filter_,
            "limit": limit,
            "created_at": now_iso(),
            "results": results,
        }
        path = self._path(self.key(query, filter_, limit))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def _search(yt, query: str, filter_: str, limit: int) -> list[dict[str, Any]]:
    results = with_retries(
        lambda: yt.search(query, filter=filter_, limit=limit),
        label=f"поиск «{query}»",
    )
    items = [item for item in (results or []) if item.get("videoId")]
    # ytmusicapi 1.12 отдаёт до 20 результатов независимо от limit.
    return items[:limit]


def _search_cached(
    cache: SearchCache | None,
    yt,
    query: str,
    filter_: str,
    limit: int,
) -> list[dict[str, Any]]:
    if cache is not None:
        cached = cache.get(query, filter_, limit)
        if cached is not None:
            return cached
        if cache.offline:
            raise RuntimeError(
                f"в кэше нет запроса «{query}»; запустите без --reuse-cache, чтобы заполнить кэш"
            )
    if yt is None:
        raise RuntimeError("нет клиента YouTube Music для поиска")
    results = _search(yt, query, filter_, limit)
    if cache is not None:
        cache.put(query, filter_, limit, results)
    return results


def find_candidates(
    yt,
    track: YandexTrack,
    limit: int,
    allow_videos: bool,
    min_score: float,
    cache: SearchCache | None = None,
) -> list[dict[str, Any]]:
    query = build_query(track)
    candidates = _search_cached(cache, yt, query, "songs", limit)
    if allow_videos:
        best = max((score_candidate(track, candidate) for candidate in candidates), default=0.0)
        if best < min_score:
            candidates.extend(_search_cached(cache, yt, query, "videos", limit))
    if not candidates and track.title:
        alternative = clean_text(track.title) or track.title
        if alternative and alternative.casefold() != query.casefold():
            candidates = _search_cached(cache, yt, alternative, "songs", limit)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.get("isAvailable") is False:
            continue
        video_id = str(candidate.get("videoId") or "")
        if video_id and video_id not in seen:
            seen.add(video_id)
            deduped.append(candidate)
    return deduped


class Progress:
    def __init__(self, total: int, quiet: bool, show_bar: bool) -> None:
        self.total = total
        self.quiet = quiet
        self.bar = None
        if show_bar and tqdm is not None:
            self.bar = tqdm(total=total, desc="Поиск", unit="трек", dynamic_ncols=True)

    def update(self, index: int, track: YandexTrack, match: TrackMatch) -> None:
        if self.bar is not None:
            self.bar.update(1)
            self.bar.set_postfix_str(match.status)
            return
        if not self.quiet:
            print(f"[{index}/{self.total}] {format_track(track)} -> {format_match(match)}")
            return
        if self.total >= 25 and index % 25 == 0:
            print(f"... {index}/{self.total}", file=sys.stderr)

    def close(self) -> None:
        if self.bar is not None:
            self.bar.close()


def match_tracks(
    yt,
    tracks: Sequence[YandexTrack],
    args: argparse.Namespace,
    cache: SearchCache | None,
    state: dict[str, Any] | None,
    state_path: Path | None,
    progress: Progress,
) -> tuple[list[TrackMatch], bool]:
    matches: list[TrackMatch] = []
    total = len(tracks)
    interrupted = False
    by_position: dict[int, dict[str, Any]] = {}
    if state:
        for record in state.get("tracks", {}).values():
            if isinstance(record, dict) and record.get("position") is not None:
                by_position[int(record["position"])] = record

    for index, track in enumerate(tracks, 1):
        record = None
        if state:
            record = state.get("tracks", {}).get(track.key)
            if not isinstance(record, dict):
                fallback = by_position.get(track.position)
                if fallback and fallback.get("title") in (None, track.title):
                    record = fallback
        retry = bool(
            record
            and getattr(args, "retry_missing", False)
            and record.get("status") in (STATUS_NOT_FOUND, STATUS_ERROR)
        )
        if record and not retry:
            match = match_from_state(track, record)
            matches.append(match)
            progress.update(index, track, match)
            continue

        did_search = False
        if not track.available or not track.title:
            match = TrackMatch(track, STATUS_UNAVAILABLE, note="трек недоступен в Яндекс.Музыке")
        else:
            try:
                did_search = True
                candidates = find_candidates(
                    yt,
                    track,
                    args.search_limit,
                    args.allow_videos,
                    args.min_score,
                    cache,
                )
                match = choose_match(track, candidates, args.min_score, args.alternatives)
            except KeyboardInterrupt:
                interrupted = True
                print("\nПрервано. Сохраняю частичный результат...", file=sys.stderr)
                break
            except Exception as exc:
                match = TrackMatch(track, STATUS_ERROR, note=str(exc))

        if getattr(args, "explain", False) and match.parts:
            parts = match.parts
            duration = f"{parts.duration:.2f}" if parts.duration is not None else "—"
            print(
                f"    score={parts.total:.3f} title={parts.title:.3f} "
                f"artists={parts.artists:.3f} duration={duration} penalty={parts.penalty:.2f}",
                file=sys.stderr,
            )
        matches.append(match)
        progress.update(index, track, match)
        if state is not None:
            state["tracks"][track.key] = state_record(match)
            if state_path is not None:
                save_state(state_path, state)
        if did_search and args.delay > 0 and index < total:
            time.sleep(args.delay)

    return matches, interrupted


# ---------------------------------------------------------------------------
# Состояние, отчёт, decisions и review
# ---------------------------------------------------------------------------


def _chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-")
    return cleaned[:100] or "playlist"


def new_state(source_key: str, source_url: str) -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "version": STATE_VERSION,
        "source": source_key,
        "source_url": source_url,
        "target_playlist_id": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "tracks": {},
    }


def load_state(path: Path, source_key: str, source_url: str) -> tuple[dict[str, Any], bool]:
    if not path.is_file():
        return new_state(source_key, source_url), False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! состояние {path} не прочитано: {exc}", file=sys.stderr)
        return new_state(source_key, source_url), False
    if data.get("version") != STATE_VERSION or data.get("source") != source_key:
        print(
            f"  ! состояние {path} относится к другому источнику или версии; начинаю заново",
            file=sys.stderr,
        )
        return new_state(source_key, source_url), False
    if not isinstance(data.get("tracks"), dict):
        data["tracks"] = {}
    return data, True


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def config_parts(parts: ScoreParts | None) -> dict[str, Any] | None:
    if parts is None:
        return None
    return {
        "total": parts.total,
        "title": parts.title,
        "artists": parts.artists,
        "duration": parts.duration,
        "penalty": parts.penalty,
    }


def parts_from_config(data: dict[str, Any] | None) -> ScoreParts | None:
    if not isinstance(data, dict):
        return None
    return ScoreParts(
        total=float(data.get("total", 0.0)),
        title=float(data.get("title", 0.0)),
        artists=float(data.get("artists", 0.0)),
        duration=data.get("duration"),
        penalty=float(data.get("penalty", 1.0)),
    )


def compact_candidate(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not candidate:
        return None
    album = candidate.get("album")
    artists = [
        {"name": artist.get("name")}
        for artist in (candidate.get("artists") or [])
        if isinstance(artist, dict) and artist.get("name")
    ]
    return {
        "videoId": candidate.get("videoId"),
        "title": candidate.get("title"),
        "artists": artists,
        "album": {"name": album.get("name")} if isinstance(album, dict) and album.get("name") else None,
        "duration_seconds": candidate.get("duration_seconds"),
        "duration": candidate.get("duration"),
        "isAvailable": candidate.get("isAvailable"),
    }


def state_record(match: TrackMatch) -> dict[str, Any]:
    return {
        "position": match.track.position,
        "title": match.track.title,
        "status": match.status,
        "video_id": match.video_id,
        "score": round(match.score, 4),
        "note": match.note,
        "decided": match.decided,
        "parts": config_parts(match.parts),
        "candidate": compact_candidate(match.candidate),
        "alternatives": [
            {"candidate": compact_candidate(item.candidate), "parts": config_parts(item.parts)}
            for item in match.alternatives
        ],
    }


def match_from_state(track: YandexTrack, record: dict[str, Any]) -> TrackMatch:
    candidate = record.get("candidate")
    alternatives = []
    for item in record.get("alternatives") or []:
        if not isinstance(item, dict) or not isinstance(item.get("candidate"), dict):
            continue
        alternatives.append(
            ScoredCandidate(item["candidate"], parts_from_config(item.get("parts")) or ScoreParts(0, 0, 0, None))
        )
    return TrackMatch(
        track=track,
        status=str(record.get("status") or STATUS_NOT_FOUND),
        candidate=candidate if isinstance(candidate, dict) else None,
        score=float(record.get("score") or 0.0),
        note=str(record.get("note") or ""),
        parts=parts_from_config(record.get("parts")),
        alternatives=alternatives,
        decided=bool(record.get("decided")),
    )


def format_track(track: YandexTrack) -> str:
    artist = ", ".join(track.artists) if track.artists else "?"
    return f"{artist} — {track.title or '?'}"


def format_candidate(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    artists = ", ".join(_candidate_artists(candidate)) or "?"
    return f"{artists} — {candidate.get('title') or '?'}"


def format_match(match: TrackMatch) -> str:
    if match.status == STATUS_MATCHED:
        return f"OK ({match.score:.2f}) {format_candidate(match.candidate)}"
    if match.status == STATUS_AMBIGUOUS:
        return f"?? ({match.score:.2f}) {format_candidate(match.candidate)}"
    if match.status == STATUS_NOT_FOUND:
        return "-- не найдено"
    if match.status == STATUS_UNAVAILABLE:
        return "-- недоступно в Яндексе"
    if match.status == STATUS_SKIPPED:
        return "-- пропущено вручную"
    if match.status == STATUS_DUPLICATE:
        return "-- дубликат"
    return f"!! {match.note}"


def mark_duplicates(matches: list[TrackMatch]) -> None:
    seen: dict[str, int] = {}
    for match in matches:
        if match.status not in (STATUS_MATCHED, STATUS_AMBIGUOUS):
            continue
        video_id = match.video_id
        if not video_id:
            continue
        if video_id in seen:
            match.status = STATUS_DUPLICATE
            match.note = f"совпадает с позицией {seen[video_id]}"
        else:
            seen[video_id] = match.track.position


def format_duration_ms(duration_ms: int | None) -> str:
    if not duration_ms:
        return ""
    total_seconds = round(duration_ms / 1000)
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


def format_duration_seconds(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def write_report(matches: Sequence[TrackMatch], path: Path, alternatives: int = DEFAULT_ALTERNATIVES) -> None:
    fieldnames = [
        "position",
        "status",
        "yandex_title",
        "yandex_artists",
        "yandex_duration",
        "yandex_url",
        "youtube_title",
        "youtube_artists",
        "youtube_album",
        "youtube_duration",
        "video_id",
        "youtube_url",
        "score",
        "title_score",
        "artist_score",
        "duration_score",
        "penalty",
        "note",
    ]
    for index in range(1, alternatives + 1):
        fieldnames.extend(
            [
                f"candidate{index}_video_id",
                f"candidate{index}_url",
                f"candidate{index}_title",
                f"candidate{index}_artists",
                f"candidate{index}_score",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for match in matches:
            candidate = match.candidate or {}
            parts = match.parts
            row: dict[str, Any] = {
                "position": match.track.position,
                "status": match.status,
                "yandex_title": match.track.title,
                "yandex_artists": ", ".join(match.track.artists),
                "yandex_duration": format_duration_ms(match.track.duration_ms),
                "yandex_url": yandex_track_url(match.track),
                "youtube_title": candidate.get("title", ""),
                "youtube_artists": ", ".join(_candidate_artists(candidate)),
                "youtube_album": _candidate_album(candidate),
                "youtube_duration": format_duration_seconds(_duration_seconds(candidate)),
                "video_id": match.video_id or "",
                "youtube_url": WATCH_URL.format(match.video_id) if match.video_id else "",
                "score": f"{match.score:.3f}" if match.score else "",
                "title_score": f"{parts.title:.3f}" if parts else "",
                "artist_score": f"{parts.artists:.3f}" if parts else "",
                "duration_score": f"{parts.duration:.3f}" if parts and parts.duration is not None else "",
                "penalty": f"{parts.penalty:.2f}" if parts else "",
                "note": match.note,
            }
            pool = match.alternatives
            if not pool and match.candidate:
                pool = [ScoredCandidate(match.candidate, match.parts or ScoreParts(match.score, 0, 0, None))]
            for index, item in enumerate(pool[:alternatives], 1):
                row[f"candidate{index}_video_id"] = item.candidate.get("videoId", "")
                row[f"candidate{index}_url"] = (
                    WATCH_URL.format(item.candidate.get("videoId")) if item.candidate.get("videoId") else ""
                )
                row[f"candidate{index}_title"] = item.candidate.get("title", "")
                row[f"candidate{index}_artists"] = ", ".join(_candidate_artists(item.candidate))
                row[f"candidate{index}_score"] = f"{item.parts.total:.3f}"
            writer.writerow(row)


def write_candidates(matches: Sequence[TrackMatch], path: Path, alternatives: int = DEFAULT_ALTERNATIVES) -> None:
    fieldnames = [
        "position",
        "key",
        "status",
        "chosen",
        "yandex_title",
        "yandex_artists",
        "yandex_duration",
        "rank",
        "video_id",
        "url",
        "title",
        "artists",
        "album",
        "duration",
        "score",
        "title_score",
        "artist_score",
        "duration_score",
        "penalty",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for match in matches:
            pool = match.alternatives
            if not pool and match.candidate:
                pool = [ScoredCandidate(match.candidate, match.parts or ScoreParts(match.score, 0, 0, None))]
            for rank, item in enumerate(pool[:alternatives], 1):
                candidate = item.candidate
                writer.writerow(
                    {
                        "position": match.track.position,
                        "key": match.track.key,
                        "status": match.status,
                        "chosen": "1" if match.video_id == candidate.get("videoId") else "",
                        "yandex_title": match.track.title,
                        "yandex_artists": ", ".join(match.track.artists),
                        "yandex_duration": format_duration_ms(match.track.duration_ms),
                        "rank": rank,
                        "video_id": candidate.get("videoId", ""),
                        "url": WATCH_URL.format(candidate.get("videoId")) if candidate.get("videoId") else "",
                        "title": candidate.get("title", ""),
                        "artists": ", ".join(_candidate_artists(candidate)),
                        "album": _candidate_album(candidate),
                        "duration": format_duration_seconds(_duration_seconds(candidate)),
                        "score": f"{item.parts.total:.3f}",
                        "title_score": f"{item.parts.title:.3f}",
                        "artist_score": f"{item.parts.artists:.3f}",
                        "duration_score": f"{item.parts.duration:.3f}" if item.parts.duration is not None else "",
                        "penalty": f"{item.parts.penalty:.2f}",
                    }
                )


def write_decisions_template(
    matches: Sequence[TrackMatch],
    path: Path,
    overwrite: bool = False,
) -> bool:
    if path.exists() and not overwrite:
        return False
    fieldnames = [
        "position",
        "key",
        "yandex_artists",
        "yandex_title",
        "status",
        "recommended_video_id",
        "chosen_video_id",
        "action",
        "note",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for match in matches:
            recommended = match.video_id or ""
            chosen = recommended if match.status == STATUS_MATCHED else ""
            action = "skip" if match.status == STATUS_SKIPPED else ""
            writer.writerow(
                {
                    "position": match.track.position,
                    "key": match.track.key,
                    "yandex_artists": ", ".join(match.track.artists),
                    "yandex_title": match.track.title,
                    "status": match.status,
                    "recommended_video_id": recommended,
                    "chosen_video_id": chosen,
                    "action": action,
                    "note": match.note,
                }
            )
    return True


def load_decisions(path: Path) -> dict[str, dict[str, str]]:
    decisions: dict[str, dict[str, str]] = {}
    if not path.is_file():
        return decisions
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row.get("key") or "").strip()
            position = (row.get("position") or "").strip()
            if not key and position:
                key = f"pos:{position}"
            if not key:
                continue
            decisions[key] = {
                "chosen_video_id": (row.get("chosen_video_id") or row.get("video_id") or "").strip(),
                "action": (row.get("action") or "").strip().casefold(),
            }
    return decisions


def apply_decisions(matches: Sequence[TrackMatch], decisions: dict[str, dict[str, str]]) -> int:
    applied = 0
    for match in matches:
        decision = decisions.get(match.track.key) or decisions.get(f"pos:{match.track.position}")
        if not decision:
            continue
        action = decision.get("action", "")
        video_id = decision.get("chosen_video_id", "")
        if action in {"skip", "exclude", "no", "пропустить"}:
            match.status = STATUS_SKIPPED
            match.note = "исключено в decisions.csv"
            match.decided = True
            applied += 1
            continue
        if not video_id:
            continue
        if not VIDEO_ID_RE.fullmatch(video_id):
            match.note = f"некорректный video_id в decisions.csv: {video_id!r}"
            applied += 1
            continue
        if match.status == STATUS_MATCHED and match.video_id == video_id:
            continue
        if match.status in (STATUS_DUPLICATE,):
            continue
        selected = None
        for item in match.alternatives:
            if item.candidate.get("videoId") == video_id:
                selected = item
                break
        if selected is not None:
            match.candidate = selected.candidate
            match.parts = selected.parts
            match.score = selected.parts.total
        else:
            match.candidate = {"videoId": video_id, "title": "", "artists": []}
            match.parts = None
            match.score = 1.0
        match.status = STATUS_MATCHED
        match.note = "выбрано вручную"
        match.decided = True
        applied += 1
    return applied


REVIEW_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Разбор совпадений — __TITLE__</title>
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background: #f6f7f9; color: #1f2328; }
header { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; padding: 12px 20px; background: #fff; border-bottom: 1px solid #d8dee4; }
header h1 { font-size: 18px; margin: 0 auto 0 0; }
button { padding: 8px 12px; border: 1px solid #d0d7de; border-radius: 8px; background: #f6f8fa; cursor: pointer; font: inherit; }
button.primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
main { padding: 8px 0 40px; }
.track { margin: 12px 20px; padding: 14px; background: #fff; border: 1px solid #d8dee4; border-radius: 10px; }
.track h2 { margin: 0 0 4px; font-size: 16px; }
.meta { color: #59636e; font-size: 13px; margin-bottom: 10px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #eaeef2; margin-left: 8px; }
.badge.matched { background: #dafbe1; color: #116329; }
.badge.ambiguous { background: #fff8c5; color: #7d4e00; }
.badge.not_found, .badge.error, .badge.unavailable { background: #ffebe9; color: #a40e26; }
.badge.skipped, .badge.duplicate { background: #eaeef2; color: #59636e; }
.options { display: grid; gap: 8px; }
.option { display: grid; grid-template-columns: 24px 1fr auto; gap: 10px; align-items: start; padding: 8px; border: 1px solid #e1e4e8; border-radius: 8px; }
.option:hover { background: #f6f8fa; }
.option input { margin-top: 4px; }
.option .title { font-weight: 600; }
.option .sub { color: #59636e; font-size: 13px; }
.score { font-variant-numeric: tabular-nums; color: #59636e; font-size: 13px; white-space: nowrap; }
details { margin-top: 8px; }
summary { cursor: pointer; color: #1f6feb; font-size: 14px; }
.manual { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
.manual input[type=text] { width: 240px; padding: 6px 8px; border: 1px solid #d0d7de; border-radius: 6px; font: inherit; }
a { color: #1f6feb; text-decoration: none; }
a:hover { text-decoration: underline; }
.hint { color: #59636e; font-size: 13px; margin: 6px 20px 0; }
</style>
</head>
<body>
<header>
  <h1>Разбор совпадений</h1>
  <span id="stats" class="meta"></span>
  <button id="reset">Сбросить</button>
  <button id="export" class="primary">Скачать decisions.csv</button>
</header>
<p class="hint">Выберите кандидата или вставьте свой video_id. «Скачать decisions.csv» и запустите тот же перенос с <code>--resume</code>; строка с action=skip исключает трек.</p>
<main id="rows"></main>
<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const STORAGE = 'y2y-review:' + __KEY__;
let state = {};
try { state = JSON.parse(localStorage.getItem(STORAGE) || '{}') || {}; } catch (e) { state = {}; }
const rows = document.getElementById('rows');
const esc = (value) => String(value == null ? '' : value);

function save() {
  try { localStorage.setItem(STORAGE, JSON.stringify(state)); } catch (e) { /* file:// может запрещать storage */ }
  updateStats();
}

function rowState(row) {
  return state[row.key] || { video_id: row.selected || '', skip: row.skip ? true : false };
}

function updateStats() {
  let chosen = 0, skipped = 0, missing = 0;
  for (const row of DATA) {
    const value = rowState(row);
    if (value.skip) skipped++;
    else if (String(value.video_id || '').trim()) chosen++;
    else missing++;
  }
  document.getElementById('stats').textContent = `${DATA.length} треков: выбрано ${chosen}, пропущено ${skipped}, без решения ${missing}`;
}

function render() {
  for (const row of DATA) {
    const value = rowState(row);
    const section = document.createElement('section');
    section.className = 'track';

    const title = document.createElement('h2');
    title.textContent = `${row.position}. ${esc(row.source.artists) || '?'} — ${esc(row.source.title) || '?'}`;
    const badge = document.createElement('span');
    badge.className = 'badge ' + row.status;
    badge.textContent = row.status;
    title.appendChild(badge);
    section.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = `${row.source.duration || '—'} · ${row.source.album || ''}`;
    if (row.source.url) {
      const link = document.createElement('a');
      link.href = row.source.url;
      link.target = '_blank';
      link.textContent = ' Открыть в Яндексе';
      meta.appendChild(link);
    }
    section.appendChild(meta);

    const options = document.createElement('div');
    options.className = 'options';
    for (const candidate of row.candidates) {
      const label = document.createElement('label');
      label.className = 'option';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'pos-' + row.position;
      radio.value = candidate.video_id;
      radio.checked = String(value.video_id || '') === String(candidate.video_id);
      radio.addEventListener('change', () => {
        state[row.key] = { video_id: candidate.video_id, skip: false };
        save();
        syncManual(row);
      });
      const text = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'title';
      name.textContent = esc(candidate.title) || '?';
      const sub = document.createElement('div');
      sub.className = 'sub';
      sub.textContent = `${esc(candidate.artists) || '?'} · ${esc(candidate.album) || '—'} · ${esc(candidate.duration) || '—'}`;
      text.appendChild(name);
      text.appendChild(sub);
      if (candidate.url) {
        const link = document.createElement('a');
        link.href = candidate.url;
        link.target = '_blank';
        link.className = 'sub';
        link.textContent = 'YouTube';
        text.appendChild(link);
      }
      const score = document.createElement('div');
      score.className = 'score';
      score.textContent = `${candidate.score}${candidate.penalty < 1 ? ' · ×' + candidate.penalty : ''}`;
      label.appendChild(radio);
      label.appendChild(text);
      label.appendChild(score);
      options.appendChild(label);
    }
    if (!row.candidates.length) {
      const empty = document.createElement('div');
      empty.className = 'meta';
      empty.textContent = 'Подходящих кандидатов нет — вставьте video_id вручную или откройте поиск.';
      options.appendChild(empty);
    }
    section.appendChild(options);

    const manual = document.createElement('div');
    manual.className = 'manual';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'video_id, например dQw4w9WgXcQ';
    input.value = String(value.video_id || '');
    input.addEventListener('input', () => {
      const text = input.value.trim();
      state[row.key] = { video_id: text, skip: text ? false : !!(state[row.key] || {}).skip };
      save();
      for (const radio of section.querySelectorAll('input[type=radio]')) {
        radio.checked = radio.value === text;
      }
    });
    const skipLabel = document.createElement('label');
    const skip = document.createElement('input');
    skip.type = 'checkbox';
    skip.checked = !!value.skip;
    skip.addEventListener('change', () => {
      state[row.key] = { video_id: input.value.trim(), skip: skip.checked };
      save();
    });
    skipLabel.appendChild(skip);
    skipLabel.appendChild(document.createTextNode(' пропустить'));
    manual.appendChild(input);
    manual.appendChild(skipLabel);
    if (row.search_url) {
      const search = document.createElement('a');
      search.href = row.search_url;
      search.target = '_blank';
      search.textContent = 'Искать в YouTube Music';
      manual.appendChild(search);
    }
    section.appendChild(manual);
    section.dataset.key = row.key;
    section.dataset.manual = '1';
    section._syncManual = () => syncManual(row, section);
    rows.appendChild(section);
  }
  updateStats();
}

function syncManual(row, section) {
  const target = section || document.querySelector(`section[data-key="${CSS.escape(row.key)}"]`);
  if (!target) return;
  const input = target.querySelector('.manual input[type=text]');
  const skip = target.querySelector('.manual input[type=checkbox]');
  const value = rowState(row);
  if (input) input.value = String(value.video_id || '');
  if (skip) skip.checked = !!value.skip;
}

function csvCell(value) {
  const text = String(value == null ? '' : value);
  return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
}

document.getElementById('export').addEventListener('click', () => {
  const lines = ['position,key,chosen_video_id,action'];
  for (const row of DATA) {
    const value = rowState(row);
    const video = String(value.video_id || '').trim();
    const action = value.skip ? 'skip' : (video ? 'add' : '');
    lines.push([row.position, row.key, video, action].map(csvCell).join(','));
  }
  const blob = new Blob(['\\ufeff' + lines.join('\\n') + '\\n'], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'decisions.csv';
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
});

document.getElementById('reset').addEventListener('click', () => {
  state = {};
  save();
  rows.innerHTML = '';
  render();
});

render();
</script>
</body>
</html>
"""


def write_review(
    matches: Sequence[TrackMatch],
    path: Path,
    source_key: str,
    title: str,
    alternatives: int = DEFAULT_ALTERNATIVES,
) -> None:
    rows_payload = []
    for match in matches:
        pool = match.alternatives
        if not pool and match.candidate:
            pool = [ScoredCandidate(match.candidate, match.parts or ScoreParts(match.score, 0, 0, None))]
        candidates = []
        for item in pool[:alternatives]:
            candidate = item.candidate
            video_id = candidate.get("videoId") or ""
            candidates.append(
                {
                    "video_id": video_id,
                    "url": WATCH_URL.format(video_id) if video_id else "",
                    "title": candidate.get("title") or "",
                    "artists": ", ".join(_candidate_artists(candidate)),
                    "album": _candidate_album(candidate),
                    "duration": format_duration_seconds(_duration_seconds(candidate)),
                    "score": f"{item.parts.total:.2f}",
                    "penalty": round(item.parts.penalty, 2),
                }
            )
        rows_payload.append(
            {
                "position": match.track.position,
                "key": match.track.key,
                "status": match.status,
                # Преследуем только уверенные совпадения: ambiguous/not_found
                # по умолчанию пропускаются, чтобы экспорт не добавлял их молча.
                "selected": (match.video_id or "") if match.status == STATUS_MATCHED else "",
                "skip": match.status == STATUS_SKIPPED,
                "source": {
                    "title": match.track.title,
                    "artists": ", ".join(match.track.artists),
                    "album": match.track.album or "",
                    "duration": format_duration_ms(match.track.duration_ms),
                    "url": yandex_track_url(match.track),
                },
                "candidates": candidates,
                "search_url": YT_SEARCH_URL.format(quote_plus(build_query(match.track))),
            }
        )
    payload = json.dumps(rows_payload, ensure_ascii=False).replace("</", "<\\/")
    document = (
        REVIEW_TEMPLATE.replace("__TITLE__", html.escape(title or "плейлист"))
        .replace("__KEY__", json.dumps(source_key, ensure_ascii=False))
        .replace("__DATA__", payload)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


# ---------------------------------------------------------------------------
# Запись в YouTube Music
# ---------------------------------------------------------------------------


def _write_ok(response: Any) -> bool:
    if isinstance(response, str):
        return "SUCCEEDED" in response.upper()
    if isinstance(response, dict):
        return str(response.get("status", "")).endswith("SUCCEEDED")
    return False


def fetch_target_items(yt, playlist_id: str) -> dict[str, str | None]:
    data = yt.get_playlist(playlist_id, limit=None)
    items: dict[str, str | None] = {}
    for item in data.get("tracks") or []:
        if not isinstance(item, dict):
            continue
        video_id = item.get("videoId")
        if video_id:
            items[str(video_id)] = item.get("setVideoId")
    return items


def create_playlist(yt, name: str, description: str, privacy: str) -> str:
    response = with_retries(
        lambda: yt.create_playlist(name, description, privacy_status=privacy),
        label="создание плейлиста",
    )
    if isinstance(response, str) and response:
        return response
    raise RuntimeError(f"Не удалось создать плейлист: {response}")


def add_videos(
    yt,
    playlist_id: str,
    video_ids: Sequence[str],
    quiet: bool,
    existing: set[str] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    if existing is None:
        try:
            existing = set(fetch_target_items(yt, playlist_id))
        except Exception as exc:
            if not quiet:
                print(f"  ! не удалось прочитать текущий плейлист: {exc}")
            existing = set()

    pending = [video_id for video_id in dict.fromkeys(video_ids) if video_id not in existing]
    added: list[str] = []
    failed: list[tuple[str, str]] = []
    for chunk in _chunked(pending, YT_ADD_CHUNK_SIZE):
        try:
            response = with_retries(
                lambda chunk=chunk: yt.add_playlist_items(playlist_id, chunk),
                label=f"добавление {len(chunk)} треков",
            )
            if not _write_ok(response):
                raise RuntimeError(f"API отказал: {response}")
            added.extend(chunk)
            continue
        except Exception as exc:
            if not quiet:
                print(f"  ! пакет не добавился ({exc}), пробую по одному")
        for video_id in chunk:
            try:
                response = with_retries(
                    lambda video_id=video_id: yt.add_playlist_items(playlist_id, [video_id]),
                    label=f"добавление {video_id}",
                    attempts=2,
                )
                if not _write_ok(response):
                    raise RuntimeError(f"API отказал: {response}")
                added.append(video_id)
            except Exception as item_exc:
                failed.append((video_id, str(item_exc)))
    return added, failed


def verify_added(yt, playlist_id: str, added: Sequence[str]) -> list[str]:
    if not added:
        return []
    try:
        current = set(fetch_target_items(yt, playlist_id))
    except Exception:
        return []
    return [video_id for video_id in added if video_id not in current]


def remove_videos(yt, playlist_id: str, items: Sequence[dict[str, Any]]) -> list[str]:
    if not items:
        return []
    response = with_retries(
        lambda: yt.remove_playlist_items(playlist_id, list(items)),
        label=f"удаление {len(items)} треков",
    )
    if not _write_ok(response):
        raise RuntimeError(f"API отказал при удалении: {response}")
    return [str(item.get("videoId")) for item in items if item.get("videoId")]


def eligible_video_ids(matches: Sequence[TrackMatch], add_ambiguous: bool) -> list[str]:
    statuses = {STATUS_MATCHED}
    if add_ambiguous:
        statuses.add(STATUS_AMBIGUOUS)
    return [
        video_id
        for match in matches
        if match.video_id and match.status in statuses
        for video_id in [str(match.video_id)]
    ]


def sync_target(
    yt,
    matches: Sequence[TrackMatch],
    state: dict[str, Any],
    args: argparse.Namespace,
    state_path: Path,
    quiet: bool,
    source_title: str,
) -> tuple[str, list[str], list[tuple[str, str]], list[str]]:
    desired = list(dict.fromkeys(eligible_video_ids(matches, args.add_ambiguous)))
    target_id = state.get("target_playlist_id") or getattr(args, "playlist_id", None)

    existing: dict[str, str | None] = {}
    created = False
    if target_id:
        try:
            existing = fetch_target_items(yt, target_id)
        except Exception as exc:
            raise RuntimeError(
                f"Не удалось прочитать плейлист {target_id}: {exc}. "
                "Проверьте --playlist-id или удалите target_playlist_id из состояния."
            ) from exc
    else:
        name = args.name or source_title
        description = args.description or f"Перенесено из Яндекс.Музыки: {source_title}"
        print(f"Создаю плейлист «{name}» ({args.privacy})...")
        target_id = create_playlist(yt, name, description, args.privacy)
        state["target_playlist_id"] = target_id
        save_state(state_path, state)
        created = True

    removed: list[str] = []
    if args.remove_missing and target_id and not created and existing:
        desired_set = set(desired)
        known = {
            str(record.get("video_id"))
            for record in state.get("tracks", {}).values()
            if isinstance(record, dict) and record.get("video_id")
        }
        stale = [video_id for video_id in known if video_id not in desired_set and video_id in existing]
        items = [
            {"videoId": video_id, "setVideoId": existing[video_id]}
            for video_id in stale
            if existing.get(video_id)
        ]
        if items:
            removed = remove_videos(yt, target_id, items)
            print(f"Удалено из плейлиста: {len(removed)}")

    added, failed = add_videos(yt, target_id, desired, quiet, existing=set(existing))
    # API может вернуть ошибку «уже есть в плейлисте»: если видео фактически
    # присутствует, это не неудача добавления.
    if failed:
        try:
            current = set(fetch_target_items(yt, target_id))
        except Exception:
            current = set()
        recovered = [video_id for video_id, _ in failed if video_id in current]
        if recovered:
            added.extend(recovered)
            failed = [(video_id, reason) for video_id, reason in failed if video_id not in current]
    missing = verify_added(yt, target_id, added)
    for video_id in missing:
        failed.append((video_id, "не найден в плейлисте после добавления"))

    if created:
        print(f"Плейлист создан: {PLAYLIST_URL.format(target_id)}")
    print(f"Добавлено {len(added) - len(missing)} из {len(desired)}.")
    if failed:
        print(f"Не удалось добавить {len(failed)}. Подробности в отчёте.")
    return target_id, added, failed, removed


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def resolve_out_dir(args: argparse.Namespace) -> Path:
    if args.out:
        return Path(args.out)
    return Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")


def resolve_state_path(args: argparse.Namespace, source_key: str) -> Path:
    if args.state:
        return Path(args.state)
    return DEFAULT_STATE_DIR / f"{slugify(source_key)}.json"


def summary_counts(matches: Sequence[TrackMatch]) -> dict[str, int]:
    counts = Counter(match.status for match in matches)
    return {status: counts.get(status, 0) for status in ALL_STATUSES}


def build_summary(
    *,
    source: SourcePlaylist | None,
    source_title: str,
    matches: Sequence[TrackMatch],
    started_at: str,
    out_dir: Path,
    dry_run: bool,
    interrupted: bool,
    target_id: str | None,
    added: Sequence[str],
    failed: Sequence[tuple[str, str]],
    removed: Sequence[str],
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": error is None and not failed,
        "error": error,
        "source": source.key if source else None,
        "source_url": source.url if source else None,
        "title": source_title,
        "started_at": started_at,
        "finished_at": now_iso(),
        "dry_run": dry_run,
        "interrupted": interrupted,
        "track_count": len(matches),
        "counts": summary_counts(matches),
        "target_playlist_id": target_id,
        "playlist_url": PLAYLIST_URL.format(target_id) if target_id else None,
        "added": len(added),
        "failed": [{"video_id": video_id, "error": reason} for video_id, reason in failed],
        "removed": list(removed),
        "report": str(out_dir / "report.csv"),
        "candidates": str(out_dir / "candidates.csv"),
        "review": str(out_dir / "review.html"),
        "decisions": str(out_dir / "decisions.csv"),
    }


def write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_transfer(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    started_at = now_iso()
    source: SourcePlaylist | None = None
    source_title = ""
    out_dir = resolve_out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)

    token = args.yandex_token or os.environ.get("YANDEX_MUSIC_TOKEN")
    if args.yandex_token_file:
        token = Path(args.yandex_token_file).read_text(encoding="utf-8").strip()
    if token:
        token = token.strip()
        if token.lower().startswith("oauth "):
            token = token[6:].strip()

    try:
        print("Подключаюсь к Яндекс.Музыке...")
        yandex = build_yandex_client(token)

        if args.liked:
            ref = PlaylistRef(liked=True)
        else:
            if not args.yandex_playlist:
                raise RuntimeError("не задан плейлист: используйте --yandex-playlist, --liked или --config")
            ref = parse_playlist_ref(args.yandex_playlist)

        source = load_source(yandex, ref)
        source_title = source.title
        print(f"Плейлист: {source_title} ({source.track_count or '?'} треков)")
        tracks = resolve_yandex_tracks(yandex, source)
        if source.track_count and len(tracks) != source.track_count:
            print(
                f"  ! в плейлисте {source.track_count} треков, разобрано {len(tracks)}; "
                "недоступные позиции останутся в отчёте",
                file=sys.stderr,
            )
        if args.max_tracks:
            tracks = tracks[: args.max_tracks]
        if not tracks:
            raise RuntimeError("В плейлисте не оказалось треков.")

        state_path = resolve_state_path(args, source.key)
        resume = args.resume or args.update
        state = new_state(source.key, source.url)
        if resume:
            state, loaded = load_state(state_path, source.key, source.url)
            if loaded:
                print(f"Продолжаю по состоянию {state_path}")
            else:
                print(f"  ! состояния нет, начинаю с нуля: {state_path}", file=sys.stderr)
        if args.playlist_id:
            state["target_playlist_id"] = args.playlist_id
            save_state(state_path, state)

        auth_path = resolve_auth_path(args.ytm_auth)
        yt = None
        if not (args.reuse_cache and args.dry_run and not auth_path):
            print("Подключаюсь к YouTube Music...")
            yt = build_ytmusic(
                auth_path,
                args.dry_run,
                _proxy_config(args.proxy),
                args.language,
                args.location,
            )
            if auth_path:
                check_ytm_auth(yt)

        cache = None
        if not args.no_cache:
            cache = SearchCache(Path(args.cache_dir), refresh=args.refresh_cache, offline=args.reuse_cache)

        use_bar = bool(
            tqdm is not None
            and (args.progress or (not args.quiet and getattr(sys.stderr, "isatty", lambda: False)()))
        )
        progress = Progress(len(tracks), args.quiet, use_bar)
        print(f"Ищу совпадения для {len(tracks)} треков...")
        try:
            matches, interrupted = match_tracks(
                yt, tracks, args, cache, state, state_path, progress
            )
        finally:
            progress.close()

        apply_decisions_applied = 0
        decisions_path = Path(args.decisions) if args.decisions else out_dir / "decisions.csv"
        if decisions_path.is_file() and not args.refresh_decisions:
            decisions = load_decisions(decisions_path)
            apply_decisions_applied = apply_decisions(matches, decisions)
            if apply_decisions_applied:
                print(f"Применено решений из {decisions_path}: {apply_decisions_applied}")
        mark_duplicates(matches)

        for match in matches:
            state["tracks"][match.track.key] = state_record(match)
        save_state(state_path, state)

        report_path = out_dir / "report.csv"
        candidates_path = out_dir / "candidates.csv"
        review_path = out_dir / "review.html"
        write_report(matches, report_path, args.alternatives)
        write_candidates(matches, candidates_path, args.alternatives)
        wrote_decisions = write_decisions_template(
            matches, decisions_path, overwrite=args.refresh_decisions
        )
        if not args.no_review:
            write_review(matches, review_path, source.key, source_title, args.alternatives)

        print_summary(matches, report_path)
        if wrote_decisions:
            print(f"Решения: {decisions_path}")
        if not args.no_review:
            print(f"Разбор: {review_path}")
        else:
            review_path = Path("")

        summary = build_summary(
            source=source,
            source_title=source_title,
            matches=matches,
            started_at=started_at,
            out_dir=out_dir,
            dry_run=args.dry_run,
            interrupted=interrupted,
            target_id=state.get("target_playlist_id"),
            added=[],
            failed=[],
            removed=[],
        )
        summary["decisions_applied"] = apply_decisions_applied

        error_count = summary["counts"].get(STATUS_ERROR, 0)
        if interrupted:
            summary["ok"] = False
            summary["playlist_url"] = (
                PLAYLIST_URL.format(state["target_playlist_id"])
                if state.get("target_playlist_id")
                else None
            )
            if args.json_summary:
                write_json(args.json_summary, summary)
            return 130, summary

        if args.dry_run:
            print("Dry-run: плейлист в YouTube Music не создавался.")
            code = 0 if not error_count else 2
            summary["ok"] = code == 0
            if args.json_summary:
                write_json(args.json_summary, summary)
            return code, summary

        desired = eligible_video_ids(matches, args.add_ambiguous)
        if not desired and not state.get("target_playlist_id"):
            raise RuntimeError(
                "Не нашлось ни одного трека для добавления, плейлист не создаю. "
                "Проверьте review.html и decisions.csv."
            )

        target_id, added, failed, removed = sync_target(
            yt,
            matches,
            state,
            args,
            state_path,
            args.quiet,
            source_title,
        )
        if failed:
            reasons = {video_id: reason for video_id, reason in failed}
            for match in matches:
                if match.video_id in reasons:
                    match.status = STATUS_ERROR
                    match.note = f"не добавлен в YouTube Music: {reasons[match.video_id]}"
            write_report(matches, report_path, args.alternatives)
            write_candidates(matches, candidates_path, args.alternatives)
            if not args.no_review:
                write_review(matches, review_path, source.key, source_title, args.alternatives)
            for match in matches:
                state["tracks"][match.track.key] = state_record(match)
            save_state(state_path, state)

        summary = build_summary(
            source=source,
            source_title=source_title,
            matches=matches,
            started_at=started_at,
            out_dir=out_dir,
            dry_run=False,
            interrupted=False,
            target_id=target_id,
            added=added,
            failed=failed,
            removed=removed,
        )
        summary["decisions_applied"] = apply_decisions_applied
        summary["ok"] = not failed and not error_count
        if args.json_summary:
            write_json(args.json_summary, summary)

        if target_id:
            print(f"Плейлист: {PLAYLIST_URL.format(target_id)}")
        code = 0 if not failed and not error_count else 2
        return code, summary
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        summary = build_summary(
            source=source,
            source_title=source_title or "",
            matches=[],
            started_at=started_at,
            out_dir=out_dir,
            dry_run=args.dry_run,
            interrupted=False,
            target_id=None,
            added=[],
            failed=[],
            removed=[],
            error=str(exc),
        )
        if args.json_summary:
            write_json(args.json_summary, summary)
        raise


def print_summary(matches: Sequence[TrackMatch], report_path: Path) -> None:
    counts = Counter(match.status for match in matches)
    print()
    print(
        "Итог: "
        f"matched={counts[STATUS_MATCHED]}, "
        f"ambiguous={counts[STATUS_AMBIGUOUS]}, "
        f"not_found={counts[STATUS_NOT_FOUND]}, "
        f"unavailable={counts[STATUS_UNAVAILABLE]}, "
        f"error={counts[STATUS_ERROR]}, "
        f"duplicate={counts[STATUS_DUPLICATE]}, "
        f"skipped={counts[STATUS_SKIPPED]}"
    )
    print(f"Отчёт: {report_path}")


def check_auth(args: argparse.Namespace) -> int:
    token = args.yandex_token or os.environ.get("YANDEX_MUSIC_TOKEN")
    if args.yandex_token_file:
        token = Path(args.yandex_token_file).read_text(encoding="utf-8").strip()
    if token:
        token = token.strip()
        if token.lower().startswith("oauth "):
            token = token[6:].strip()
    print("Яндекс.Музыка...")
    build_yandex_client(token)
    print("  OK")
    auth_path = resolve_auth_path(args.ytm_auth)
    if not auth_path:
        print("YouTube Music: авторизация не задана (доступен только поиск, запись невозможна).")
        return 0
    print(f"YouTube Music ({auth_path})...")
    yt = build_ytmusic(
        auth_path,
        False,
        _proxy_config(args.proxy),
        args.language,
        args.location,
    )
    check_ytm_auth(yt)
    print("  OK")
    return 0


def load_config(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise RuntimeError(f"Файл конфигурации не найден: {path}")
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Конфигурация должна быть TOML-таблицей")
    return data


def _apply_config_value(
    args: argparse.Namespace,
    entry: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
    attr: str,
    cli_default: Any,
) -> None:
    value = entry.get(key, defaults.get(key))
    if value is None:
        return
    if getattr(args, attr, None) == cli_default:
        setattr(args, attr, value)


def run_config(args: argparse.Namespace) -> tuple[int, list[dict[str, Any]]]:
    config = load_config(args.config)
    defaults = config.get("defaults") or {}
    entries = config.get("sync") or config.get("playlists") or []
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("В конфигурации нет ни одной секции [[sync]]")
    if not isinstance(defaults, dict):
        raise RuntimeError("Секция [defaults] должна быть таблицей")

    default_out = args.out or defaults.get("out") or (Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S"))
    base_out = Path(str(default_out))
    summaries: list[dict[str, Any]] = []
    codes: list[int] = []

    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("playlist_name") or f"playlist-{index}")
        if args.only and args.only.casefold() not in {name.casefold(), str(index)}:
            continue
        ns = copy.copy(args)
        ns.json_summary = None
        _apply_config_value(ns, entry, defaults, "min_score", "min_score", DEFAULT_MIN_SCORE)
        _apply_config_value(ns, entry, defaults, "search_limit", "search_limit", DEFAULT_SEARCH_LIMIT)
        _apply_config_value(ns, entry, defaults, "allow_videos", "allow_videos", False)
        _apply_config_value(ns, entry, defaults, "add_ambiguous", "add_ambiguous", False)
        _apply_config_value(ns, entry, defaults, "delay", "delay", 0.3)
        _apply_config_value(ns, entry, defaults, "privacy", "privacy", "PRIVATE")
        _apply_config_value(ns, entry, defaults, "cache_dir", "cache_dir", str(DEFAULT_CACHE_DIR))
        _apply_config_value(ns, entry, defaults, "retry_missing", "retry_missing", False)
        _apply_config_value(ns, entry, defaults, "alternatives", "alternatives", DEFAULT_ALTERNATIVES)
        _apply_config_value(ns, entry, defaults, "remove_missing", "remove_missing", False)
        _apply_config_value(ns, entry, defaults, "language", "language", "en")
        _apply_config_value(ns, entry, defaults, "location", "location", "")
        _apply_config_value(ns, entry, defaults, "proxy", "proxy", None)
        _apply_config_value(ns, entry, defaults, "description", "description", None)

        source = entry.get("source")
        if source and str(source).casefold() in {"liked", "likes", "favorites", "мне нравится"}:
            ns.liked = True
            ns.yandex_playlist = None
        elif source:
            ns.liked = False
            ns.yandex_playlist = str(source)
        elif not ns.liked:
            summaries.append({"name": name, "error": "не задан source", "ok": False})
            codes.append(1)
            continue

        target = entry.get("target") or ns.playlist_id
        ns.playlist_id = str(target) if target else None
        ns.update = bool(ns.playlist_id)
        ns.resume = True
        ns.name = str(entry.get("playlist_name") or entry.get("name") or ns.name or "") or None
        # out в секции [[sync]] — точный каталог для этой записи;
        # [defaults].out / --out — общий каталог, внутри него подпапка по имени.
        entry_out = entry.get("out")
        ns.out = str(Path(str(entry_out))) if entry_out else str(base_out / slugify(name))
        if args.state:
            state_path = Path(args.state)
            ns.state = str(state_path.with_name(f"{state_path.stem}-{slugify(name)}{state_path.suffix or '.json'}"))
        if args.decisions:
            decisions_path = Path(args.decisions)
            ns.decisions = str(
                decisions_path.with_name(f"{decisions_path.stem}-{slugify(name)}{decisions_path.suffix or '.csv'}")
            )

        print(f"\n=== {name} ===")
        try:
            code, summary = run_transfer(ns)
        except Exception as exc:
            print(f"Ошибка в «{name}»: {exc}", file=sys.stderr)
            code = 1
            summary = {"name": name, "ok": False, "error": str(exc)}
        summary["name"] = name
        summaries.append(summary)
        codes.append(code)

    if not summaries:
        raise RuntimeError("Ни одна секция [[sync]] не выбрана (--only)")
    if args.json_summary:
        payload = summaries[0] if len(summaries) == 1 else {"runs": summaries}
        write_json(args.json_summary, payload)
    return (max(codes) if codes else 1), summaries


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Перенос и синхронизация плейлистов Яндекс.Музыки в YouTube Music.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--yandex-playlist",
        help="ссылка на плейлист Яндекс.Музыки, owner:kind или kind своего плейлиста",
    )
    parser.add_argument("--liked", action="store_true", help="перенести «Мне нравится» (нужен токен)")
    parser.add_argument("--yandex-token", help="OAuth-токен Яндекса (иначе YANDEX_MUSIC_TOKEN)")
    parser.add_argument("--yandex-token-file", help="файл с OAuth-токеном Яндекса")
    parser.add_argument("--ytm-auth", help="путь к oauth.json/browser.json (или YTMUSIC_AUTH)")
    parser.add_argument("--check-auth", action="store_true", help="проверить авторизацию и выйти")
    parser.add_argument("--name", help="название нового плейлиста (по умолчанию исходное)")
    parser.add_argument("--description", help="описание нового плейлиста")
    parser.add_argument(
        "--privacy",
        choices=["PRIVATE", "UNLISTED", "PUBLIC"],
        default="PRIVATE",
        help="доступ к новому плейлисту",
    )
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="минимальная оценка совпадения")
    parser.add_argument(
        "--search-limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        help="сколько кандидатов учитывать на один запрос",
    )
    parser.add_argument(
        "--alternatives",
        type=int,
        default=DEFAULT_ALTERNATIVES,
        help="сколько кандидатов сохранять в отчёт и review",
    )
    parser.add_argument(
        "--allow-videos",
        action="store_true",
        help="искать в видео, если среди песен нет уверенного совпадения",
    )
    parser.add_argument(
        "--add-ambiguous",
        action="store_true",
        help="добавлять неоднозначные совпадения (по умолчанию пропускаются)",
    )
    parser.add_argument(
        "--skip-ambiguous",
        action="store_true",
        help="устарело: ambiguous теперь пропускаются по умолчанию",
    )
    parser.add_argument("--max-tracks", type=int, help="обработать только первые N треков")
    parser.add_argument("--dry-run", action="store_true", help="только поиск и отчёт, без записи")
    parser.add_argument("--out", help="каталог прогона (по умолчанию runs/<дата-время>)")
    parser.add_argument("--resume", action="store_true", help="продолжить по сохранённому состоянию")
    parser.add_argument("--retry-missing", action="store_true", help="при resume повторить not_found/error")
    parser.add_argument("--state", help="путь к файлу состояния")
    parser.add_argument("--playlist-id", help="дополнять существующий плейлист YouTube Music")
    parser.add_argument("--update", action="store_true", help="синхронизировать с существующим плейлистом")
    parser.add_argument("--remove-missing", action="store_true", help="удалять из цели ранее перенесённые треки")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="каталог кэша поиска")
    parser.add_argument("--no-cache", action="store_true", help="не использовать кэш поиска")
    parser.add_argument("--refresh-cache", action="store_true", help="игнорировать и обновить кэш")
    parser.add_argument("--reuse-cache", "--offline", dest="reuse_cache", action="store_true", help="только кэш, без поиска")
    parser.add_argument("--decisions", help="CSV с ручными решениями (по умолчанию <out>/decisions.csv)")
    parser.add_argument("--refresh-decisions", action="store_true", help="перезаписать decisions.csv")
    parser.add_argument("--no-review", action="store_true", help="не создавать review.html")
    parser.add_argument("--explain", action="store_true", help="печатать компоненты оценки")
    parser.add_argument("--progress", action="store_true", help="показывать прогресс-бар")
    parser.add_argument("--quiet", action="store_true", help="не печатать каждый трек")
    parser.add_argument("--debug", action="store_true", help="показывать traceback")
    parser.add_argument("--delay", type=float, default=0.3, help="пауза между поисковыми запросами, секунд")
    parser.add_argument("--proxy", help="HTTP(S)-прокси, например http://127.0.0.1:8080")
    parser.add_argument("--language", default="en", help="язык ответов YouTube Music")
    parser.add_argument("--location", default="", help="локация YouTube Music (может влиять на каталог)")
    parser.add_argument("--config", help="TOML-конфигурация с секциями [[sync]]")
    parser.add_argument("--only", help="выполнить только одну секцию sync по имени или номеру")
    parser.add_argument("--json-summary", help="записать JSON-сводку")
    parser.add_argument("--log-file", help="дублировать вывод в файл")

    args = parser.parse_args(argv)

    if args.max_tracks is not None and args.max_tracks <= 0:
        parser.error("--max-tracks должен быть больше нуля")
    if args.search_limit <= 0:
        parser.error("--search-limit должен быть больше нуля")
    if args.alternatives <= 0:
        parser.error("--alternatives должен быть больше нуля")
    if not 0.0 <= args.min_score <= 1.0:
        parser.error("--min-score должен быть в диапазоне 0..1")
    if args.delay < 0:
        parser.error("--delay не может быть отрицательным")
    if args.reuse_cache and args.refresh_cache:
        parser.error("--reuse-cache и --refresh-cache несовместимы")
    if args.reuse_cache and args.no_cache:
        parser.error("--reuse-cache и --no-cache несовместимы")
    if args.config:
        if args.yandex_playlist or args.liked:
            parser.error("--config нельзя сочетать с --yandex-playlist/--liked")
    elif not args.check_auth and not args.yandex_playlist and not args.liked:
        parser.error("нужен --yandex-playlist, --liked, --config или --check-auth")
    if args.update and not args.playlist_id and not args.config:
        parser.error("--update требует --playlist-id или target в конфиге")
    if args.remove_missing and not args.playlist_id and not args.config:
        parser.error("--remove-missing требует --playlist-id или target в конфиге")
    if args.only and not args.config:
        parser.error("--only используется только с --config")
    return args


class Tee:
    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            try:
                stream.write(data)
            except UnicodeEncodeError:
                stream.write(data.encode(stream.encoding or "utf-8", errors="replace").decode(stream.encoding or "utf-8"))
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        try:
            return bool(self.streams[0].isatty())
        except Exception:
            return False


class LogContext:
    def __init__(self, path: str | None) -> None:
        self.path = path
        self.handle = None
        self.stdout = None
        self.stderr = None

    def __enter__(self) -> "LogContext":
        if not self.path:
            return self
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.handle = target.open("w", encoding="utf-8")
        self.handle.write(f"# {now_iso()} {' '.join(sys.argv)}\n")
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        sys.stdout = Tee(sys.stdout, self.handle)  # type: ignore[assignment]
        sys.stderr = Tee(sys.stderr, self.handle)  # type: ignore[assignment]
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.stdout is not None:
            sys.stdout = self.stdout
        if self.stderr is not None:
            sys.stderr = self.stderr
        if self.handle is not None:
            self.handle.flush()
            self.handle.close()


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_stdio()
    logging_context = LogContext(args.log_file)

    try:
        with logging_context:
            if args.check_auth:
                return check_auth(args)
            if args.config:
                code, _summaries = run_config(args)
                return code
            code, summary = run_transfer(args)
            return code
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130
    except Exception as exc:
        if args.debug:
            raise
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
