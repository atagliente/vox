"""Property-based tests for the splitter that separates thinking from answer.

The hand-written cases in test_reasoning.py cover the shapes someone thought
of. These cover the ones nobody did: a tag arriving one character at a time,
a buffer that ends mid-marker, text that merely looks like the start of a tag.
That is the whole difficulty of the module, and it is exactly what a stream of
tokens does to it in practice.
"""

from __future__ import annotations

from itertools import pairwise

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from vox_chat.reasoning import CLOSE_TAG, OPEN_TAG, ThinkSplitter

# Fragments deliberately biased towards the hard cases: prefixes of the tags,
# lone angle brackets, the tags themselves. Random letters would almost never
# produce a split marker.
FRAGMENTS = st.sampled_from(
    [
        OPEN_TAG,
        CLOSE_TAG,
        "<",
        ">",
        "<thi",
        "nk>",
        "</thin",
        "k>",
        "<think",
        "think>",
        "a",
        "hello ",
        "",
        "<<",
        "</",
    ]
)

DOCUMENTS = st.lists(FRAGMENTS, max_size=12).map("".join)


def split_at(text: str, cuts: list[int]) -> list[str]:
    """Cut ``text`` at the given offsets, in order."""
    points = sorted({0, *(c % (len(text) + 1) for c in cuts), len(text)})
    return [text[a:b] for a, b in pairwise(points) if a != b]


def run(chunks: list[str]) -> list[tuple[str, str]]:
    """Feed the chunks through a splitter and collect every segment."""
    splitter = ThinkSplitter()
    out: list[tuple[str, str]] = []
    for chunk in chunks:
        out.extend(splitter.feed(chunk))
    out.extend(splitter.flush())
    return out


def merged(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Join neighbouring segments of the same kind.

    Where a segment boundary falls is an artefact of chunking; which kind the
    text was assigned to is not. Only the second is a promise.
    """
    out: list[tuple[str, str]] = []
    for kind, text in segments:
        if not text:
            continue
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))
    return out


@given(document=DOCUMENTS, cuts=st.lists(st.integers(min_value=0), max_size=8))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_where_the_chunks_fall_does_not_change_the_split(
    document: str, cuts: list[int]
) -> None:
    """The stream is cut wherever the provider felt like cutting it. A tag
    broken across two chunks must come out the same as one that is not."""
    whole = merged(run([document]))
    piecemeal = merged(run(split_at(document, cuts)))
    assert whole == piecemeal


@given(document=DOCUMENTS, cuts=st.lists(st.integers(min_value=0), max_size=8))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_one_character_at_a_time_is_the_same_as_all_at_once(
    document: str, cuts: list[int]
) -> None:
    """The worst case a provider can hand us, and the one that broke it
    before: a token per character."""
    assert merged(run(list(document))) == merged(run([document]))


@given(document=DOCUMENTS, cuts=st.lists(st.integers(min_value=0), max_size=8))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_nothing_is_dropped_and_nothing_is_invented(
    document: str, cuts: list[int]
) -> None:
    """Every character of the input comes out exactly once. Only the tags the
    splitter actually acted on are consumed; everything else is text."""
    segments = run(split_at(document, cuts))
    emitted = "".join(text for _kind, text in segments)
    assert emitted == strip_matched(document)


def strip_matched(document: str) -> str:
    """The document with exactly the tags the splitter would act on removed.

    It only ever looks for the one tag it is waiting for, so a second
    ``<think>`` inside a thought, or a ``</think>`` part-way through an
    answer, is ordinary text. This mirrors that rule rather than removing
    every tag — including the one exception, a stream that opens on a closing
    tag, where the tag is swallowed because a provider put the model inside a
    thought it never announced.
    """
    out: list[str] = []
    rest, thinking = document, False
    leading = rest.lstrip()
    if leading.startswith(CLOSE_TAG):
        gap = len(rest) - len(leading)
        out.append(rest[:gap])
        rest = leading[len(CLOSE_TAG) :]
    while rest:
        marker = CLOSE_TAG if thinking else OPEN_TAG
        index = rest.find(marker)
        if index < 0:
            out.append(rest)
            break
        out.append(rest[:index])
        rest = rest[index + len(marker) :]
        thinking = not thinking
    return "".join(out)


@given(document=DOCUMENTS, cuts=st.lists(st.integers(min_value=0), max_size=8))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_awaited_tag_never_reaches_the_transcript(
    document: str, cuts: list[int]
) -> None:
    """A leaked ``<think>`` in the answer is the visible symptom of every bug
    this module has had. An answer segment must not contain an opening tag,
    and a reasoning segment must not contain a closing one — those are the
    tags the splitter was watching for and would have acted on."""
    for kind, text in run(split_at(document, cuts)):
        assert (CLOSE_TAG if kind == "reasoning" else OPEN_TAG) not in text


def test_an_unmatched_tag_is_ordinary_text() -> None:
    """Pinning the rule the properties above lean on, because it is a choice
    and not an accident: the splitter looks for one tag at a time.

    A ``</think>`` in the middle of an answer stays in the answer, and a
    second ``<think>`` inside a thought stays in the reasoning."""
    assert run(["answer </think> more"]) == [("text", "answer </think> more")]
    assert run(["<think>", "<think>", "</think>"]) == [("reasoning", "<think>")]


def test_a_stream_that_opens_on_a_closing_tag_loses_the_tag() -> None:
    """The one asymmetry, and the reason for it.

    Several servers start the model already inside a thought and send only
    the close, so the answer began with a literal ``</think>``. There is no
    reading of that under which printing the tag is right, so it is
    swallowed and the answer carries on — including when it arrives split
    across chunks, or behind the whitespace a provider puts in front of it.
    """
    assert run(["</think>", "Hello"]) == [("text", "Hello")]
    assert run(["</th", "ink>", "Hello"]) == [("text", "Hello")]
    assert run(["  </think> answer"]) == [("text", " answer")]
    assert run(["</think>"]) == []
    # Narrow on purpose: once the answer has started, a model writing about
    # tags is likelier than a provider ending a thought a paragraph late.
    assert run(["a", "</think>"]) == [("text", "a"), ("text", "</think>")]


@given(document=DOCUMENTS)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_kinds_alternate(document: str) -> None:
    """Reasoning and answer take turns: two runs of the same kind next to
    each other would mean a tag was swallowed without switching."""
    kinds = [kind for kind, _text in merged(run(list(document)))]
    assert all(a != b for a, b in pairwise(kinds))


@given(
    before=st.text(alphabet="ab ", max_size=20),
    thought=st.text(alphabet="ab ", max_size=20),
    after=st.text(alphabet="ab ", max_size=20),
    cuts=st.lists(st.integers(min_value=0), max_size=8),
)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_well_formed_thought_is_recovered_whole(
    before: str, thought: str, after: str, cuts: list[int]
) -> None:
    """The ordinary shape, cut in every possible place: what is inside the
    tags is reasoning, what is outside is answer."""
    assume(thought)
    document = f"{before}{OPEN_TAG}{thought}{CLOSE_TAG}{after}"
    segments = merged(run(split_at(document, cuts)))
    assert ("reasoning", thought) in segments
    assert "".join(t for k, t in segments if k == "text") == before + after
