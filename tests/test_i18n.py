"""Translations: merged per-language files, fallback to English, safe formatting."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hub import i18n  # noqa: E402


def test_shipped_files_are_valid_json():
    for f in i18n.ROOT.glob("*/*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()), f


def test_placeholders_match():
    """A translation must use the same {placeholders} as its English source."""
    import re
    ph = lambda s: sorted(re.findall(r"\{(\w+)\}", s))
    for lang in i18n.LANGUAGES:
        for k, v in i18n.load(lang).items():
            assert ph(k) == ph(v), (lang, k, v)


def test_no_conflicting_translations():
    """Files are merged, so one English key must mean the same thing in every file."""
    seen = {}
    for f in sorted(i18n.ROOT.glob("*/*.json")):
        for k, v in json.loads(f.read_text(encoding="utf-8")).items():
            key = (f.parent.name, k)
            assert seen.setdefault(key, (v, f.name))[0] == v, (k, v, seen[key])


def test_lookup_and_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        old = i18n.ROOT
        i18n.ROOT = Path(tmp)
        try:
            (Path(tmp) / "it").mkdir()
            (Path(tmp) / "it" / "a.json").write_text('{"Hello {who}": "Ciao {who}", "Broken {x}": "Rotto {y}"}')
            (Path(tmp) / "it" / "b.json").write_text("not json")
            i18n.set_language("it")
            assert i18n._("Hello {who}", who="Ada") == "Ciao Ada"
            assert i18n._("Missing") == "Missing"
            assert i18n._("Broken {x}", x=1) == "Broken 1"  # bad translation -> English
            i18n.set_language("xx")
            assert i18n.language() == i18n.DEFAULT
            i18n.set_language("en")
            assert i18n._("Hello {who}", who="Ada") == "Hello Ada"
        finally:
            i18n.ROOT = old
            i18n.set_language("it")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
