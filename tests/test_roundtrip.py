"""
Cross-format round-trip regression tests ("the defense line").

For every writable format pair (A, B) we convert a scenario file out of A
into B and back (A -> B -> A), plus a self round-trip (A -> A), and compare
the semantic content before and after. What exactly is compared, which
formats are covered and why some known differences are whitelisted instead
of fixed is documented in ``tests/ROUNDTRIP.md`` (the rubric). Every
whitelist entry carries its justification there; the tests fail both on an
unexpected difference and on a whitelist entry that no longer occurs.

MicroDVD is intentionally out of scope (excluded by the task). SAMI and
WhisperJAX are read-only (they define no writer), so they cannot take part
in a write-read round trip and are excluded by construction.
"""

import itertools
import warnings

import pytest

from pysubs2 import SSAEvent, SSAFile, SSAStyle
from pysubs2.formats.substation import parse_tags

#: Formats that can both be written and read (MicroDVD excluded per task;
#: sami/whisper_jax have no writer).
WRITABLE_FORMATS = ["srt", "ass", "ssa", "vtt", "ttml", "mpl2", "tmp", "json"]

#: Millisecond quantum each format's writer applies to timestamps (existing
#: conventions, not to be changed - see ROUNDTRIP.md, rule R1).
TIME_QUANTUM_MS = {
    "srt": 1,
    "vtt": 1,
    "ttml": 1,
    "json": 1,
    "ass": 10,   # centisecond rounding, pinned by test_substation.py::test_centisecond_rounding
    "ssa": 10,   # same SubStation writer
    "mpl2": 100,  # decisecond timestamps
    "tmp": 1000,  # whole-second timestamps
}

#: Formats whose written file carries no end timestamp (reader estimates it;
#: pinned by test_tmp.py::test_simple_read). End axis is skipped for these.
FORMATS_WITHOUT_END_TIME = {"tmp"}

#: Formats whose write+read cycle preserves inline bold/italic/underline/
#: strikeout. The others lose styling by documented design - see ROUNDTRIP.md,
#: rule R4 (ttml reader, mpl2 writer, tmp reader as pinned by
#: test_tmp.py::test_styled_read).
STYLE_PRESERVING_FORMATS = {"srt", "vtt", "ass", "ssa", "json"}

#: Whitelisted known differences: (scenario, transit format, axis, event index)
#: -> justification. Each entry must occur at least once in the matrix or the
#: suite fails (test_whitelist_entries_are_observed). See ROUNDTRIP.md, W1.
WHITELIST = {
    ("inline_styles", "ttml", "text", 2): (
        "Adjacent styled <span> elements get a whitespace-only tail from "
        "ET.indent() pretty-printing, read back as an extra space "
        "('both  only italic'). The indented TTML output is pinned by "
        "tests/formats/test_ttml.py::test_serialize, so this is whitelisted "
        "per rubric rule R6 (conflict with an existing test)."
    ),
}


def build_scenarios() -> dict[str, SSAFile]:
    """Scenario set - see ROUNDTRIP.md, section "Scenario set"."""
    scenarios = {}

    subs = SSAFile()
    subs.append(SSAEvent(start=1000, end=2500, text="Hello, world."))
    subs.append(SSAEvent(start=61234, end=65432, text="Second line of dialogue"))
    subs.append(SSAEvent(start=3600000, end=3602500, text="One hour in"))
    scenarios["basic"] = subs

    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text=r"{\i1}italic{\i0} and {\b1}bold{\b0}"))
    subs.append(SSAEvent(start=1000, end=2000, text=r"{\u1}under{\u0} {\s1}strike{\s0}"))
    subs.append(SSAEvent(start=2000, end=3000, text=r"{\i1\b1}both{\b0} only italic{\i0}"))
    scenarios["inline_styles"] = subs

    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text=r"first line\Nsecond line"))
    subs.append(SSAEvent(start=1000, end=2000, text=r"hard\hspace here"))
    scenarios["multiline"] = subs

    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="Unicode: 你好 éè \U0001f600"))
    subs.append(SSAEvent(start=1000, end=2000, text="5 < 6 & 7 > 2, with comma"))
    scenarios["special_chars"] = subs

    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="line one\nline two"))
    subs.append(SSAEvent(start=1000, end=2000, text="next"))
    scenarios["raw_newline"] = subs

    subs = SSAFile()
    subs.append(SSAEvent(start=1234, end=5678, text="odd ms"))
    subs.append(SSAEvent(start=5999, end=6001, text="tight"))
    scenarios["timing_edge"] = subs

    subs = SSAFile()
    subs.styles["Italic"] = SSAStyle(italic=True, bold=True)
    subs.append(SSAEvent(start=0, end=1000, text="styled line", style="Italic"))
    subs.append(SSAEvent(start=1000, end=2000, text="default line"))
    scenarios["styled_lines"] = subs

    return scenarios


