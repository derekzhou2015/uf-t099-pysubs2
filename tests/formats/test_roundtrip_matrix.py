"""
Cross-format roundtrip semantics matrix for ASS, SRT and WebVTT.

Why this module exists
----------------------
The library converts every supported format into one in-memory model (SubStation
based) and writes it back out.  Conversion is therefore always a *roundtrip*:
``src -> intermediate -> src``.  Until now the test suite only checked individual
readers and writers against fixed strings, so nothing verified that a semantic
value survives such a trip.  This module is that safety net.

What is compared
----------------
For every fixture, source format and intermediate format (3 x 3 x N
combinations) the following five dimensions are compared between the file as
*understood before* the trip and the file as *understood after* it:

``count``  number of subtitle events
``time``   start/end timestamps
``style``  style name, event metadata (type/layer/name/margins/effect) and, for
           SubStation, the whole style library
``text``   the text content, compared character by character with the emphasis
           attributes (bold/italic/underline/strikeout) attached to each
           character; for SubStation the raw text including override tags is
           compared exactly
``order``  the sequence of events

Verdict rules
-------------
The oracle is *not* "the two files must be identical".  It is "the two files must
be semantically identical **after subtracting what the intermediate format cannot
express**".  Anything the intermediate format has no representation for (a style
library, a comment event, a blank line inside a cue...) is a *capability loss*:
it is recorded as a ``Loss`` with a capability id, and every capability id is
declared in :data:`CAPABILITIES` together with a justification and a basis in the
repository (spec, existing test, release note).

Anything else - a value that *could* have been carried but came back
deformed, dropped or misplaced - is a *defect* and fails the test.

Known, deliberate conventions are part of the projection, not of the defect
list: SubStation's centisecond rounding (``test_centisecond_rounding``),
timestamp clamping at the format limits (``test_overflow_timestamp_write`` /
``test_underflow_timestamp_write``) and the WebVTT writer's cue sorting
(``test_writes_in_time_order``) are locked by existing tests and are kept
as-is.

Defects found and fixed while building the matrix
-------------------------------------------------
Two writer asymmetries were uncovered here and are fixed in the library:

* ``SubripFormat.to_file()`` never wrote ``<b>`` even though its reader parses
  it, so bold was silently lost through SRT/WebVTT.
* ``SubripFormat.to_file()`` emitted an empty ``<i></i>`` for every empty text
  fragment (which override sequences produce), so each save grew the file and
  re-parsing invented styling.  Empty fragments are now skipped.
* ``SubstationFormat._field_to_string()`` wrote a raw newline in ``text``
  verbatim, which truncated the subtitle on the next read; it now writes ``\\N``.

How to extend the matrix
------------------------
Add a ``Case`` to :data:`CASES`.  ``build`` returns a fresh :class:`SSAFile`;
``allowed_losses`` lists the capability ids the fixture is expected to trigger.
The case is then automatically parametrized over all source/intermediate pairs.
If a new fixture exposes a capability that is not declared yet, add it to
:data:`CAPABILITIES` - the test suite refuses to use an undeclared id.
"""
from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from pysubs2 import Alignment, Color, SSAEvent, SSAFile, SSAStyle, make_time
from pysubs2.formats.subrip import MAX_REPRESENTABLE_TIME as SRT_MAX_TIME
from pysubs2.formats.substation import MAX_REPRESENTABLE_TIME as ASS_MAX_TIME
from pysubs2.formats.substation import is_valid_field_content, parse_tags
from pysubs2.formats.webvtt import WebVTTFormat
from pysubs2.time import TIMESTAMP
from pysubs2.warnings import (
    SubtitleAttributeWarning,
    TimestampOverflow,
    TimestampUnderflow,
)

#: Formats covered by the matrix.  MicroDVD is out of scope for this acceptance.
FORMATS: tuple[str, ...] = ("ass", "srt", "vtt")

#: Formats whose payload is plain text plus inline markup.
TEXT_FORMATS = frozenset({"srt", "vtt"})

#: Emphasis attributes that SRT and WebVTT can carry inline.
EMPHASIS_FIELDS: tuple[str, ...] = ("bold", "italic", "underline", "strikeout")

#: Largest timestamp representable in each format (clamping is an existing convention).
MAX_REPRESENTABLE_TIME: dict[str, int] = {"ass": ASS_MAX_TIME, "srt": SRT_MAX_TIME, "vtt": SRT_MAX_TIME}

#: Timestamp pattern used by each reader (VTT has its own, narrower one).
TIMESTAMP_PATTERN: dict[str, re.Pattern[str]] = {
    "ass": TIMESTAMP, "srt": TIMESTAMP, "vtt": WebVTTFormat.TIMESTAMP,
}

#: Matches anything the SubRip/WebVTT readers would treat as inline markup.
MARKUP_PATTERN = re.compile(r"< */? *[a-zA-Z][^>]*>")


# ---------------------------------------------------------------------------
# Capability whitelist: what each intermediate format provably cannot express
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """A deliberate, documented information loss of some intermediate format."""

    id: str
    formats: frozenset[str]
    summary: str
    basis: str


