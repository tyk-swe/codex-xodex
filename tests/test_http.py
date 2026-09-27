import math

import pytest

from xodex.http import strict_json
from xodex.errors import XodexError
from xodex.tools import TOOLS


@pytest.mark.parametrize("body", [
    b'{"id":1,"id":2}',
    b'{"nested":{"key":1,"key":2}}',
    b'{"value":NaN}',
    b'{"value":Infinity}',
    b'{"value":-Infinity}',
    b'{"value":1e400}',
    b'{"value":-1e400}',
    b'{"id":"\\ud800"}',
    b'{"\\udfff":1}',
    b'"\xff"',
])
def test_strict_json_rejects_ambiguous_or_unencodable_values(body):
    with pytest.raises((ValueError, UnicodeError)):
        strict_json(body)


def test_strict_json_preserves_valid_unicode_and_finite_numbers():
    result = strict_json(b'{"text":"\\ud83d\\ude80","value":1.5}')
    assert result == {"text": "\U0001f680", "value": 1.5}
    assert math.isfinite(result["value"])


@pytest.mark.parametrize("args", [{"limit": 1.0}, {"offset": 0.0}, {"limit": True}])
def test_tool_integer_fields_require_python_integers(args):
    with pytest.raises(XodexError) as result:
        TOOLS["list_tasks"].validate(args)
    assert result.value.code == "invalid_arguments"


def test_non_json_arguments_report_a_tool_error():
    with pytest.raises(XodexError) as result:
        TOOLS["list_tasks"].validate({"limit": object()})
    assert result.value.code == "invalid_arguments"


@pytest.mark.parametrize("tool,args", [
    ("exec_command", {"cmd": "true\x00"}),
    ("finish_task", {"title": "Title", "summary": "Summary", "checks": ["true\x00"]}),
])
def test_nul_commands_are_rejected_before_admission(tool, args):
    with pytest.raises(XodexError) as result:
        TOOLS[tool].validate({"task_id": "0" * 36, "request_id": "nul", **args})
    assert result.value.code == "invalid_arguments"
