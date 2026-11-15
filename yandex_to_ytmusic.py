#!/usr/bin/env python3
"""Перенос плейлиста из Яндекс.Музыки в YouTube Music."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator, Sequence, TypeVar
from urllib.parse import urlparse

DEFAULT_MIN_SCORE = 0.72
DEFAULT_SEARCH_LIMIT = 5
YANDEX_CHUNK_SIZE = 100
YT_ADD_CHUNK_SIZE = 50
PLAYLIST_URL = "https://music.youtube.com/playlist?list={}"

T = TypeVar("T")

STATUS_MATCHED = "matched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NOT_FOUND = "not_found"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"
STATUS_DUPLICATE = "duplicate"

NOISE_WORD_RE = re.compile(
    r"\b(feat(?:uring)?|ft|prod(?:uced)?|remaster(?:ed)?|version|edit|remix|mix|"
    r"live|instrumental|acoustic|demo|bonus|official|audio|video|lyrics?|cover|"
    r"radio|extended|deluxe|explicit|mono|stereo|bootleg|sped\s*up|slowed|"
    r"nightcore)\b",
    re.IGNORECASE,
)
BRACKET_RE = re.compile(r"[\(\[\{]([^)\]\}]*)[\)\]\}]")


@dataclass
class YandexTrack:
    position: int
    title: str
    artists: list[str]
    duration_ms: int | None = None
    available: bool = True


@dataclass
class PlaylistRef:
    owner: str | None = None
    kind: str | None = None
    uuid: str | None = None


@dataclass
class TrackMatch:
    track: YandexTrack
    status: str
    candidate: dict[str, Any] | None = None
    score: float = 0.0
    note: str = ""

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


def _drop_noise_brackets(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        return " " if NOISE_WORD_RE.search(inner) else f" {inner} "

    return BRACKET_RE.sub(replace, text)


def _drop_noise_tail(text: str) -> str:
    parts = re.split(r"\s+-\s+", text)
    while len(parts) > 1 and NOISE_WORD_RE.search(parts[-1]):
        parts.pop()
    return " - ".join(parts)


def clean_text(text: str) -> str:
    text = _drop_noise_tail(_drop_noise_brackets(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    text = clean_text(text).replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _ratio(first: str, second: str) -> float:
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    return SequenceMatcher(None, first, second).ratio()


def _title_score(target_title: str, youtube_title: str) -> float:
    target = normalize(target_title)
    variants = [normalize(youtube_title)]
    if " - " in youtube_title:
        variants.append(normalize(youtube_title.split(" - ", 1)[1]))
    return max((_ratio(target, variant) for variant in variants), default=0.0)


def _candidate_artists(candidate: dict[str, Any]) -> list[str]:
    artists = candidate.get("artists") or []
    names = []
    for artist in artists:
        if isinstance(artist, dict):
            name = artist.get("name")
            if name:
                names.append(str(name))
    return names


def _artists_score(target_artists: Sequence[str], youtube_artists: Sequence[str]) -> float:
    targets = [normalize(name) for name in target_artists]
    targets = [name for name in targets if name]
    candidates = [normalize(name) for name in youtube_artists]
    candidates = [name for name in candidates if name]
    if not targets:
        return 0.5
    if not candidates:
        return 0.0
    best = 0.0
    for target in targets:
        for candidate in candidates:
            score = _ratio(target, candidate)
            if target in candidate or candidate in target:
                score = max(score, 0.95)
            best = max(best, score)
    return best


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


def score_candidate(track: YandexTrack, candidate: dict[str, Any]) -> float:
    title_score = _title_score(track.title, str(candidate.get("title") or ""))
    artists_score = _artists_score(track.artists, _candidate_artists(candidate))
    duration_score = _duration_score(track.duration_ms, _duration_seconds(candidate))
    if duration_score is None:
        return 0.62 * title_score + 0.38 * artists_score
    return 0.55 * title_score + 0.30 * artists_score + 0.15 * duration_score


def build_query(track: YandexTrack) -> str:
    title = clean_text(track.title) or track.title
    artist = track.artists[0] if track.artists else ""
    return f"{artist} {title}".strip()


def choose_match(track: YandexTrack, candidates: list[dict[str, Any]], min_score: float) -> TrackMatch:
    scored = sorted(
        ((score_candidate(track, candidate), candidate) for candidate in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored:
        return TrackMatch(track, STATUS_NOT_FOUND, note="ничего не найдено")
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < min_score:
        return TrackMatch(
            track,
            STATUS_NOT_FOUND,
            best,
            best_score,
            f"лучший результат {best_score:.2f} ниже порога {min_score:.2f}",
        )
    if runner_up >= best_score - 0.05 and best_score < 0.9:
        return TrackMatch(
            track,
            STATUS_AMBIGUOUS,
            best,
            best_score,
            f"похожие результаты ({best_score:.2f} и {runner_up:.2f})",
        )
    return TrackMatch(track, STATUS_MATCHED, best, best_score)


def _chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def with_retries(fn, label: str, attempts: int = 3, base_delay: float = 1.5):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(f"  ! {label}: {exc}. Повтор через {delay:.0f} с ({attempt}/{attempts})")
            time.sleep(delay)


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
                "(токен нужен для приватных плейлистов и большинства запросов)."
            ) from exc
        raise RuntimeError(f"Не удалось подключиться к Яндекс.Музыке, проверьте токен: {exc}") from exc


def build_ytmusic(auth_path: str | None, dry_run: bool):
    try:
        from ytmusicapi import YTMusic
    except ImportError as exc:
        raise RuntimeError(
            "Не установлен пакет ytmusicapi. Выполните: pip install -r requirements.txt"
        ) from exc
    if auth_path:
        if not Path(auth_path).is_file():
            raise RuntimeError(f"Файл авторизации не найден: {auth_path}")
        return YTMusic(auth_path)
    if not dry_run:
        raise RuntimeError(
            "Для создания плейлиста нужна авторизация YouTube Music. "
            "Выполните `ytmusicapi browser` и передайте --ytm-auth browser.json"
        )
    return YTMusic()


def load_playlist(client, ref: PlaylistRef):
    if ref.uuid:
        playlist = client.playlist(ref.uuid)
    else:
        playlist = client.users_playlists(ref.kind, ref.owner)
    if isinstance(playlist, list):
        playlist = playlist[0] if playlist else None
    if playlist is None:
        raise RuntimeError("Плейлист не найден или недоступен. Проверьте ссылку и токен.")
    return playlist


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
    return YandexTrack(position, title, artists, int(duration_ms) if duration_ms else None, available)


def resolve_yandex_tracks(client, playlist) -> list[YandexTrack]:
    shorts = list(getattr(playlist, "tracks", None) or [])
    track_count = getattr(playlist, "track_count", None)
    if track_count and len(shorts) < track_count:
        try:
            fetched_shorts = playlist.fetch_tracks() or []
            if len(fetched_shorts) > len(shorts):
                shorts = list(fetched_shorts)
        except Exception as exc:
            print(f"  ! не удалось дозагрузить треки плейлиста: {exc}")

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

        by_key = {}
        by_id = {}
        for track in fetched:
            if track is None:
                continue
            key = _track_key(track)
            if key:
                by_key[key] = track
            track_id = getattr(track, "id", None)
            if track_id is not None:
                by_id[str(track_id)] = track

        for offset, (position, track_id) in enumerate(chunk):
            track = by_key.get(track_id) or by_id.get(track_id.split(":", 1)[0])
            if track is None and offset < len(fetched):
                track = fetched[offset]
            if track is None:
                resolved[position] = YandexTrack(
                    position + 1, f"трек {track_id}", [], None, False
                )
            else:
                resolved[position] = _to_yandex_track(position + 1, track)

    return [track for track in resolved if track is not None]


def _search(yt, query: str, filter_: str, limit: int) -> list[dict[str, Any]]:
    results = with_retries(
        lambda: yt.search(query, filter=filter_, limit=limit),
        label=f"поиск «{query}»",
    )
    return [item for item in (results or []) if item.get("videoId")]


def find_candidates(
    yt,
    track: YandexTrack,
    limit: int,
    allow_videos: bool,
    min_score: float,
) -> list[dict[str, Any]]:
    query = build_query(track)
    candidates = _search(yt, query, "songs", limit)
    if allow_videos:
        best = max((score_candidate(track, candidate) for candidate in candidates), default=0.0)
        if best < min_score:
            candidates.extend(_search(yt, query, "videos", limit))
    if not candidates and track.title:
        alternative = clean_text(track.title) or track.title
        if alternative and alternative.casefold() != query.casefold():
            candidates = _search(yt, alternative, "songs", limit)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        video_id = str(candidate.get("videoId") or "")
        if video_id and video_id not in seen:
            seen.add(video_id)
            deduped.append(candidate)
    return deduped


def match_tracks(
    yt,
    tracks: Sequence[YandexTrack],
    min_score: float,
    limit: int,
    allow_videos: bool,
    delay: float,
    quiet: bool,
) -> list[TrackMatch]:
    matches: list[TrackMatch] = []
    total = len(tracks)
    for index, track in enumerate(tracks, 1):
        if not track.available or not track.title:
            matches.append(
                TrackMatch(track, STATUS_UNAVAILABLE, note="трек недоступен в Яндекс.Музыке")
            )
            continue
        try:
            candidates = find_candidates(yt, track, limit, allow_videos, min_score)
            match = choose_match(track, candidates, min_score)
        except Exception as exc:
            match = TrackMatch(track, STATUS_ERROR, note=str(exc))
        matches.append(match)
        if not quiet:
            print(f"[{index}/{total}] {format_track(track)} -> {format_match(match)}")
        if delay > 0 and index < total:
            time.sleep(delay)
    return matches


def format_track(track: YandexTrack) -> str:
    artist = ", ".join(track.artists) if track.artists else "?"
    return f"{artist} — {track.title or '?'}"


def format_match(match: TrackMatch) -> str:
    if match.status == STATUS_MATCHED:
        return f"OK ({match.score:.2f}) {format_candidate(match.candidate)}"
    if match.status == STATUS_AMBIGUOUS:
        return f"?? ({match.score:.2f}) {format_candidate(match.candidate)}"
    if match.status == STATUS_NOT_FOUND:
        return "-- не найдено"
    if match.status == STATUS_UNAVAILABLE:
        return "-- недоступно в Яндексе"
    return f"!! {match.note}"


def format_candidate(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    artists = ", ".join(_candidate_artists(candidate)) or "?"
    return f"{artists} — {candidate.get('title') or '?'}"


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


def write_report(matches: Sequence[TrackMatch], path: Path) -> None:
    fieldnames = [
        "position",
        "status",
        "yandex_title",
        "yandex_artists",
        "yandex_duration",
        "youtube_title",
        "youtube_artists",
        "youtube_duration",
        "video_id",
        "score",
        "note",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for match in matches:
            candidate = match.candidate or {}
            youtube_artists = _candidate_artists(candidate)
            writer.writerow(
                {
                    "position": match.track.position,
                    "status": match.status,
                    "yandex_title": match.track.title,
                    "yandex_artists": ", ".join(match.track.artists),
                    "yandex_duration": format_duration_ms(match.track.duration_ms),
                    "youtube_title": candidate.get("title", ""),
                    "youtube_artists": ", ".join(youtube_artists),
                    "youtube_duration": format_duration_seconds(_duration_seconds(candidate)),
                    "video_id": match.video_id or "",
                    "score": f"{match.score:.3f}" if match.score else "",
                    "note": match.note,
                }
            )


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
        f"duplicate={counts[STATUS_DUPLICATE]}"
    )
    print(f"Отчёт: {report_path}")


def add_videos(yt, playlist_id: str, video_ids: Sequence[str], quiet: bool) -> tuple[list[str], list[tuple[str, str]]]:
    added: list[str] = []
    failed: list[tuple[str, str]] = []
    for chunk in _chunked(list(video_ids), YT_ADD_CHUNK_SIZE):
        try:
            with_retries(
                lambda chunk=chunk: yt.add_playlist_items(playlist_id, chunk),
                label=f"добавление {len(chunk)} треков",
            )
            added.extend(chunk)
            continue
        except Exception as exc:
            if not quiet:
                print(f"  ! пакет не добавился целиком ({exc}), пробую по одному")
        for video_id in chunk:
            try:
                with_retries(
                    lambda video_id=video_id: yt.add_playlist_items(playlist_id, [video_id]),
                    label=f"добавление {video_id}",
                    attempts=2,
                )
                added.append(video_id)
            except Exception as item_exc:
                failed.append((video_id, str(item_exc)))
    return added, failed


def run(args: argparse.Namespace) -> int:
    ref = parse_playlist_ref(args.yandex_playlist)

    token = args.yandex_token or os.environ.get("YANDEX_MUSIC_TOKEN")
    if token:
        token = token.strip()
        if token.lower().startswith("oauth "):
            token = token[6:].strip()

    print("Подключаюсь к Яндекс.Музыке...")
    yandex = build_yandex_client(token)
    playlist = load_playlist(yandex, ref)
    source_title = str(playlist.title or args.name or "Плейлист из Яндекс.Музыки")
    print(f"Плейлист: {source_title} ({playlist.track_count or '?'} треков)")

    tracks = resolve_yandex_tracks(yandex, playlist)
    if args.max_tracks:
        tracks = tracks[: args.max_tracks]
    if not tracks:
        print("В плейлисте не оказалось треков.", file=sys.stderr)
        return 1

    print("Подключаюсь к YouTube Music...")
    yt = build_ytmusic(args.ytm_auth, args.dry_run)

    print(f"Ищу совпадения для {len(tracks)} треков...")
    matches = match_tracks(
        yt,
        tracks,
        args.min_score,
        args.search_limit,
        args.allow_videos,
        args.delay,
        args.quiet,
    )
    mark_duplicates(matches)

    report_path = Path(args.out) / "report.csv"
    write_report(matches, report_path)
    print_summary(matches, report_path)

    eligible_statuses = {STATUS_MATCHED}
    if not args.skip_ambiguous:
        eligible_statuses.add(STATUS_AMBIGUOUS)
    video_ids = [
        match.video_id
        for match in matches
        if match.video_id and match.status in eligible_statuses
    ]

    if args.dry_run:
        print("Dry-run: плейлист в YouTube Music не создавался.")
        return 0

    if not video_ids:
        print("Не нашлось ни одного трека для добавления, плейлист не создаю.", file=sys.stderr)
        return 1

    name = args.name or source_title
    description = args.description or f"Перенесено из Яндекс.Музыки: {source_title}"
    print(f"Создаю плейлист «{name}» ({args.privacy})...")
    try:
        playlist_id = with_retries(
            lambda: yt.create_playlist(name, description, privacy_status=args.privacy),
            label="создание плейлиста",
        )
    except Exception as exc:
        print(f"Не удалось создать плейлист: {exc}", file=sys.stderr)
        return 1
    if not isinstance(playlist_id, str) or not playlist_id:
        print(f"Неожиданный ответ при создании плейлиста: {playlist_id!r}", file=sys.stderr)
        return 1

    print(f"Добавляю {len(video_ids)} треков...")
    added, failed = add_videos(yt, playlist_id, video_ids, args.quiet)

    if failed:
        failed_reasons = dict(failed)
        for match in matches:
            video_id = match.video_id
            if video_id and video_id in failed_reasons:
                match.status = STATUS_ERROR
                match.note = f"не добавлен в YouTube Music: {failed_reasons[video_id]}"
        write_report(matches, report_path)

    print(f"Готово: {PLAYLIST_URL.format(playlist_id)}")
    print(f"Добавлено {len(added)} из {len(video_ids)}.")
    if failed:
        print(f"Не удалось добавить {len(failed)}. Подробности в отчёте: {report_path}")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Перенос плейлиста из Яндекс.Музыки в YouTube Music.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--yandex-playlist",
        required=True,
        help="ссылка на плейлист Яндекс.Музыки, owner:kind или kind своего плейлиста",
    )
    parser.add_argument(
        "--yandex-token",
        help="OAuth-токен Яндекс.Музыки (иначе берётся из YANDEX_MUSIC_TOKEN)",
    )
    parser.add_argument(
        "--ytm-auth",
        help="путь к browser.json или oauth.json, созданному ytmusicapi",
    )
    parser.add_argument("--name", help="название нового плейлиста (по умолчанию исходное)")
    parser.add_argument("--description", help="описание нового плейлиста")
    parser.add_argument(
        "--privacy",
        choices=["PRIVATE", "UNLISTED", "PUBLIC"],
        default="PRIVATE",
        help="доступ к новому плейлисту",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help="минимальная оценка совпадения",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        help="сколько результатов запрашивать у YouTube Music",
    )
    parser.add_argument(
        "--allow-videos",
        action="store_true",
        help="искать в видео, если среди песен нет уверенного совпадения",
    )
    parser.add_argument(
        "--skip-ambiguous",
        action="store_true",
        help="не добавлять неоднозначные совпадения",
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        help="обработать только первые N треков (для пробного запуска)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только найти совпадения и записать отчёт, без создания плейлиста",
    )
    parser.add_argument("--out", default="out", help="каталог для report.csv")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="пауза между поисковыми запросами, секунд",
    )
    parser.add_argument("--quiet", action="store_true", help="не печатать каждый трек")
    parser.add_argument("--debug", action="store_true", help="показывать traceback при ошибках")
    args = parser.parse_args(argv)
    if args.max_tracks is not None and args.max_tracks <= 0:
        parser.error("--max-tracks должен быть больше нуля")
    if args.search_limit <= 0:
        parser.error("--search-limit должен быть больше нуля")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
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
