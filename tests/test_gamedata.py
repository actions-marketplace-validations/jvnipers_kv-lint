import textwrap

import pytest

from kv_lint.config import FileSet
from test_linter import run, write


def gamedata(root, text, style="sourcemod", name="gamedata/x.games.txt", extra=None):
    write(root, {name: textwrap.dedent(text), **(extra or {})})
    return run(root, FileSet(["gamedata/**/*.txt"], "gamedata", {"style": style}))


def found(issues):
    return sorted((i.line, i.severity, i.check) for i in issues)


SM_VALID = r"""
"Games"
{
	"#default"
	{
		"#supported"
		{
			"game"	"cstrike"
			"game"	"csgo"
		}
		"Signatures"
		{
			"Spawn"
			{
				"library"	"server"
				"windows"	"\x55\x8B\xEC\x2A\x2A\x56\x57\x8B"
				"linux"		"@_ZN11CBaseEntity5SpawnEv"
			}
		}
		"Offsets"
		{
			"Teleport"
			{
				"windows"	"108"
				"linux"		"0x6D"
			}
			"m_iHealth"
			{
				"class"		"CBasePlayer"
				"prop"		"m_iHealth"
			}
		}
		"Addresses"
		{
			"g_pGameRules"
			{
				"signature"	"Spawn"
				"windows"
				{
					"read"	"2"
				}
				"read"		"0"
				"offset"	"4"
			}
		}
		"Functions"
		{
			"CBaseEntity::Spawn"
			{
				"signature"	"Spawn"
				"callconv"	"thiscall"
			}
		}
	}
	"#default"
	{
		"Keys"
		{
			"GameRulesProxy"	"CCSGameRulesProxy"
		}
	}
}
"""


def test_sourcemod_valid(tmp_path):
    assert gamedata(tmp_path, SM_VALID) == []


@pytest.mark.parametrize(
    "value, check",
    [
        (r"\x55\x8B\c2A\x56", "invalid-signature"),
        ("55 8B EC 56", "invalid-signature"),
        (r"\x2A\x2A\x2A", "wildcard-signature"),
        (r"\x55\x8B\x2A\x56", "short-signature"),
        (r"\x2A\x55\x8B\xEC\x56\x57\x2A", None),
    ],
)
def test_sourcemod_signature_bytes(tmp_path, value, check):
    text = SM_VALID.replace(r"\x55\x8B\xEC\x2A\x2A\x56\x57\x8B", value)
    assert [i.check for i in gamedata(tmp_path, text)] == ([check] if check else [])


def test_sourcemod_keys_and_offsets(tmp_path):
    text = (
        SM_VALID.replace('"linux"\t\t"0x6D"', '"Linux"\t\t"109"')
        .replace('"windows"\t"108"', '"windows"\t"0108"\n\t\t\t\t"win32"\t"1"')
        .replace('"read"\t\t"0"', '"read"\t\t"zero"')
    )
    assert sorted((i.check, i.message) for i in gamedata(tmp_path, text)) == [
        ("invalid-offset", "Address 'g_pGameRules' (read) isn't a number: 'zero'"),
        # "Linux" is never read, so only windows is left
        ("missing-platform", "Offset 'Teleport' has no linux entry"),
        (
            "octal-offset",
            "Offset 'Teleport' (windows) has a leading zero, which SourceMod's strtol reads as octal (8)",
        ),
        (
            "unknown-gamedata-key",
            "Platform key 'Linux' in offset 'Teleport' is never read, SourceMod compares keys case-sensitively",
        ),
        (
            "unknown-gamedata-key",
            "Unknown key 'win32' in offset 'Teleport' (expected: windows, linux, mac, windows64, linux64, mac64)",
        ),
    ]