CAPABILITIES: dict[str, Capability] = {cap.id: cap for cap in (
    Capability(
        id="non-text-events",
        formats=frozenset({"srt", "vtt"}),
        summary="Comment events and SSA drawings have no representation in a time-based "
                "text format; the writer omits them, so the event count drops.",
        basis="SSAFile.get_text_events() (pysubs2/ssafile.py) and "
              "tests/formats/test_subrip.py::test_write_drawing, which locks the drop "
              "of drawing events; comments are dropped by the same code path.",
    ),
    Capability(
        id="style-library",
        formats=frozenset({"srt", "vtt"}),
        summary="SRT/WebVTT carry no style definitions.  Style names fall back to "
                "'Default' and the style library is not preserved.  Emphasis that the "
                "style carried (bold/italic/underline/strikeout) is inlined as markup "
                "by the writer and therefore still survives the trip.",
        basis="docs/supported-formats.rst: SRT 'uses HTML tags for formatting' and VTT "
              "'Currently implemented as a flavour of SubRip, with no extra support for "
              "WebVTT-specific features like styles or subtitle alignment'.",
    ),
    Capability(
        id="event-metadata",
        formats=frozenset({"srt", "vtt"}),
        summary="Layer, actor name, margins and effect are SubStation-only event fields; "
                "the readers always produce the neutral value.",
        basis="Security of the SRT/WebVTT cue grammar itself (only timing line plus "
              "payload); the writers emit no such fields by construction.",
    ),
    Capability(
        id="hard-space",
        formats=frozenset({"srt", "vtt"}),
        summary="A non-breaking space (\\h) is written as an ordinary space.  Neither "
                "format can mark a non-breaking space, and the shared writer does not "
                "emit WebVTT entities.",
        basis="SubripFormat.to_file() replaces \\h with a plain space; character "
              "escaping is not implemented in the shared SRT/WebVTT writer.",
    ),
    Capability(
        id="blank-lines",
        formats=frozenset({"srt", "vtt"}),
        summary="An empty line ends a cue in both formats, so a blank line inside a cue "
                "payload (\\N\\N) is collapsed and leading/trailing line breaks are "
                "trimmed.",
        basis="SubRip cue grammar (blank line terminates a cue); the writer enforces it "
              "with re.sub('\\n+', '\\n', ...) and .strip() in SubripFormat.to_file().",
    ),
    Capability(
        id="edge-whitespace",
        formats=frozenset({"srt", "vtt"}),
        summary="Leading/trailing whitespace of a cue payload is not significant in "
                "either format and is trimmed on write (and again on read).",
        basis="SubripFormat.to_file() strips the payload; SubripFormat.from_file() "
              "strips it again, so the value cannot survive even in principle.",
    ),
    Capability(
        id="payload-markup",
        formats=frozenset({"srt", "vtt"}),
        summary="Literal angle-bracket text such as '<i>...' is indistinguishable from "
                "inline markup in the output, so it is re-read as styling (or stripped). "
                "Neither format has an escape mechanism here.",
        basis="SubripFormat.from_file() rewrites <i>/<b>/<u>/<s> and strips remaining "
              "tags; WebVTT entity escaping is not implemented.",
    ),
    Capability(
        id="payload-timing-line",
        formats=frozenset({"srt", "vtt"}),
        summary="A payload line that itself contains a timestamp pair is read back as a "
                "cue separator, which splits the cue.  WebVTT explicitly forbids '-->' "
                "in a cue payload and SRT has no escaping either.",
        basis="WebVTT specification, cue payload grammar; the readers treat any line "
              "with two timestamps as a timing line (SubripFormat.from_file()).",
    ),
    Capability(
        id="time-quantization",
        formats=frozenset({"ass"}),
        summary="SubStation timestamps have centisecond precision, so millisecond values "
                "are rounded to the nearest 10 ms (round-half-up, as Aegisub does).",
        basis="tests/formats/test_substation.py::test_centisecond_rounding and "
              "::test_ass_ms_to_timestamp; release notes 1.6.0.",
    ),
    Capability(
        id="time-range",
        formats=frozenset({"ass", "srt", "vtt"}),
        summary="Timestamps outside the format's representable range are clamped (to 0 "
                "for negative values, to the format maximum for overflow).",
        basis="tests/formats/test_substation.py::test_overflow_timestamp_write, "
              "::test_underflow_timestamp_write and the equivalent tests in "
              "tests/formats/test_subrip.py; release notes 1.6.0.",
    ),
    Capability(
        id="event-order",
        formats=frozenset({"vtt"}),
        summary="The WebVTT writer emits cues sorted by start time; the input order is "
                "not preserved.",
        basis="tests/formats/test_vtt.py::test_writes_in_time_order; release notes 1.6.1.",
    ),
    Capability(
        id="field-commas",
        formats=frozenset({"ass"}),
        summary="SubStation stores Style/Name/Effect as comma-separated fields, and the "
                "reader follows that grammar.  A comma inside such a value shifts the "
                "remaining fields of that line, so the value cannot survive.  The "
                "library offers no escaping for this.",
        basis="is_valid_field_content() in pysubs2/formats/substation.py documents that "
              "commas are not acceptable in a SubStation field; the reader splits each "
              "line on commas.",
    ),
)}

