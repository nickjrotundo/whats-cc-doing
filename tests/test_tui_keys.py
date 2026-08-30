"""decode_keys: the pure byte-stream -> keys decoder behind the TUI.

Regression suite for the live-test arrow bug: the original reader pulled
single chars through buffered sys.stdin while select() watched the raw
fd, so an arrow key's escape sequence split apart and its tail bytes
leaked as literal characters - LEFT (ESC [ D) leaked a 'D' that opened
the days prompt, and UP/DOWN decoded as lone ESC (nothing).
"""

from __future__ import annotations

from whats_cc_doing.tui import decode_keys


def keys(buf: bytes, flush: bool = False) -> list[str]:
    got, _rest = decode_keys(buf, flush=flush)
    return got


def test_arrow_keys_decode_whole_sequences():
    assert keys(b"\x1b[A") == ["UP"]
    assert keys(b"\x1b[B") == ["DOWN"]
    assert keys(b"\x1b[C") == ["RIGHT"]
    assert keys(b"\x1b[D") == ["LEFT"]


def test_left_arrow_never_leaks_a_literal_d():
    # The exact live-test failure: LEFT must not produce 'D' (or 'd').
    got = keys(b"\x1b[D")
    assert "D" not in got and "d" not in got and got == ["LEFT"]


def test_application_mode_ss3_arrows():
    assert keys(b"\x1bOA") == ["UP"]
    assert keys(b"\x1bOB") == ["DOWN"]


def test_plain_chars_enter_backspace():
    assert keys(b"d") == ["d"]
    assert keys(b"jkq") == ["j", "k", "q"]
    assert keys(b"\r") == ["ENTER"]
    assert keys(b"\n") == ["ENTER"]
    assert keys(b"\x7f") == ["BACKSPACE"]


def test_burst_of_mixed_input_stays_in_order():
    assert keys(b"\x1b[Bj\x1b[A\rq") == ["DOWN", "j", "UP", "ENTER", "q"]


def test_incomplete_sequence_held_as_remainder_not_leaked():
    got, rest = decode_keys(b"\x1b[")
    assert got == [] and rest == b"\x1b["
    got, rest = decode_keys(b"\x1b[" + b"A")
    assert got == ["UP"] and rest == b""
    # lone ESC: held until the caller decides (flush) that nothing follows
    got, rest = decode_keys(b"\x1b")
    assert got == [] and rest == b"\x1b"
    assert keys(b"\x1b", flush=True) == ["ESC"]


def test_flush_drops_incomplete_csi_instead_of_leaking():
    assert keys(b"\x1b[", flush=True) == []
    assert keys(b"\x1b[1;5", flush=True) == []


def test_unknown_complete_sequences_swallowed_whole():
    # F5 (CSI 15~), PgUp (CSI 5~), a ctrl-arrow (CSI 1;5C -> RIGHT is fine
    # to surface or swallow; final byte C maps to RIGHT here by design)
    assert keys(b"\x1b[15~") == []
    assert keys(b"\x1b[5~q") == ["q"]


def test_esc_then_ordinary_char_is_esc_plus_char():
    assert keys(b"\x1bq") == ["ESC", "q"]


def test_utf8_and_control_chars():
    assert keys("é".encode()) == ["é"]
    assert keys(b"\x01\x02x") == ["x"]  # stray control bytes swallowed
    got, rest = decode_keys("é".encode()[:1])  # truncated multibyte held
    assert got == [] and rest == "é".encode()[:1]
