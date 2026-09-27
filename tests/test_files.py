import os
import struct
import threading

import pytest

from xodex.errors import XodexError
from xodex.files import WorkspaceFS, image, read_text, relative, search, sha256


@pytest.mark.parametrize("path", ["", "/etc/passwd", "../secret", "a/../b", "a//b", "a/./b", "a\\b", "a\x00b", "."])
def test_bad_paths(path):
    with pytest.raises(XodexError):
        relative(path)


def test_roundtrip(tmp_path):
    with WorkspaceFS(tmp_path) as fs:
        fs.write("src/a.txt", b"hello\nworld\n", 0o755)
        assert fs.read("src/a.txt") == (b"hello\nworld\n", 0o755)
        text = read_text(fs, "src/a.txt", 2, 1)
        assert text["text"] == "world\n"
        assert text["sha256"] == sha256(b"hello\nworld\n")
        assert fs.listing()["entries"][1]["path"] == "src/a.txt"
        fs.delete("src/a.txt")
        assert not fs.exists("src/a.txt")


def test_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "leaf").symlink_to(outside / "secret")
    with WorkspaceFS(root) as fs:
        for path in ["link/secret", "leaf"]:
            with pytest.raises(XodexError):
                fs.read(path)
            with pytest.raises(XodexError):
                fs.write(path, b"changed")
        assert {entry["type"] for entry in fs.listing()["entries"]} == {"symlink"}
    assert (outside / "secret").read_text() == "secret"


def test_special_files(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with WorkspaceFS(tmp_path) as fs:
        with pytest.raises(XodexError, match="regular"):
            fs.read("pipe")
        with pytest.raises(XodexError):
            fs.write("pipe", b"x")
        with pytest.raises(XodexError):
            fs.delete("pipe")


def test_oversized_binary(tmp_path):
    (tmp_path / "large").write_bytes(b"a" * 20)
    (tmp_path / "binary").write_bytes(b"\xff\x00")
    with WorkspaceFS(tmp_path) as fs:
        with pytest.raises(XodexError):
            fs.read("large", 10)
        with pytest.raises(XodexError):
            read_text(fs, "binary")


def test_search_budget(tmp_path):
    (tmp_path / "a").write_text("abc\nABC\nabc\n")
    (tmp_path / "binary").write_bytes(b"\xff")
    with WorkspaceFS(tmp_path) as fs:
        result = search(fs, "abc", "*", False, 2)
        assert len(result["matches"]) == 2
        assert result["truncated"]
        assert len(search(fs, "ABC", "a", True, 9)["matches"]) == 1


def test_depth_truncation(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/a").write_text("x")
    with WorkspaceFS(tmp_path) as fs:
        assert fs.listing(depth=0)["truncated"]


def test_image_headers(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 10, 20) + b"\x08\x02\0\0\0" + b"\0" * 4
    (tmp_path / "image.png").write_bytes(png)
    with WorkspaceFS(tmp_path) as fs:
        result = image(fs, "image.png")
        assert (result["width"], result["height"], result["mimeType"]) == (10, 20, "image/png")
        (tmp_path / "image.png").write_bytes(png[:16] + struct.pack(">II", 100000, 100000) + png[24:])
        with pytest.raises(XodexError):
            image(fs, "image.png")


def test_symlink_race(tmp_path):
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "data").write_bytes(b"SECRET")
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a/data").write_bytes(b"safe")
    stop = threading.Event()

    def swap():
        while not stop.is_set():
            try:
                (root / "a").rename(root / "held")
                (root / "a").symlink_to(outside, target_is_directory=True)
                (root / "a").unlink()
                (root / "held").rename(root / "a")
            except OSError:
                pass

    worker = threading.Thread(target=swap)
    worker.start()
    try:
        with WorkspaceFS(root) as fs:
            for _ in range(200):
                try:
                    assert fs.read("a/data")[0] == b"safe"
                except XodexError:
                    pass
    finally:
        stop.set()
        worker.join(timeout=2)


def test_listing_non_utf8_filename_is_a_structured_error(tmp_path):
    descriptor = os.open(os.fsencode(tmp_path) + b"/invalid-\xff", os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with WorkspaceFS(tmp_path) as fs:
        with pytest.raises(XodexError) as result:
            fs.listing()
    assert result.value.code == "unsupported_filename"