#: Capabilities that no fixture is allowed to trigger silently.
RESERVED_BASES = "CAPABILITIES"


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Loss:
    """One documented capability loss observed during a projected roundtrip."""

    capability: str
    detail: str

    def __str__(self) -> str:
        return f"{self.capability}: {self.detail}"


@dataclass(frozen=True)
class Mismatch:
    """A semantic difference that the intermediate format *could* have carried."""

    dimension: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.dimension}] {self.detail}"


class LossLog:
    """Collects capability losses for one projected roundtrip."""

    def __init__(self) -> None:
        self.items: list[Loss] = []

    def add(self, capability: str, detail: str) -> None:
        if capability not in CAPABILITIES:
            raise AssertionError(f"Undeclared capability {capability!r} - add it to CAPABILITIES")
        self.items.append(Loss(capability, detail))

    def ids(self) -> set[str]:
        return {loss.capability for loss in self.items}

    def __str__(self) -> str:
        return "\n".join(str(loss) for loss in self.items)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def iter_payload_chars(fragment: str) -> Iterator[tuple[str, bool]]:
    """Split a text fragment into (character, is_hard_space) pairs.

    ``\\N``/``\\n`` become a real newline, ``\\h`` becomes a flagged space.
    A lone backslash is kept literally.
    """
    i = 0
    while i < len(fragment):
        pair = fragment[i:i + 2]
        if pair in (r"\N", r"\n"):
            yield "\n", False
            i += 2
        elif pair == r"\h":
            yield " ", True
            i += 2
        else:
            yield fragment[i], False
            i += 1


def emphasis_of(sty: SSAStyle) -> frozenset[str]:
    return frozenset(field for field in EMPHASIS_FIELDS if getattr(sty, field))


def extract_chars(ev: SSAEvent, styles: dict[str, SSAStyle]) -> tuple[tuple[str, frozenset[str]], ...]:
    """Canonical (character, emphasis) sequence of an event's text, tags removed."""
    base = styles.get(ev.style, SSAStyle.DEFAULT_STYLE)
    out: list[tuple[str, frozenset[str]]] = []
    for fragment, sty in parse_tags(ev.text, base, styles):
        attrs = emphasis_of(sty)
        out.extend((char, attrs) for char, _ in iter_payload_chars(fragment))
    return tuple(out)


@dataclass(frozen=True)
class ProjectedChar:
    """One payload character with its emphasis and whether markup wraps it."""

    char: str
    attrs: frozenset[str]
    wrapped: bool


def project_chars(ev: SSAEvent, styles: dict[str, SSAStyle]) -> list[ProjectedChar]:
    """Characters of the event as the text-format writer would see them.

    ``wrapped`` mirrors the writer, which wraps each non-empty fragment in
    ``<b>/<i>/<u>/<s>`` whenever that fragment carries emphasis.  The wrapping
    matters for whitespace: ``.strip()`` runs on the serialized string, so
    whitespace at the very edge of the payload is only reachable when no markup
    precedes it.
    """
    base = styles.get(ev.style, SSAStyle.DEFAULT_STYLE)
    out: list[ProjectedChar] = []
    for fragment, sty in parse_tags(ev.text, base, styles):
        if not fragment:
            # SubripFormat.to_file() skips empty fragments (they carry no text).
            continue
        attrs = emphasis_of(sty)
        wrapped = bool(attrs)
        out.extend(ProjectedChar(char, attrs, wrapped) for char, _ in iter_payload_chars(fragment))
    return out


def render_chars(chars: tuple[tuple[str, frozenset[str]], ...] | list[tuple[str, frozenset[str]]]) -> str:
    return "".join("\n" if char == "\n" else char for char, _ in chars)


def has_timing_line(plaintext: str, fmt: str) -> bool:
    pattern = TIMESTAMP_PATTERN[fmt]
    return any(len(pattern.findall(line)) >= 2 for line in plaintext.split("\n"))


def project_time(ms: int, fmt: str, losses: LossLog, subject: str) -> int:
    limit = MAX_REPRESENTABLE_TIME[fmt]
    if ms < 0:
        losses.add("time-range", f"{subject} clamped {ms} -> 0")
        return 0
    if ms > limit:
        losses.add("time-range", f"{subject} clamped {ms} -> {limit}")
        ms = limit
    if fmt == "ass":
        # Mirrors SubstationFormat.ms_to_timestamp(): round half up to centiseconds.
        rounded = ((ms + 5) - (ms + 5) % 10)
        if rounded != ms:
            losses.add("time-quantization", f"{subject} rounded {ms} -> {rounded}")
        return rounded
    return ms


