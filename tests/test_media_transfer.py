"""Moving finished downloads between different mounts (NAS shares)."""

import errno
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.plugins.media.downloads import Downloads  # noqa: E402

real_replace = os.replace


def cross_device(src, dst):
    if src.endswith(".part"):  # the final rename inside the destination share works
        return real_replace(src, dst)
    raise OSError(errno.EXDEV, "Invalid cross-device link")


def test_move_across_shares_without_stat_permission():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "dl", "Show.S01E01.mkv")
        src.parent.mkdir()
        src.write_bytes(b"x" * 1000)
        dst = Path(d, "lib", "Show", "Season 1", "Show - S01E01.mkv")
        denied = OSError(errno.EPERM, "Operation not permitted")
        with mock.patch("os.replace", cross_device), \
                mock.patch.object(shutil, "copystat", side_effect=denied):
            Downloads._transfer([(str(src), str(dst))], move=True)
        assert dst.read_bytes() == b"x" * 1000 and not src.exists()
        assert not Path(str(dst) + ".part").exists()
        # retried import: source already gone, destination in place -> fine
        Downloads._transfer([(str(src), str(dst))], move=True)


def test_failed_copy_leaves_no_partial_file():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "a.mkv")
        src.write_bytes(b"x" * 10)
        dst = Path(d, "lib", "a.mkv")
        with mock.patch("os.replace", cross_device), \
                mock.patch.object(shutil, "copyfile", side_effect=OSError(errno.ENOSPC, "full")):
            try:
                Downloads._transfer([(str(src), str(dst))], move=True)
                raise AssertionError("should fail")
            except OSError:
                pass
        assert src.exists() and not dst.exists() and not Path(str(dst) + ".part").exists()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