def test_sourcemod_references_and_address_rules(tmp_path):
    text = (
        SM_VALID.replace(
            '"signature"\t"Spawn"\n\t\t\t\t"windows"',
            '"signature"\t"Spwan"\n\t\t\t\t"windows"',
        )
        .replace(
            '"read"\t\t"0"\n\t\t\t\t"offset"\t"4"',
            '"offset"\t"4"\n\t\t\t\t"read"\t\t"0"',
        )
        .replace(
            '"signature"\t"Spawn"\n\t\t\t\t"callconv"',
            '"offset"\t"Nope"\n\t\t\t\t"callconv"',
        )
    )
    assert sorted((i.check, i.message) for i in gamedata(tmp_path, text)) == [
        (
            "gamedata-structure",
            "'offset' in address 'g_pGameRules' must be its last read/offset entry",
        ),
        (
            "unknown-reference",
            "'CBaseEntity::Spawn' refers to offset 'Nope', which isn't defined",
        ),
        (
            "unknown-reference",
            "'g_pGameRules' refers to signature 'Spwan', which isn't defined",
        ),
    ]


def test_sourcemod_duplicates_respect_supported_blocks(tmp_path):
    text = """\
    "Games"
    {
    	"#default"
    	{
    		"Keys"
    		{
    			"Sound"	"a.wav"
    		}
    	}
    	"#default"
    	{
    		"#supported"
    		{
    			"engine"	"left4dead"
    		}
    		"Keys"
    		{
    			"Sound"	"b.wav"
    		}
    	}
    	"#default"
    	{
    		"Keys"
    		{
    			"Sound"	"c.wav"
    		}
    	}
    }
    """
    assert found(gamedata(tmp_path, text)) == [(25, "error", "duplicate-entry")]


def test_master_file_lists_existing_files(tmp_path):
    master = '"Game Master"\n{\n\t"x.games.txt"\n\t{\n\t}\n\t"gone.txt"\n\t{\n\t}\n}\n'
    issues = gamedata(tmp_path, SM_VALID, extra={"gamedata/master.games.txt": master})
    assert [(i.file, i.line, i.check) for i in issues] == [
        ("gamedata/master.games.txt", 6, "unknown-reference")
    ]


IDA_VALID = """
"Games"
{
	"csgo"
	{
		"Signatures"
		{
			"SwitchTeam"
			{
				"library"	"server"
				"windows"	"40 53 57 48 81 EC ?? ?? ?? ?? 48 8B D9"
				"linux"		"55 48 89 E5 41 54 ? 89 FC"
				"allow_multi_match"	"1"
			}
		}
		"Offsets"
		{
			"Teleport"
			{
				"windows"	"165"
				"linux"		"164"
			}
		}
		"Patches"
		{
			"NoBlock"
			{
				"windows"	"90 90 EB"
				"linux"		"90 90 EB"
			}
		}
	}
}
"""


def test_ida_valid(tmp_path):
    assert gamedata(tmp_path, IDA_VALID, "ida") == []


@pytest.mark.parametrize(
    "value, check",
    [
        ("40 53  57 48", "invalid-signature"),
        ("40 53 57 48 ", "invalid-signature"),
        ("4053 57 48", "invalid-signature"),
        (r"\x40\x53\x57", "invalid-signature"),
        ("?? ?? ??", "wildcard-signature"),
        ("40 ?? 53 57", "short-signature"),
        ("?? 40 53 57 48 81 ??", None),
    ],
)
def test_ida_signature_bytes(tmp_path, value, check):
    text = IDA_VALID.replace("40 53 57 48 81 EC ?? ?? ?? ?? 48 8B D9", value)
    assert [i.check for i in gamedata(tmp_path, text, "ida")] == (
        [check] if check else []
    )


def test_ida_rules(tmp_path):
    text = (
        IDA_VALID.replace('\t\t\t\t"library"\t"server"\n', "")
        .replace('"linux"\t\t"164"', '"Linux"\t\t"164"')
        .replace('"windows"\t"90 90 EB"', '"windows"\t"90 ?? EB"')
        .replace('\t"csgo"\n', '\t"csgo"\n\t{\n\t}\n\t"csgo"\n')
    )
    assert found(gamedata(tmp_path, text, "ida")) == [
        (7, "error", "duplicate-entry"),
        (11, "error", "missing-library"),
        (23, "warning", "gamedata-key-case"),
        (30, "error", "invalid-signature"),
    ]


def test_missing_platform_is_info(tmp_path):
    text = IDA_VALID.replace('\t\t\t\t"linux"\t\t"164"\n', "")
    assert found(gamedata(tmp_path, text, "ida")) == [(18, "info", "missing-platform")]