def project_text(
    ev: SSAEvent,
    styles: dict[str, SSAStyle],
    fmt: str,
    losses: LossLog,
    subject: str,
) -> tuple[tuple[str, frozenset[str]], ...] | None:
    """Semantic payload the intermediate format can carry, or None if inexpressible."""
    plaintext = ev.plaintext
    if MARKUP_PATTERN.search(plaintext):
        losses.add("payload-markup", f"{subject}: literal markup in {plaintext!r} is re-read as styling")
        return None

    projected = project_chars(ev, styles)
    hard_space = any(
        is_hard_space
        for fragment, _ in parse_tags(ev.text, styles.get(ev.style, SSAStyle.DEFAULT_STYLE), styles)
        for _, is_hard_space in iter_payload_chars(fragment)
    )
    if hard_space:
        losses.add("hard-space", f"{subject}: non-breaking space written as an ordinary space")

    collapsed: list[ProjectedChar] = []
    for pc in projected:
        if pc.char == "\n" and collapsed and collapsed[-1].char == "\n":
            continue
        collapsed.append(pc)
    if len(collapsed) != len(projected):
        losses.add("blank-lines", f"{subject}: empty line inside the cue payload is collapsed")

    # The writer strips the serialized line and the reader collates lines; markup
    # around a fragment means the outer whitespace is inside a tag and survives.
    start, end = 0, len(collapsed)
    while start < end and collapsed[start].char in " \t\n" and not collapsed[start].wrapped:
        start += 1
    while end > start and collapsed[end - 1].char in " \t\n" and not collapsed[end - 1].wrapped:
        end -= 1
    if (start, end) != (0, len(collapsed)):
        trimmed = [pc.char for pc in collapsed[:start] + collapsed[end:]]
        capability = "blank-lines" if "\n" in trimmed else "edge-whitespace"
        losses.add(capability, f"{subject}: leading/trailing whitespace is trimmed")
    return tuple((pc.char, pc.attrs) for pc in collapsed[start:end])


# ---------------------------------------------------------------------------
# The oracle: project one roundtrip and compare the result
# ---------------------------------------------------------------------------

def load(fmt: str, subs: SSAFile) -> SSAFile:
    """Write ``subs`` as ``fmt`` and read it back."""
    return SSAFile.from_string(subs.to_string(fmt), fmt)


