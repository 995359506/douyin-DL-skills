from pathlib import Path

import pytest

from skills.douyin_downloader.web import _find_media_candidates, _find_task_video_file


def test_finds_exact_filename_recursively(tmp_path: Path):
    nested = tmp_path / "task-folder"
    nested.mkdir()
    filename = "7680683795409931526_title_video.mp4"
    expected = nested / filename
    expected.write_bytes(b"mock-video")

    assert _find_task_video_file(tmp_path, [filename], ["7680683795409931526"]) == expected.resolve()


def test_falls_back_to_anchored_aweme_id(tmp_path: Path):
    nested = tmp_path / "task-folder"
    nested.mkdir()
    expected = nested / "12345_title_video.mp4"
    expected.write_bytes(b"mock-video")

    assert _find_task_video_file(tmp_path, [], ["12345"]) == expected.resolve()


def test_rejects_ambiguous_exact_filename(tmp_path: Path):
    for folder in (tmp_path / "a", tmp_path / "b"):
        folder.mkdir()
        (folder / "same_video.mp4").write_bytes(b"mock-video")

    with pytest.raises(RuntimeError, match="VIDEO_FILE_AMBIGUOUS"):
        _find_task_video_file(tmp_path, ["same_video.mp4"], [])


def test_does_not_accept_path_outside_download_root(tmp_path: Path):
    outside = tmp_path.parent / "outside_video.mp4"
    outside.write_bytes(b"mock-video")
    with pytest.raises(FileNotFoundError):
        _find_task_video_file(tmp_path, [str(outside)], [])


def test_id_prefix_is_anchored_not_fuzzy(tmp_path: Path):
    (tmp_path / "123456_title_video.mp4").write_bytes(b"mock-video")
    with pytest.raises(FileNotFoundError):
        _find_task_video_file(tmp_path, [], ["12345"])


def test_rejects_empty_temporary_and_symlink_escape_files(tmp_path: Path):
    (tmp_path / "12345_empty.mp4").write_bytes(b"")
    (tmp_path / "12345_video.mp4.part").write_bytes(b"partial")
    (tmp_path / "12345_video.tmp").write_bytes(b"partial")
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"outside-video")
    (tmp_path / "12345_link.mp4").symlink_to(outside)
    with pytest.raises(FileNotFoundError):
        _find_task_video_file(tmp_path, [], ["12345"])


def test_media_lookup_prefers_exact_video_over_aweme_companion_files(tmp_path: Path):
    folder = tmp_path / "title-folder"
    folder.mkdir()
    video = folder / "12345_title_video.mp4"
    video.write_bytes(b"mock-video")
    (folder / "12345_title_music.mp3").write_bytes(b"mock-audio")
    (folder / "12345_title_cover.jpeg").write_bytes(b"mock-cover")
    (folder / "12345_title_desc.txt").write_text("desc", encoding="utf-8")

    matches = _find_media_candidates(tmp_path, video.name, video.stem, "12345")

    assert matches == [video]