SCENARIOS = build_scenarios()


def roundtrip(subs: SSAFile, format_: str) -> SSAFile:
    """Write ``subs`` to ``format_`` and read it back (in-memory)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return SSAFile.from_string(subs.to_string(format_), format_)


def _fragment_plaintext(fragment: str) -> str:
    """Plaintext of one parse_tags fragment (canonical text form, rubric R3)."""
    return (fragment
            .replace("\\h", " ")
            .replace("\\N", "\n").replace("\\n", "\n")
            .replace("\r\n", "\n").replace("\r", "\n"))


def _style_runs(subs: SSAFile, event: SSAEvent) -> list[tuple[str, bool, bool, bool, bool]]:
    """Effective (text, bold, italic, underline, strikeout) runs of an event.

    Adjacent runs with identical flags are merged so that superficial tag
    placement differences (eg. an unclosed ``{\\u1}``) do not register.
    """
    style = subs.styles.get(event.style, SSAStyle.DEFAULT_STYLE)
    runs = []
    for fragment, sty in parse_tags(event.text, style, subs.styles):
        text = _fragment_plaintext(fragment)
        if not text:
            continue
        flags = (sty.bold, sty.italic, sty.underline, sty.strikeout)
        if runs and runs[-1][1:] == flags:
            runs[-1] = (runs[-1][0] + text, *flags)
        else:
            runs.append((text, *flags))
    return runs


class SemanticDiff:
    """One semantic difference between two SSAFiles (see ROUNDTRIP.md, axes)."""

    def __init__(self, axis: str, event_index: int | None, detail: str) -> None:
        self.axis = axis
        self.event_index = event_index
        self.detail = detail

    def __repr__(self) -> str:
        return f"<{self.axis} ev{self.event_index}: {self.detail}>"


def _visible_events(subs: SSAFile) -> list[SSAEvent]:
    # cue order is not a compared axis (rubric R5): the WebVTT writer sorts
    # cues by start time, pinned by test_vtt.py::test_writes_in_time_order
    return sorted(subs.get_text_events(), key=lambda e: (e.start, e.end, e.plaintext))


def compare_semantics(before: SSAFile, after: SSAFile, transit: str) -> list[SemanticDiff]:
    """Compare two SSAFiles on the rubric axes, given the transit format."""
    diffs: list[SemanticDiff] = []
    tolerance = TIME_QUANTUM_MS[transit]
    events_before = _visible_events(before)
    events_after = _visible_events(after)

    if len(events_before) != len(events_after):
        diffs.append(SemanticDiff(
            "count", None, f"{len(events_before)} -> {len(events_after)} visible events"))
        return diffs  # per-event axes are meaningless after a count change

    for i, (ev_before, ev_after) in enumerate(zip(events_before, events_after)):
        if abs(ev_before.start - ev_after.start) > tolerance:
            diffs.append(SemanticDiff(
                "start", i, f"{ev_before.start} -> {ev_after.start} (tolerance {tolerance}ms)"))
        if transit not in FORMATS_WITHOUT_END_TIME:
            if abs(ev_before.end - ev_after.end) > tolerance:
                diffs.append(SemanticDiff(
                    "end", i, f"{ev_before.end} -> {ev_after.end} (tolerance {tolerance}ms)"))
        if ev_before.plaintext != ev_after.plaintext:
            diffs.append(SemanticDiff(
                "text", i, f"{ev_before.plaintext!r} -> {ev_after.plaintext!r}"))
        if transit in STYLE_PRESERVING_FORMATS:
            runs_before = _style_runs(before, ev_before)
            runs_after = _style_runs(after, ev_after)
            if runs_before != runs_after:
                diffs.append(SemanticDiff(
                    "style", i, f"{runs_before} -> {runs_after}"))

    return diffs


def _is_whitelisted(scenario: str, transit: str, diff: SemanticDiff) -> bool:
    return (scenario, transit, diff.axis, diff.event_index) in WHITELIST


def _check_diffs(scenario: str, transit: str, diffs: list[SemanticDiff],
                 observed_whitelist: set) -> None:
    unexpected = []
    for diff in diffs:
        if _is_whitelisted(scenario, transit, diff):
            observed_whitelist.add((scenario, transit, diff.axis, diff.event_index))
        else:
            unexpected.append(diff)
    assert not unexpected, (
        f"unexpected semantic differences via {transit!r}: " + "; ".join(map(repr, unexpected)))


# Tracks which whitelist entries were actually hit (module scope, filled while
# the parametrized tests run, asserted by test_whitelist_entries_are_observed).
_OBSERVED_WHitelist: set = set()


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("format_", WRITABLE_FORMATS)
def test_self_roundtrip(scenario: str, format_: str) -> None:
    """A -> A: writing and re-reading one format must be semantically stable."""
    subs = SCENARIOS[scenario]
    back = roundtrip(subs, format_)
    diffs = compare_semantics(subs, back, transit=format_)
    _check_diffs(scenario, format_, diffs, _OBSERVED_WHitelist)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("format_a,format_b", itertools.permutations(WRITABLE_FORMATS, 2))
def test_pair_roundtrip(scenario: str, format_a: str, format_b: str) -> None:
    """A -> B -> A: transiting through B must not lose semantics B can hold.

    The comparison is between the file read back from A (stage 1) and the
    file returned via B (stage 2), so format A's own asymmetries (covered by
    test_self_roundtrip) do not leak into the pair verdicts.
    """
    subs = SCENARIOS[scenario]
    stage1 = roundtrip(subs, format_a)
    stage2 = roundtrip(stage1, format_b)
    diffs = compare_semantics(stage1, stage2, transit=format_b)
    _check_diffs(scenario, format_b, diffs, _OBSERVED_WHitelist)


def test_whitelist_entries_are_observed() -> None:
    """Every whitelist entry must correspond to a live difference.

    A whitelist entry whose underlying behavior changed (eg. the defect was
    fixed) is stale and must be removed - this keeps the whitelist honest.
    """
    stale = set(WHITELIST) - _OBSERVED_WHitelist
    assert not stale, f"stale whitelist entries (no longer observed): {stale}"


def test_ssa_family_preserves_non_dialogue_events() -> None:
    """Comments and drawings survive a self round-trip in ass/ssa/json."""
    for format_ in ("ass", "ssa", "json"):
        subs = SSAFile()
        subs.append(SSAEvent(start=0, end=1000, text="spoken line"))
        subs.append(SSAEvent(start=0, end=1000, text="a comment", type="Comment"))
        subs.append(SSAEvent(start=1000, end=2000, text=r"{\p1}m 0 0 l 100 0 100 100{\p0}"))
        back = roundtrip(subs, format_)
        before = [(ev.type, ev.start, ev.end, ev.text) for ev in subs]
        after = [(ev.type, ev.start, ev.end, ev.text) for ev in back]
        assert before == after, f"{format_} self round-trip changed non-dialogue events"


def test_non_ssa_writers_emit_only_visible_events() -> None:
    """Documented behavior: non-SSA writers skip comments and drawings.

    All non-SSA writers iterate ``subs.get_text_events()``; locking this in
    also documents why the matrix compares visible events only (rubric R2).
    """
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="spoken line"))
    subs.append(SSAEvent(start=0, end=1000, text="a comment", type="Comment"))
    subs.append(SSAEvent(start=1000, end=2000, text=r"{\p1}m 0 0 l 100 0 100 100{\p0}"))
    for format_ in ("srt", "vtt", "ttml", "mpl2", "tmp"):
        back = roundtrip(subs, format_)
        assert len(back) == 1, f"{format_} round-trip should keep only the visible event"
        assert back[0].plaintext == "spoken line"