def compare_roundtrip(origin: SSAFile, back: SSAFile, src: str, mid: str) -> tuple[list[Mismatch], LossLog]:
    losses = LossLog()
    mismatches: list[Mismatch] = []

    # -- count -------------------------------------------------------------
    expressible: list[SSAEvent] = []
    for index, ev in enumerate(origin):
        if mid in TEXT_FORMATS and not ev.is_text:
            kind = "comment" if ev.is_comment else "drawing"
            losses.add("non-text-events", f"event[{index}] ({kind}) not representable in {mid}")
            continue
        if mid in TEXT_FORMATS and has_timing_line(ev.plaintext, mid):
            # The payload is structurally unsafe: the reader sees an extra timing
            # line and splits the cue, so neither the event count nor the payload
            # mapping is well defined.  The outcome is pinned separately in
            # test_timing_line_in_payload_is_not_roundtrip_safe().
            losses.add("payload-timing-line",
                       f"event[{index}] payload {ev.plaintext!r} contains a timing line")
            return mismatches, losses
        expressible.append(ev)

    if len(expressible) != len(back):
        mismatches.append(Mismatch(
            "count", f"{len(expressible)} expressible event(s) -> {len(back)} after {src}->{mid}->{src}"))

    # -- order -------------------------------------------------------------
    ordered = expressible
    if mid == "vtt":
        ordered = sorted(expressible, key=lambda ev: ev.start)
        if [ev.start for ev in ordered] != [ev.start for ev in expressible]:
            losses.add("event-order", "WebVTT writer sorts cues by start time")

    # -- per-event dimensions ---------------------------------------------
    for index, (expected, actual) in enumerate(zip(ordered, back.events)):
        subject = f"event[{index}]"

        exp_start = project_time(expected.start, mid, losses, f"{subject}.start")
        exp_end = project_time(expected.end, mid, losses, f"{subject}.end")
        if (exp_start, exp_end) != (actual.start, actual.end):
            mismatches.append(Mismatch(
                "time", f"{subject} {exp_start},{exp_end} -> {actual.start},{actual.end}"))

        if mid == "ass":
            if expected.style != actual.style:
                mismatches.append(Mismatch("style", f"{subject}.style {expected.style!r} -> {actual.style!r}"))
            if expected.type != actual.type:
                mismatches.append(Mismatch("style", f"{subject}.type {expected.type!r} -> {actual.type!r}"))
            for field in ("layer", "name", "marginl", "marginr", "marginv", "effect"):
                exp_value, act_value = getattr(expected, field), getattr(actual, field)
                if exp_value != act_value:
                    mismatches.append(Mismatch("style", f"{subject}.{field} {exp_value!r} -> {act_value!r}"))
        else:
            # SRT/WebVTT keep the cue's style only as inlined emphasis (checked in
            # the text dimension below); the name itself and the style library
            # cannot be represented.
            if expected.style != "Default":
                losses.add("style-library",
                           f"{subject} uses style {expected.style!r}; {mid} has no style library, "
                           f"falls back to 'Default'")
            if actual.style != "Default":
                mismatches.append(Mismatch("style", f"{subject}.style {expected.style!r} -> {actual.style!r}"))
            if expected.type != "Dialogue":
                mismatches.append(Mismatch("style", f"{subject}.type {expected.type!r} -> {actual.type!r}"))
            for field in ("layer", "name", "marginl", "marginr", "marginv", "effect"):
                neutral = 0 if field.startswith("margin") or field == "layer" else ""
                exp_value, act_value = getattr(expected, field), getattr(actual, field)
                if exp_value != neutral:
                    losses.add("event-metadata", f"{subject}.{field} {exp_value!r} has no {mid} representation")
                if act_value != neutral:
                    mismatches.append(Mismatch("style", f"{subject}.{field} -> {act_value!r}"))

        # -- text ----------------------------------------------------------
        # SubStation stores the payload verbatim, override tags included, so an
        # ASS -> ASS trip must be byte-identical.  As soon as a text format is
        # involved on either leg, the comparison unit becomes plain characters
        # plus inline emphasis, because that is all the text format can carry;
        # tag order on the way back is the writer's normal form, not a semantic.
        if src == "ass" and mid == "ass":
            if expected.text != actual.text:
                mismatches.append(Mismatch("text", f"{subject}.text {expected.text!r} -> {actual.text!r}"))
        else:
            if expected.type != "Dialogue":
                continue
            expected_chars = project_text(expected, origin.styles, mid, losses, f"{subject}.text")
            if expected_chars is None:
                continue
            actual_chars = extract_chars(actual, back.styles)
            if expected_chars != actual_chars:
                mismatches.append(Mismatch(
                    "text",
                    f"{subject}.text {render_chars(expected_chars)!r} -> {render_chars(actual_chars)!r}"))

    # -- style library -----------------------------------------------------
    if mid == "ass":
        for name in sorted(set(origin.styles) | set(back.styles)):
            if name not in origin.styles:
                mismatches.append(Mismatch("style", f"styles[{name!r}] appeared out of nowhere"))
            elif name not in back.styles:
                mismatches.append(Mismatch("style", f"styles[{name!r}] lost"))
            else:
                expected_fields = origin.styles[name].as_dict()
                actual_fields = back.styles[name].as_dict()
                for field, value in expected_fields.items():
                    if actual_fields[field] != value:
                        mismatches.append(Mismatch(
                            "style", f"styles[{name!r}].{field} {value!r} -> {actual_fields[field]!r}"))
    elif set(origin.styles) - {"Default"}:
        losses.add("style-library",
                   "the style library itself (including unused styles) is not representable in " + mid)

    return mismatches, losses


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    """One semantic scenario, parametrized over every source/intermediate pair."""

    name: str
    build: Callable[[], SSAFile]
    allowed_losses: frozenset[str]
    rationale: str = ""


def _plain() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1234, text="Hello, world!"))
    subs.append(SSAEvent(start=make_time(s=2, ms=5), end=make_time(s=3, ms=678), text="Second line\\Nwith break"))
    return subs


def _inline_emphasis() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text=r"{\b1}bold{\b0} plain"))
    subs.append(SSAEvent(start=1000, end=2000, text=r"{\i1}it{\i0} {\u1}un{\u0} {\s1}st{\s0}"))
    subs.append(SSAEvent(start=2000, end=3000, text=r"{\i1}both {\b1}and{\b0}{\i0}"))
    return subs


def _style_library() -> SSAFile:
    subs = SSAFile()
    subs.styles["Fancy"] = SSAStyle(
        fontname="Times New Roman", bold=True, italic=True, underline=True, strikeout=True,
        primarycolor=Color(1, 2, 3, 4), outlinecolor=Color(9, 8, 7), backcolor=Color(5, 6, 7),
        fontsize=33.5, scalex=110.0, scaley=90.0, spacing=1.5, angle=12.0, borderstyle=3,
        outline=4.5, shadow=1.25, alignment=Alignment.TOP_LEFT, marginl=1, marginr=2,
        marginv=3, encoding=128,
    )
    subs.styles["Unused"] = SSAStyle(fontsize=48.0)
    subs.append(SSAEvent(start=0, end=1000, text="styled", style="Fancy"))
    subs.append(SSAEvent(start=1000, end=2000, text="plain", style="Default"))
    return subs


def _style_emphasis() -> SSAFile:
    subs = SSAFile()
    subs.styles["Shout"] = SSAStyle(bold=True, italic=True)
    subs.styles["Whisper"] = SSAStyle(italic=True)
    subs.append(SSAEvent(start=0, end=1000, text="loud", style="Shout"))
    subs.append(SSAEvent(start=1000, end=2000, text="quiet", style="Whisper"))
    subs.append(SSAEvent(start=2000, end=3000, text="normal", style="Default"))
    return subs


