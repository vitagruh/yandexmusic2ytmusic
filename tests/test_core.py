"""Тесты ядра переносчика: разбор ссылок, матчинг, состояние, отчёты, запись."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import yandex_to_ytmusic as y2y


# ---------------------------------------------------------------------------
# Разбор ссылок и текст
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://music.yandex.ru/users/ivan/playlists/3", ("ivan", "3", None, False)),
        ("https://music.yandex.ru/playlists/abc-def?utm=1", (None, None, "abc-def", False)),
        ("ivan:3", ("ivan", "3", None, False)),
        ("3", (None, "3", None, False)),
        ("liked", (None, None, None, True)),
    ],
)
def test_parse_playlist_ref(value, expected):
    ref = y2y.parse_playlist_ref(value)
    assert (ref.owner, ref.kind, ref.uuid, ref.liked) == expected


def test_parse_playlist_ref_invalid():
    with pytest.raises(ValueError):
        y2y.parse_playlist_ref("https://example.com/nothing")


def test_clean_text_strips_noise_and_keeps_meaning():
    assert y2y.clean_text("Song (Remastered 2011)") == "Song"
    assert y2y.clean_text("Song (Live at Wembley)") == "Song"
    assert y2y.clean_text("Song (feat. Someone)") == "Song"
    assert y2y.clean_text("Song - Radio Edit") == "Song"
    assert y2y.clean_text("Song (Part 2)") == "Song Part 2"
    assert y2y.clean_text("Unslept 0:00") == "Unslept"
    assert y2y.normalize("Ёлка & Медведь") == "елка and медведь"


# ---------------------------------------------------------------------------
# Матчинг
# ---------------------------------------------------------------------------


def make_track(title="Song", artists=("Artist",), duration_ms=200_000, **kwargs):
    return y2y.YandexTrack(1, title, list(artists), duration_ms, **kwargs)


def candidate(title="Song", artist="Artist", video_id="AAAAAAAAAAA", duration=200, **kwargs):
    data = {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}],
        "duration_seconds": duration,
    }
    data.update(kwargs)
    return data


def test_transliteration_matches_cyrillic_and_latin():
    track = make_track("Группа крови", ["Кино"], 300_000)
    item = candidate("Gruppa Krovi", "Kino", duration=300)
    match = y2y.choose_match(track, [item], min_score=0.72)
    assert match.status == y2y.STATUS_MATCHED
    assert match.score > 0.9


def test_cover_and_lullaby_are_penalized():
    track = make_track("Yesterday", ["The Beatles"])
    original = candidate("Yesterday", "The Beatles", video_id="ORIGINALAAA")
    lullaby_same_artist = candidate(
        "Yesterday (Lullaby Version)",
        "The Beatles",
        video_id="LULLABYAAAA",
        album={"name": "Lullaby Renditions of The Beatles"},
    )
    lullaby_other_artist = candidate(
        "Yesterday (Lullaby Version)",
        "Sparrow Sleeps",
        video_id="LULLABYBBBB",
        album={"name": "Lullaby Renditions of The Beatles"},
    )
    assert y2y.score_candidate_parts(track, lullaby_same_artist).penalty == pytest.approx(0.7)
    assert y2y.score_candidate_parts(track, lullaby_other_artist).penalty == pytest.approx(0.49)
    match = y2y.choose_match(track, [lullaby_same_artist], min_score=0.72)
    assert match.status == y2y.STATUS_NOT_FOUND
    assert "штраф" in match.note

    match = y2y.choose_match(track, [lullaby_other_artist, original], min_score=0.72)
    assert match.status == y2y.STATUS_MATCHED
    assert match.video_id == "ORIGINALAAA"


def test_wrong_artist_is_rejected_even_with_exact_title():
    track = make_track("Monica", ["Imagine Dragons"], duration_ms=200_000)
    impostor = candidate("Monica", "Igor Dragonov", video_id="IMPOSTORAAA", duration=200)
    parts = y2y.score_candidate_parts(track, impostor)
    assert parts.artists < y2y.ARTIST_FLOOR
    assert parts.penalty == pytest.approx(y2y.ARTIST_PENALTY)
    assert y2y.choose_match(track, [impostor], min_score=0.72).status == y2y.STATUS_NOT_FOUND


def test_wrong_song_by_same_artist_is_rejected():
    track = make_track("Make It All Right", ["The Offspring"], duration_ms=220_000)
    other = candidate("Get It Right (2008 Remaster)", "The Offspring", video_id="OTHERSONGAA", duration=200)
    parts = y2y.score_candidate_parts(track, other)
    assert parts.title < y2y.TITLE_FLOOR
    assert parts.penalty == pytest.approx(y2y.TITLE_PENALTY)
    assert y2y.choose_match(track, [other], min_score=0.72).status == y2y.STATUS_NOT_FOUND


def test_live_version_penalty():
    track = make_track("Song", ["Artist"])
    studio = candidate("Song", "Artist", video_id="STUDIOAAAAA")
    live = candidate("Song (Live)", "Artist", video_id="LIVEAAAAAAA")
    live_parts = y2y.score_candidate_parts(track, live)
    assert live_parts.penalty < 1.0
    assert y2y.score_candidate(track, studio) > y2y.score_candidate(track, live)


def test_unavailable_candidates_are_filtered():
    track = make_track()
    match = y2y.choose_match(track, [candidate(isAvailable=False)], min_score=0.5)
    assert match.status == y2y.STATUS_NOT_FOUND
    assert match.video_id is None


def test_close_candidates_become_ambiguous():
    track = make_track("Song", ["Artist"], duration_ms=None)
    options = [
        candidate("Song (Part 2)", "Artist", video_id="PARTTWOAAAA"),
        candidate("Song (Part 3)", "Artist", video_id="PARTTHREEAA"),
    ]
    match = y2y.choose_match(track, options, min_score=0.4)
    assert match.status == y2y.STATUS_AMBIGUOUS
    assert match.video_id is not None
    assert len(match.alternatives) == 2


def test_duration_component():
    track = make_track(duration_ms=200_000)
    exact = y2y.score_candidate_parts(track, candidate(duration=200))
    far = y2y.score_candidate_parts(track, candidate(duration=260, video_id="FARFARFARF"))
    assert exact.duration == pytest.approx(1.0)
    assert far.duration < 1.0
    assert exact.total > far.total


# ---------------------------------------------------------------------------
# Кэш поиска
# ---------------------------------------------------------------------------


class SearchStub:
    def __init__(self, results):
        self.results = results
        self.calls = 0

    def search(self, query, filter=None, limit=20):
        self.calls += 1
        return list(self.results)


def test_cache_roundtrip_and_offline(tmp_path):
    cache = y2y.SearchCache(tmp_path)
    stub = SearchStub([candidate()])
    first = y2y._search_cached(cache, stub, "Artist Song", "songs", 5)
    second = y2y._search_cached(cache, None, "Artist Song", "songs", 5)
    assert first == second
    assert stub.calls == 1

    offline = y2y.SearchCache(tmp_path, offline=True)
    with pytest.raises(RuntimeError, match="в кэше нет запроса"):
        y2y._search_cached(offline, None, "Unknown", "songs", 5)


def test_search_respects_limit():
    stub = SearchStub([candidate(video_id=f"VIDEO{i:06d}") for i in range(20)])
    assert len(y2y._search(stub, "q", "songs", 5)) == 5


# ---------------------------------------------------------------------------
# Состояние и resume
# ---------------------------------------------------------------------------


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    track = make_track(track_id="42", album_id="7")
    match = y2y.choose_match(track, [candidate()], min_score=0.5)
    state = y2y.new_state("src", "https://example.com")
    state["tracks"][track.key] = y2y.state_record(match)
    y2y.save_state(path, state)

    loaded, ok = y2y.load_state(path, "src", "https://example.com")
    assert ok
    restored = y2y.match_from_state(track, loaded["tracks"][track.key])
    assert restored.status == match.status
    assert restored.video_id == match.video_id
    assert restored.score == pytest.approx(match.score)
    assert restored.alternatives


def test_state_rejects_other_source(tmp_path):
    path = tmp_path / "state.json"
    state = y2y.new_state("src", "")
    state["tracks"]["x"] = {"position": 1}
    y2y.save_state(path, state)
    loaded, ok = y2y.load_state(path, "other", "")
    assert not ok
    assert loaded["tracks"] == {}


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


def test_decisions_skip_and_choose(tmp_path):
    first = make_track("Song", ["Artist"], track_id="1")
    second = make_track("Missing", ["Artist"], track_id="2")
    matches = [
        y2y.choose_match(first, [candidate()], min_score=0.5),
        y2y.choose_match(second, [], min_score=0.5),
    ]
    matches[1].alternatives = [y2y.ScoredCandidate(candidate("Other", video_id="BBBBBBBBBBB"), y2y.ScoreParts(0.5, 1, 1, None))]
    path = tmp_path / "decisions.csv"
    path.write_text(
        "position,key,chosen_video_id,action\n"
        f"1,{first.key},,skip\n"
        f"2,{second.key},BBBBBBBBBBB,add\n",
        encoding="utf-8",
    )
    applied = y2y.apply_decisions(matches, y2y.load_decisions(path))
    assert applied == 2
    assert matches[0].status == y2y.STATUS_SKIPPED
    assert matches[1].status == y2y.STATUS_MATCHED
    assert matches[1].video_id == "BBBBBBBBBBB"


def test_decisions_invalid_video_id():
    track = make_track(track_id="5")
    match = y2y.choose_match(track, [], min_score=0.5)
    decisions = {track.key: {"chosen_video_id": "bad id", "action": "add"}}
    assert y2y.apply_decisions([match], decisions) == 1
    assert "некорректный video_id" in match.note


# ---------------------------------------------------------------------------
# Отчёты
# ---------------------------------------------------------------------------


def test_report_contains_urls_and_components(tmp_path):
    track = make_track(track_id="1", album_id="2")
    match = y2y.choose_match(track, [candidate()], min_score=0.5)
    path = tmp_path / "report.csv"
    y2y.write_report([match], path)
    text = path.read_text(encoding="utf-8-sig")
    assert "yandex_url" in text
    assert "https://music.yandex.ru/album/2/track/1" in text
    assert "https://music.youtube.com/watch?v=AAAAAAAAAAA" in text
    assert "candidate1_video_id" in text


def test_review_preselects_only_matched(tmp_path):
    track = make_track(track_id="1")
    matched = y2y.choose_match(track, [candidate()], min_score=0.5)
    ambiguous = y2y.choose_match(make_track(track_id="2"), [], min_score=0.5)
    ambiguous.status = y2y.STATUS_AMBIGUOUS
    ambiguous.candidate = candidate(video_id="BBBBBBBBBBB")
    ambiguous.alternatives = [y2y.ScoredCandidate(ambiguous.candidate, y2y.ScoreParts(0.5, 1, 1, None))]
    path = tmp_path / "review.html"
    y2y.write_review([matched, ambiguous], path, "src", "Test")
    html_text = path.read_text(encoding="utf-8")
    match = re.search(r'<script id="data" type="application/json">(.*?)</script>', html_text, re.S)
    assert match
    rows = json.loads(match.group(1))
    assert rows[0]["selected"] == "AAAAAAAAAAA"
    assert rows[1]["selected"] == ""
    assert rows[1]["skip"] is False


# ---------------------------------------------------------------------------
# Запись в YouTube Music
# ---------------------------------------------------------------------------


class WriteStub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def add_playlist_items(self, playlist_id, video_ids):
        self.calls.append(list(video_ids))
        return self.responses.pop(0) if self.responses else "STATUS_SUCCEEDED"

    def get_playlist(self, playlist_id, limit=None):
        return {"tracks": []}

    def create_playlist(self, name, description, privacy_status="PRIVATE"):
        return "PL1"


def test_write_ok_variants():
    assert y2y._write_ok("STATUS_SUCCEEDED")
    assert y2y._write_ok("status_succeeded")
    assert y2y._write_ok({"status": "STATUS_SUCCEEDED"})
    assert not y2y._write_ok("STATUS_FAILED")
    assert not y2y._write_ok({"status": "STATUS_FAILED"})
    assert not y2y._write_ok(None)


def test_add_videos_reports_api_failure():
    stub = WriteStub(
        [
            {"status": "STATUS_FAILED"},  # пакет
            "STATUS_SUCCEEDED",  # первый поштучно
            {"status": "STATUS_FAILED"},  # второй поштучно
        ]
    )
    added, failed = y2y.add_videos(stub, "PL1", ["AAAAAAAAAAA", "BBBBBBBBBBB"], quiet=True, existing=set())
    assert added == ["AAAAAAAAAAA"]
    assert failed and failed[0][0] == "BBBBBBBBBBB"
    assert stub.calls == [["AAAAAAAAAAA", "BBBBBBBBBBB"], ["AAAAAAAAAAA"], ["BBBBBBBBBBB"]]


def test_add_videos_skips_existing():
    stub = WriteStub(["STATUS_SUCCEEDED"])
    added, failed = y2y.add_videos(
        stub, "PL1", ["AAAAAAAAAAA", "BBBBBBBBBBB"], quiet=True, existing={"AAAAAAAAAAA"}
    )
    assert added == ["BBBBBBBBBBB"]
    assert failed == []
    assert stub.calls == [["BBBBBBBBBBB"]]


def test_sync_target_surfaces_failed_writes(tmp_path):
    stub = WriteStub([{"status": "STATUS_FAILED"}] * 5)
    track = make_track(track_id="1")
    match = y2y.choose_match(track, [candidate()], min_score=0.5)
    state = y2y.new_state("src", "")
    args = SimpleNamespace(
        add_ambiguous=False,
        playlist_id=None,
        name="Test",
        description=None,
        privacy="PRIVATE",
        remove_missing=False,
    )
    target, added, failed, removed = y2y.sync_target(
        stub, [match], state, args, tmp_path / "state.json", True, "Test"
    )
    assert target == "PL1"
    assert added == []
    assert failed and failed[0][0] == "AAAAAAAAAAA"
    assert removed == []


def test_sync_target_recovers_failed_items_already_present(tmp_path):
    class RecoveredYT(WriteStub):
        def create_playlist(self, name, description, privacy_status="PRIVATE"):
            return "PL1"

        def get_playlist(self, playlist_id, limit=None):
            return {"tracks": [{"videoId": "AAAAAAAAAAA", "setVideoId": "set1"}]}

    stub = RecoveredYT([{"status": "STATUS_FAILED"}] * 5)
    track = make_track(track_id="1")
    match = y2y.choose_match(track, [candidate()], min_score=0.5)
    state = y2y.new_state("src", "")
    args = SimpleNamespace(
        add_ambiguous=False,
        playlist_id=None,
        name="Test",
        description=None,
        privacy="PRIVATE",
        remove_missing=False,
    )
    target, added, failed, _removed = y2y.sync_target(
        stub, [match], state, args, tmp_path / "state.json", True, "Test"
    )
    assert target == "PL1"
    assert added == ["AAAAAAAAAAA"]
    assert failed == []


def test_eligible_video_ids_respects_ambiguous_flag():
    matched = y2y.TrackMatch(make_track(track_id="1"), y2y.STATUS_MATCHED, candidate())
    ambiguous = y2y.TrackMatch(
        make_track(track_id="2"), y2y.STATUS_AMBIGUOUS, candidate(video_id="BBBBBBBBBBB")
    )
    skipped = y2y.TrackMatch(make_track(track_id="3"), y2y.STATUS_SKIPPED, candidate(video_id="CCCCCCCCCCC"))
    assert y2y.eligible_video_ids([matched, ambiguous, skipped], False) == ["AAAAAAAAAAA"]
    assert y2y.eligible_video_ids([matched, ambiguous, skipped], True) == [
        "AAAAAAAAAAA",
        "BBBBBBBBBBB",
    ]


def test_mark_duplicates():
    first = y2y.TrackMatch(make_track(track_id="1"), y2y.STATUS_MATCHED, candidate())
    second = y2y.TrackMatch(make_track(track_id="2"), y2y.STATUS_MATCHED, candidate())
    y2y.mark_duplicates([first, second])
    assert first.status == y2y.STATUS_MATCHED
    assert second.status == y2y.STATUS_DUPLICATE


# ---------------------------------------------------------------------------
# run_transfer целиком (dry-run и offline-кэш)
# ---------------------------------------------------------------------------


class FakeYT:
    def __init__(self):
        self.searches = 0

    def search(self, query, filter=None, limit=20):
        self.searches += 1
        if "Song One" in query and filter == "songs":
            return [candidate("Song One", "Artist One")]
        return []


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(y2y.time, "sleep", lambda *_: None)


def patch_environment(monkeypatch, tracks):
    source = y2y.SourcePlaylist("src", "Test playlist", "https://music.yandex.ru/users/u/playlists/1", len(tracks), [])
    monkeypatch.setattr(y2y, "build_yandex_client", lambda token: object())
    monkeypatch.setattr(y2y, "load_source", lambda client, ref: source)
    monkeypatch.setattr(y2y, "resolve_yandex_tracks", lambda client, src: list(tracks))
    monkeypatch.setattr(y2y, "check_ytm_auth", lambda yt: None)
    monkeypatch.delenv("YTMUSIC_AUTH", raising=False)
    monkeypatch.delenv("YTM_AUTH", raising=False)
    return source


def test_run_transfer_dry_run_and_offline_cache(tmp_path, monkeypatch, capsys, no_sleep):
    tracks = [
        y2y.YandexTrack(1, "Song One", ["Artist One"], 200_000, track_id="1", album_id="10"),
        y2y.YandexTrack(2, "Missing Song", ["Artist Two"], 180_000, track_id="2", album_id="20"),
    ]
    patch_environment(monkeypatch, tracks)
    fake_yt = FakeYT()
    monkeypatch.setattr(y2y, "build_ytmusic", lambda *a, **k: fake_yt)
    cache_dir = tmp_path / "cache"

    args = y2y.parse_args(
        [
            "--yandex-playlist",
            "https://music.yandex.ru/users/u/playlists/1",
            "--dry-run",
            "--out",
            str(tmp_path / "run1"),
            "--state",
            str(tmp_path / "state1.json"),
            "--cache-dir",
            str(cache_dir),
            "--delay",
            "0",
            "--quiet",
        ]
    )
    code, summary = y2y.run_transfer(args)
    assert code == 0
    assert summary["counts"][y2y.STATUS_MATCHED] == 1
    assert summary["counts"][y2y.STATUS_NOT_FOUND] == 1
    run1 = tmp_path / "run1"
    for name in ("report.csv", "candidates.csv", "review.html", "decisions.csv"):
        assert (run1 / name).is_file()
    assert fake_yt.searches > 0

    json_path = tmp_path / "summary.json"
    args2 = y2y.parse_args(
        [
            "--yandex-playlist",
            "https://music.yandex.ru/users/u/playlists/1",
            "--dry-run",
            "--reuse-cache",
            "--out",
            str(tmp_path / "run2"),
            "--state",
            str(tmp_path / "state2.json"),
            "--cache-dir",
            str(cache_dir),
            "--delay",
            "0",
            "--quiet",
            "--json-summary",
            str(json_path),
        ]
    )
    monkeypatch.setattr(
        y2y,
        "build_ytmusic",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("сеть не должна использоваться")),
    )
    code, summary = y2y.run_transfer(args2)
    assert code == 0
    assert summary["counts"][y2y.STATUS_MATCHED] == 1
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["counts"][y2y.STATUS_MATCHED] == 1
    assert payload["dry_run"] is True


def test_run_transfer_reports_write_failure(tmp_path, monkeypatch, capsys, no_sleep):
    tracks = [y2y.YandexTrack(1, "Song One", ["Artist One"], 200_000, track_id="1", album_id="10")]
    patch_environment(monkeypatch, tracks)

    class FailingYT(FakeYT):
        def create_playlist(self, name, description, privacy_status="PRIVATE"):
            return "PL1"

        def add_playlist_items(self, playlist_id, video_ids):
            return {"status": "STATUS_FAILED"}

        def get_playlist(self, playlist_id, limit=None):
            return {"tracks": []}

    monkeypatch.setattr(y2y, "build_ytmusic", lambda *a, **k: FailingYT())
    args = y2y.parse_args(
        [
            "--yandex-playlist",
            "https://music.yandex.ru/users/u/playlists/1",
            "--out",
            str(tmp_path / "run"),
            "--state",
            str(tmp_path / "state.json"),
            "--delay",
            "0",
            "--quiet",
            "--no-cache",
            "--json-summary",
            str(tmp_path / "summary.json"),
        ]
    )
    code, summary = y2y.run_transfer(args)
    assert code == 2
    assert summary["ok"] is False
    assert summary["added"] == 0
    assert summary["failed"][0]["video_id"] == "AAAAAAAAAAA"
    report = (tmp_path / "run" / "report.csv").read_text(encoding="utf-8-sig")
    assert "не добавлен в YouTube Music" in report
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["failed"][0]["video_id"] == "AAAAAAAAAAA"


# ---------------------------------------------------------------------------
# CLI и конфигурация
# ---------------------------------------------------------------------------


def test_parse_args_validation():
    with pytest.raises(SystemExit):
        y2y.parse_args(["--min-score", "2", "--yandex-playlist", "1"])
    with pytest.raises(SystemExit):
        y2y.parse_args(["--yandex-playlist", "1", "--config", "x.toml"])
    with pytest.raises(SystemExit):
        y2y.parse_args(["--only", "x"])
    with pytest.raises(SystemExit):
        y2y.parse_args(["--reuse-cache", "--refresh-cache", "--yandex-playlist", "1"])


def test_config_out_paths_and_only(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sync.toml"
    config.write_text(
        """
