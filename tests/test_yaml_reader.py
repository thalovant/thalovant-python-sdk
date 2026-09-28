"""The config file reader without PyYAML, compared with PyYAML where it matters."""

from __future__ import annotations

import datetime
import math
import sys

import pytest

from thalovant import _yaml
from thalovant._yaml import YAMLError, _Reader

SAMPLES = [
    "",
    "# only a comment\n",
    "key: value\n",
    "a: 1\nb: -2\nc: 0x1F\nd: 0o17\ne: 017\nf: 0b101\ng: 1_000\n",
    "t: [yes, no, on, off, true, False, ~, null, Null]\n",
    "floats: [1.5, -2.25, 1e+3, .5, .inf, -.Inf, 6.02e+23]\n",
    'quoted: "a \\"b\\" \\u00e9 \\n"\nsingle: \'it\'\'s\'\n',
    "nested:\n  inner:\n    deep: 3\n  list:\n    - a\n    - b\n",
    "list:\n- one\n- two:\n    x: 1\n- - nested\n  - again\n",
    "flow: {a: 1, b: [x, y], c: {d: e}}\n",
    "profiles:\n  default:\n    access_key: abc\n    password: 'p: w'\n    site_id: kitchen\n"
    "    default_master: wss://hub.example.com\n    default_port: 443\n",
    "stamp: 2026-09-27\nwhen: 2026-09-27 10:11:12\nzoned: 2026-09-27T10:11:12.5+02:00\nutc: 2026-09-27T10:11:12Z\n",
    "url: https://hub.example.com:443/path?x=1\nhash: a#b\ncomment: x # dropped\n",
    "empty:\nafter: 1\n",
    "---\nkey: document\n",
    "- top\n- level\n",
    "just a scalar\n",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_the_reader_agrees_with_pyyaml(text):
    yaml = pytest.importorskip("yaml")
    expected = yaml.safe_load(text)
    actual = _Reader(text).document()
    if isinstance(expected, dict) and any(isinstance(v, float) and math.isnan(v) for v in expected.values()):
        pytest.skip("NaN is never equal")
    assert actual == expected


@pytest.mark.parametrize("text", [
    "a: &anchor 1\nb: *anchor\n",
    "a: !!str 1\n",
    "a: |\n  block\n",
    "a: >\n  folded\n",
    "---\na: 1\n---\nb: 2\n",
    "a: 1\na: 2\n",
    "a:\n\t- tab\n",
    "a: b: c\n",
    "a: [1, 2\n",
    'a: "unterminated\n',
    'a: "\\q"\n',
    "a:\n  b: 1\n   c: 2\n",
])
def test_what_the_reader_will_not_guess_at_is_refused(text):
    with pytest.raises(YAMLError):
        _Reader(text).document()


def test_a_refusal_names_the_extra():
    with pytest.raises(YAMLError, match=r"thalovant\[yaml\]"):
        _Reader("a: &x 1\n").document()


def test_timestamps_and_special_floats():
    value = _Reader("d: 2026-09-27\nt: 2026-09-27T01:02:03.25-01:30\nn: .nan\n").document()
    assert value["d"] == datetime.date(2026, 9, 27)
    assert value["t"].utcoffset() == datetime.timedelta(hours=-1, minutes=-30)
    assert value["t"].microsecond == 250000
    assert math.isnan(value["n"])


def test_safe_load_uses_pyyaml_when_it_is_there(monkeypatch):
    pytest.importorskip("yaml")
    assert _yaml.safe_load("a: [1, 2]\n") == {"a": [1, 2]}
    with pytest.raises(YAMLError):
        _yaml.safe_load("a: [1, 2\n")


def test_safe_load_without_pyyaml(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert _yaml.safe_load("a: 1\n") == {"a": 1}
    with pytest.raises(YAMLError, match=r"thalovant\[yaml\]"):
        _yaml.safe_load("a: !!int 1\n")


def test_the_config_loader_reads_without_pyyaml(tmp_path, monkeypatch):
    from thalovant import ThalovantIdentity, ThalovantIdentityError

    monkeypatch.setitem(sys.modules, "yaml", None)
    config = tmp_path / "config.yaml"
    config.write_text(
        "access_key: abc\npassword: secret\nsite_id: kitchen\n"
        "default_master: https://hub.example.com\ndefault_port: 443\n"
    )
    config.chmod(0o600)
    identity = ThalovantIdentity.from_config(config)
    assert identity.site_id == "kitchen" and identity.default_port == 443
    config.write_text("access_key: &a abc\n")
    with pytest.raises(ThalovantIdentityError, match=r"thalovant\[yaml\]"):
        ThalovantIdentity.from_config(config)