def _event_metadata() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="meta", layer=3, name="Actor", marginl=11,
                         marginr=22, marginv=33, effect="scroll up"))
    subs.append(SSAEvent(start=1000, end=2000, text="plain"))
    return subs


def _comments_and_drawings() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="visible"))
    subs.append(SSAEvent(start=0, end=1000, text="hidden comment", type="Comment"))
    subs.append(SSAEvent(start=1000, end=2000, text=r"{\p1}m 0 0 l 100 0 100 100{\p0}"))
    subs.append(SSAEvent(start=2000, end=3000, text="visible again"))
    return subs


def _time_precision() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=4, end=5, text="centisecond boundary"))
    subs.append(SSAEvent(start=1004, end=2345, text="arbitrary milliseconds"))
    subs.append(SSAEvent(start=make_time(h=1, m=1, ms=4), end=make_time(h=1, m=1, ms=5), text="rounds up"))
    return subs


def _whitespace() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="  lead and trail  "))
    subs.append(SSAEvent(start=1000, end=2000, text="double  inner  spaces"))
    subs.append(SSAEvent(start=2000, end=3000, text="hard\\hspace"))
    return subs


def _blank_lines() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text=""))
    subs.append(SSAEvent(start=1000, end=2000, text="a\\N\\Nb"))
    subs.append(SSAEvent(start=2000, end=3000, text="\\Nleading"))
    subs.append(SSAEvent(start=3000, end=4000, text="trailing\\N"))
    return subs


def _ass_tags() -> SSAFile:
    subs = SSAFile()
    subs.styles["Fancy"] = SSAStyle(italic=True)
    subs.append(SSAEvent(start=0, end=1000, text=r"{\an7}positioned {\k100}karaoke"))
    subs.append(SSAEvent(start=1000, end=2000, text=r"a{\r}b"))
    subs.append(SSAEvent(start=2000, end=3000, text=r"{\rFancy}x", style="Fancy"))
    return subs


def _unicode_text() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="Příliš žluťoučký kůň úpěl ďábelské ódy"))
    subs.append(SSAEvent(start=1000, end=2000, text="日本語の字幕 🎬 — test"))
    return subs


def _numeric_lines() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="Invented Town\\N1963"))
    subs.append(SSAEvent(start=1000, end=2000, text="42"))
    subs.append(SSAEvent(start=2000, end=3000, text="1"))
    return subs


def _unordered() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=5000, end=6000, text="later"))
    subs.append(SSAEvent(start=0, end=1000, text="earlier"))
    subs.append(SSAEvent(start=1000, end=2000, text="middle"))
    subs.append(SSAEvent(start=2000, end=3000, text="tie a"))
    subs.append(SSAEvent(start=2000, end=2500, text="tie b"))
    return subs


def _markup_literal() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="<i>tag-like</i> text"))
    subs.append(SSAEvent(start=1000, end=2000, text="5 < 6 > 4"))
    return subs


def _timing_line_payload() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="00:00:01,000 --> 00:00:02,000"))
    subs.append(SSAEvent(start=1000, end=2000, text="00:00:03.000 --> 00:00:04.000"))
    return subs


def _out_of_range() -> SSAFile:
    subs = SSAFile()
    subs.append(SSAEvent(start=-1000, end=1000, text="negative start"))
    # Legal in SRT/WebVTT (which allow up to 99:59:59,999) but beyond the
    # SubStation maximum of 9:59:59.99.
    subs.append(SSAEvent(start=make_time(h=20), end=make_time(h=20, s=1), text="beyond ASS range"))
    return subs


def _newlines_in_text() -> SSAFile:
    subs = SSAFile()
    # The plaintext setter is the documented way to assign multi-line text; a raw
    # newline in ``text`` must not truncate the line on SubStation output.
    ev = SSAEvent(start=0, end=1000)
    ev.plaintext = "line one\nline two"
    subs.append(ev)
    subs.append(SSAEvent(start=1000, end=2000, text="single line"))
    return subs


def _empty_file() -> SSAFile:
    return SSAFile()