[defaults]
out = "base-runs"
delay = 0
privacy = "UNLISTED"

[[sync]]
name = "First"
source = "https://music.yandex.ru/users/u/playlists/1"
target = "PL1"

[[sync]]
name = "Second"
source = "2"
out = "custom/second"
""",
        encoding="utf-8",
    )
    captured = []

    def fake_run_transfer(ns):
        captured.append(ns)
        return 0, {"ok": True, "name": ns.name}

    monkeypatch.setattr(y2y, "run_transfer", fake_run_transfer)
    args = y2y.parse_args(
        ["--config", str(config), "--dry-run", "--json-summary", str(tmp_path / "summary.json")]
    )
    code, summaries = y2y.run_config(args)
    assert code == 0
    assert len(summaries) == 2
    assert captured[0].out == str(Path("base-runs") / "First")
    assert captured[1].out == "custom/second"
    assert captured[0].privacy == "UNLISTED"
    assert captured[0].playlist_id == "PL1"
    assert captured[0].update is True
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 2

    captured.clear()
    args = y2y.parse_args(["--config", str(config), "--only", "Second", "--dry-run"])
    code, summaries = y2y.run_config(args)
    assert code == 0
    assert [ns.name for ns in captured] == ["Second"]


def test_config_missing_source(tmp_path, monkeypatch):
    config = tmp_path / "sync.toml"
    config.write_text('[[sync]]\nname = "Broken"\n', encoding="utf-8")
    monkeypatch.setattr(y2y, "run_transfer", lambda ns: (0, {}))
    args = y2y.parse_args(["--config", str(config), "--dry-run"])
    code, summaries = y2y.run_config(args)
    assert code == 1
    assert "error" in summaries[0]


def test_load_config_missing(tmp_path):
    with pytest.raises(RuntimeError):
        y2y.load_config(str(tmp_path / "nope.toml"))


def test_build_summary_counts():
    matched = y2y.TrackMatch(make_track(track_id="1"), y2y.STATUS_MATCHED, candidate())
    skipped = y2y.TrackMatch(make_track(track_id="2"), y2y.STATUS_SKIPPED)
    summary = y2y.build_summary(
        source=None,
        source_title="T",
        matches=[matched, skipped],
        started_at="now",
        out_dir=Path("out"),
        dry_run=True,
        interrupted=False,
        target_id=None,
        added=[],
        failed=[],
        removed=[],
    )
    assert summary["counts"][y2y.STATUS_MATCHED] == 1
    assert summary["counts"][y2y.STATUS_SKIPPED] == 1
    assert summary["ok"] is True
