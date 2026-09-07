"""Tests for the shared parsing helpers in module/common/misc.py."""

from module.common.misc import get_string_or_none

# A Dell structured `location` blob as check_redfish can hand it back: not a plain string but a
# nested Oem dict. str()-ing it produces ~200 chars of "{'Oem': {'Dell': {'@odata.type': ...}}}".
DELL_LOCATION = {
    "Oem": {
        "Dell": {
            "@odata.type": "#DellLocation.v1_2_0.DellLocation",
            "Locator": "BP_PSV 0:1",
        }
    }
}


def test_get_string_or_none_rejects_structured_values():
    """A nested dict/list is not a meaningful name and must not be stringified into one."""

    assert get_string_or_none(DELL_LOCATION) is None
    assert get_string_or_none(["a", "b"]) is None
    assert get_string_or_none(("a",)) is None
    assert get_string_or_none({1, 2}) is None


def test_get_string_or_none_keeps_scalar_behavior():
    """Scalars are unchanged. Many callers pass ints (core counts, slot numbers, port counts)
    and rely on them stringifying."""

    assert get_string_or_none("  Slot 5 ") == "Slot 5"
    assert get_string_or_none(5) == "5"
    assert get_string_or_none(0) == "0"
    assert get_string_or_none(None) is None
    assert get_string_or_none("") is None
    assert get_string_or_none("   ") is None