CASES: tuple[Case, ...] = (
    Case("plain", _plain, frozenset({"time-quantization"}),
         "Timestamp values must survive intact except for SubStation centisecond rounding."),
    Case("inline_emphasis", _inline_emphasis, frozenset({"time-quantization"}),
         "Inline bold/italic/underline/strikeout must survive through SRT and WebVTT markup."),
    Case("style_library", _style_library, frozenset({"time-quantization", "style-library"}),
         "Full style library survives ASS only; text formats must at least inline the emphasis."),
    Case("style_emphasis", _style_emphasis, frozenset({"style-library"}),
         "Emphasis carried by a named style must be inlined for text formats, not lost."),
    Case("event_metadata", _event_metadata, frozenset({"time-quantization", "event-metadata"}),
         "Layer/name/margins/effect are SubStation-only."),
    Case("comments_and_drawings", _comments_and_drawings, frozenset({"non-text-events"}),
         "Comment events and drawings are not text output."),
    Case("time_precision", _time_precision, frozenset({"time-quantization"}),
         "The documented centisecond rounding is a convention, not a defect."),
    Case("whitespace", _whitespace, frozenset({"time-quantization", "edge-whitespace", "hard-space"}),
         "Inner whitespace must survive; edge whitespace and non-breaking spaces are not representable."),
    Case("blank_lines", _blank_lines, frozenset({"blank-lines"}),
         "Empty cue payloads must survive; blank lines inside a payload cannot."),
    Case("ass_tags", _ass_tags, frozenset({"style-library"}),
         "Positioning/karaoke tags are unrepresentable elsewhere; text and emphasis must survive."),
    Case("unicode_text", _unicode_text, frozenset({"time-quantization"}),
         "Non-ASCII text must survive byte-for-byte."),
    Case("numeric_lines", _numeric_lines, frozenset({"time-quantization"}),
         "Lines that look like cue indices must not be eaten."),
    Case("unordered", _unordered, frozenset({"time-quantization", "event-order"}),
         "Order is preserved by ASS and SRT; WebVTT sorts by design."),
    Case("markup_literal", _markup_literal, frozenset({"payload-markup"}),
         "Literal angle-bracket text is ambiguous in SRT/WebVTT payloads."),
    Case("timing_line_payload", _timing_line_payload, frozenset({"payload-timing-line"}),
         "A cue whose text looks like a timing line cannot be represented at all."),
    Case("out_of_range", _out_of_range, frozenset({"time-range"}),
         "Negative timestamps are clamped to zero by every writer (existing convention)."),
    Case("newlines_in_text", _newlines_in_text, frozenset({"time-quantization"}),
         "A raw newline in the text must be written as \\N, not as a literal line break."),
    Case("empty_file", _empty_file, frozenset(),
         "Nothing in, nothing out."),
)

CASES_BY_NAME = {case.name: case for case in CASES}


@contextmanager
def allowed_clamping(case: Case) -> Iterator[None]:
    """Silence the clamping warnings a case deliberately triggers.

    The clamping behaviour itself is pinned by the existing writer tests; here it
    is expected noise from the ``time-range`` projection.
    """
    if "time-range" in case.allowed_losses:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TimestampOverflow)
            warnings.simplefilter("ignore", TimestampUnderflow)
            yield
    else:
        yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mid", FORMATS, ids=lambda f: f"via-{f}")
@pytest.mark.parametrize("src", FORMATS, ids=lambda f: f"from-{f}")
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_roundtrip_preserves_semantics(case: Case, src: str, mid: str) -> None:
    """src -> mid -> src must preserve every dimension the intermediate can express."""
    with allowed_clamping(case):
        origin = load(src, case.build())
        back = load(src, load(mid, origin))

    mismatches, losses = compare_roundtrip(origin, back, src, mid)

    unexpected = losses.ids() - case.allowed_losses
    assert not unexpected, (
        f"{case.name} {src}->{mid}->{src} hit undocumented capability loss(es) "
        f"{sorted(unexpected)}:\n{losses}")
    assert not mismatches, (
        f"{case.name} {src}->{mid}->{src} changed semantics:\n"
        + "\n".join(str(m) for m in mismatches)
        + (f"\n(documentary losses: {sorted(losses.ids())})" if losses.items else ""))


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_single_format_roundtrip_is_stable(case: Case, fmt: str) -> None:
    """Writing and reading the same format twice must reach a fixed point.

    This is the property that catches writers which emit constructs their own
    reader turns into something else (for example empty emphasis tags that grow
    on every save).
    """
    with allowed_clamping(case):
        once = load(fmt, case.build())
        twice = load(fmt, once)
        thrice = load(fmt, twice)

    assert twice.equals(thrice), (
        f"{case.name}: {fmt} output is not stable under repeated read/write:\n"
        f"  pass 2: {[(e.start, e.end, e.text) for e in twice]}\n"
        f"  pass 3: {[(e.start, e.end, e.text) for e in thrice]}")


def test_capability_ids_are_declared_and_used() -> None:
    """Every capability must be documented and reachable, and vice versa."""
    declared = set(CAPABILITIES)
    used_by_cases = set().union(*(case.allowed_losses for case in CASES))
    assert used_by_cases <= declared, f"cases reference undeclared capabilities: {used_by_cases - declared}"

    for capability in CAPABILITIES.values():
        assert capability.summary.strip(), f"{capability.id} has no summary"
        assert capability.basis.strip(), f"{capability.id} has no basis"
        assert capability.formats <= set(FORMATS), f"{capability.id} mentions an out-of-scope format"

    # Exercise the whole matrix and make sure no declaration is dead weight.
    fired: set[str] = set()
    for case in CASES:
        for src in FORMATS:
            for mid in FORMATS:
                with allowed_clamping(case):
                    origin = load(src, case.build())
                    _, losses = compare_roundtrip(origin, load(src, load(mid, origin)), src, mid)
                fired |= losses.ids()

    # Capabilities that are not reachable from the five compared dimensions have
    # a dedicated pinned test; count them as exercised when that behaviour holds.
    fired |= set(EXTRA_CAPABILITY_PROBES)

    assert fired == declared, (
        f"capabilities never observed: {sorted(declared - fired)}; "
        f"observed but undeclared: {sorted(fired - declared)}")


def test_ass_field_commas_are_not_representable() -> None:
    """Documented limitation: a comma inside a SubStation field shifts the line.

    SubStation writes Style/Name/Effect as comma-separated fields, so the reader
    cannot tell a delimiter from a comma inside the value.  The parser keeps going
    (fields are zipped positionally), which is why this is a recorded capability
    loss rather than a crash.
    """
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="meta", name="Last, First",
                         effect="a,b", marginl=1, marginr=2, marginv=3))

    with pytest.warns(SubtitleAttributeWarning):
        back = load("ass", subs)

    # Every field after the first comma shifts by one; the trailing "meta" text
    # is pulled into the Text field.
    assert back[0].name == "Last"
    assert back[0].marginr == 1
    assert back[0].marginv == 2
    assert back[0].effect == "3"
    assert back[0].text == "a,b,meta"
    assert not is_valid_field_content(subs[0].name)
    assert not is_valid_field_content(subs[0].effect)


def test_ass_writer_does_not_truncate_raw_newlines() -> None:
    """A raw newline in ``text`` must be written as \\N, not as a line break.

    SubStation stores one event per line; a literal newline makes the reader drop
    everything after the first line, so the subtitle would silently lose half of
    its text.  ``plaintext`` is the documented way to set multi-line text.
    """
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="line one\nline two"))
    subs.append(SSAEvent(start=1000, end=2000, text="next"))

    written = subs.to_string("ass")
    dialogue_lines = [line for line in written.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) == 2, written
    assert dialogue_lines[0].endswith(r"line one\Nline two")

    back = SSAFile.from_string(written, "ass")
    assert [ev.plaintext for ev in back] == ["line one\nline two", "next"]


#: Capabilities pinned by a dedicated test rather than by the five matrix dimensions.
EXTRA_CAPABILITY_PROBES = frozenset({"field-commas"})


def test_positional_and_karaoke_tags_are_dropped_by_text_formats() -> None:
    """Documented behaviour: SRT/WebVTT keep the words but lose the positioning.

    The matrix does not compare tag syntax for text formats (it cannot: the
    payload grammar has no equivalent), so this pins the actual outcome.
    """
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text=r"{\an7}positioned {\k100}karaoke"))
    assert load("srt", subs)[0].text == "positioned karaoke"
    assert load("vtt", subs)[0].text == "positioned karaoke"


def test_timing_line_in_payload_is_not_roundtrip_safe() -> None:
    """Documented limitation: cue text that looks like a timing line splits the cue.

    Neither format offers escaping here (WebVTT forbids '-->' inside a payload),
    so the damage is recorded rather than fixed.  The comma decimal separator is
    SubRip's; WebVTT requires a dot, and a comma-style payload is therefore inert
    there (also verified below).
    """
    for fmt, payload in (("srt", "00:00:01,000 --> 00:00:02,000"),
                         ("vtt", "00:00:01.000 --> 00:00:02.000")):
        subs = SSAFile()
        subs.append(SSAEvent(start=0, end=1000, text=payload))
        subs.append(SSAEvent(start=1000, end=2000, text="normal"))

        written = subs.to_string(fmt)
        reparsed = SSAFile.from_string(written, fmt)
        assert len(reparsed) > len(subs), f"{fmt} unexpectedly kept the payload intact"
        assert reparsed[0].text == "" and reparsed[1].text == "", (
            f"{fmt} timing-line payload was not the documented failure mode: "
            f"{[ev.text for ev in reparsed]}")
        assert reparsed[-1].text == "normal"

    # Comma decimals are not WebVTT timestamps, so such a payload passes through.
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="00:00:01,000 --> 00:00:02,000"))
    assert load("vtt", subs)[0].text == "00:00:01,000 --> 00:00:02,000"


def test_markup_in_payload_is_reinterpreted_as_styling() -> None:
    """Documented limitation: '<i>text</i>' inside ASS text becomes emphasis in SRT/WebVTT."""
    subs = SSAFile()
    subs.append(SSAEvent(start=0, end=1000, text="<i>tag-like</i> text"))
    assert load("srt", subs)[0].text == r"{\i1}tag-like{\i0} text"
    assert load("vtt", subs)[0].text == r"{\i1}tag-like{\i0} text"


def test_matrix_shape_is_intact() -> None:
    """Guard against silently shrinking the matrix."""
    assert FORMATS == ("ass", "srt", "vtt")
    assert len(CASES) >= 17
    assert all(isinstance(case.build(), SSAFile) for case in CASES)
    assert len({case.name for case in CASES}) == len(CASES)


def test_loss_log_rejects_undeclared_capability() -> None:
    """The whitelist must be the only way to record a loss."""
    log = LossLog()
    with pytest.raises(AssertionError, match="Undeclared capability"):
        log.add("made-up-loss", "should not be recordable")

    log.add("time-quantization", "declared, fine")
    assert log.ids() == {"time-quantization"}
