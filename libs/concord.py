"""Scripted answers to the conductor's ``concord`` flow.

Negotiation on the hub is the conductor engine running its ``concord`` flow
inside a task (``fastapi-backend/app/services/protocols.py``). Every turn it
puts to a member opens with a header line naming the flow and the step::

    concord · score · turn 3 of 20 · stub-a

and, for a turn asked a second time, ``concord · score (again) · turn 4 of 20 · stub-a``.
The steps, and the reply each one reads:

``anchor``
    Rate the two reference outcomes on the member's own 0-100 scale, ideal
    above bad: ``bad: 0 - why`` and ``ideal: 100 - why``, one line each.
``propose``
    One option, then a ``why:`` line: ``<the option>\\nwhy: <the reasons>``.
``score`` / ``rescore``
    One line per option, filled in from the template lines the prompt gives:
    ``B (Keep audit logs…): 70 - why``. Every rating needs its reason.
``repair``
    Asked only of the member least happy with the best option: a new option
    that answers the objections, in the ``propose`` shape. A fix that repeats
    an option already on the table is sent back once.

A ``select`` step between them picks the option the least happy member is best
off with. It ends the run ``converged`` when every member places it at the bar
or above (default 60% of the way from their bad to their ideal outcome),
``resolved`` when the fixes run out and the best found stands, and
``rejected`` when nobody proposed anything.

:class:`Persona` answers those steps the same way every time, so a stub agent's
run is reproducible; :func:`summon` builds the ask ``board coordinate`` posts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: The flow the stubs and the canary run.
FLOW = "concord"

#: The steps of ``concord`` that ask a member something.
ASK_STEPS = frozenset({"anchor", "propose", "score", "rescore", "repair"})

#: The bar a ``concord`` run holds every member to when the summon sets none
#: (``protocols.DEFAULT_THRESHOLD`` on the hub), 0-1.
DEFAULT_BAR = 0.6

#: ``concord · score · turn 3 of 20 · stub-a``, or ``… · score (again) · …``.
_HEADER = re.compile(
    r"^\s*(?P<flow>[\w-]+) · (?P<step>[\w-]+)(?P<again> \(again\))?"
    r" · turn (?P<turn>\d+) of (?P<cap>\d+)"
)
#: ``B (Keep audit logs for…): <0-100> - <why>``: one option to rate.
_RATING_TEMPLATE = re.compile(
    r"^[ \t]*(?P<label>[A-Z])(?: \((?P<name>[^)\n]*)\))?: <0-100> - <why>[ \t]*$",
    re.MULTILINE,
)
#: ``B. Keep audit logs for 90 days.``: an option as raters read it.
_OPTION_LINE = re.compile(r"^(?P<label>[A-Z])\. (?P<text>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class StepAsk:
    """Which step of which flow a turn's prompt is."""

    flow: str
    step: str
    again: bool
    turn: int
    cap: int


def parse_step(prompt: str) -> StepAsk | None:
    """The step a conductor turn asks, from its header line; ``None`` for any
    other message (a person's, or a post that asks nobody)."""
    match = _HEADER.match(prompt or "")
    if match is None:
        return None
    return StepAsk(
        flow=match.group("flow"),
        step=match.group("step"),
        again=bool(match.group("again")),
        turn=int(match.group("turn")),
        cap=int(match.group("cap")),
    )


def turn_prompt(turn: dict[str, Any]) -> str:
    """The prompt an ``await --json`` turn leads with.

    One ``await`` hands over every unread message asking for the handle,
    oldest first and the turn last (``asks``); only the last one is the step
    the conductor is waiting on.
    """
    asks = turn.get("asks") or []
    if asks and isinstance(asks[-1], dict) and asks[-1].get("prompt"):
        return str(asks[-1]["prompt"])
    return str(turn.get("prompt") or "")


def rating_targets(prompt: str) -> list[tuple[str, str]]:
    """``[(letter, template line)]`` for each option a rating prompt asks for."""
    return [(m.group("label"), m.group(0).strip()) for m in _RATING_TEMPLATE.finditer(prompt)]


def option_texts(prompt: str) -> dict[str, str]:
    """``{letter: text}`` for the options a prompt lists in full."""
    return {m.group("label"): m.group("text").strip() for m in _OPTION_LINE.finditer(prompt)}


@dataclass(frozen=True)
class Liking:
    """How a persona rates any option whose text contains ``phrase``."""

    phrase: str
    rating: int
    why: str


@dataclass
class Persona:
    """A member that answers every ``concord`` step the same way each run.

    It proposes ``proposal`` (with ``why``), rates an option by the first of
    ``likes`` whose phrase the option's text contains (``otherwise`` when none
    does), and answers each fix it's asked for with the next of ``fixes``, the
    last one again once they run out. ``anchors`` is how it rates the bad and
    the ideal outcome; at ``(0, 100)`` a rating is placed exactly where it
    reads, so 70 is 70% of the way to its ideal.
    """

    proposal: str
    why: str
    likes: tuple[Liking, ...] = ()
    otherwise: tuple[int, str] = (70, "it works for my role")
    fixes: tuple[tuple[str, str], ...] = ()
    anchors: tuple[int, int] = (0, 100)
    #: How many fixes it has been asked for so far in this run.
    fixes_asked: int = field(default=0, compare=False)

    def rate(self, text: str) -> tuple[int, str]:
        """``(0-100, why)`` for an option's text."""
        lowered = text.lower()
        for liking in self.likes:
            if liking.phrase.lower() in lowered:
                return liking.rating, liking.why
        return self.otherwise

    def anchor_reply(self) -> str:
        bad, ideal = self.anchors
        return (
            f"bad: {bad} - nothing is decided and the gap stays open\n"
            f"ideal: {ideal} - my own option, as I proposed it"
        )

    def proposal_reply(self) -> str:
        return f"{self.proposal}\nwhy: {self.why}"

    def ratings_reply(self, prompt: str) -> str:
        texts = option_texts(prompt)
        lines = []
        for label, template in rating_targets(prompt):
            name = re.search(r"\(([^)\n]*)\)", template)
            text = texts.get(label) or (name.group(1) if name else "")
            rating, why = self.rate(text)
            lines.append(template.replace("<0-100>", str(rating)).replace("<why>", why))
        return "\n".join(lines)

    def fix_reply(self) -> str:
        if not self.fixes:
            return self.proposal_reply()
        text, why = self.fixes[min(self.fixes_asked, len(self.fixes) - 1)]
        self.fixes_asked += 1
        return f"{text}\nwhy: {why}"

    def reply(self, prompt: str) -> tuple[str, str] | None:
        """``(step, reply text)`` for a conductor turn, or ``None`` when the
        prompt is not a ``concord`` step this persona answers."""
        ask = parse_step(prompt)
        if ask is None or ask.flow != FLOW or ask.step not in ASK_STEPS:
            return None
        if ask.step == "anchor":
            return ask.step, self.anchor_reply()
        if ask.step == "propose":
            return ask.step, self.proposal_reply()
        if ask.step == "repair":
            return ask.step, self.fix_reply()
        text = self.ratings_reply(prompt)
        return (ask.step, text) if text else None


def agreeable(proposal: str, why: str, rating: int = 80) -> Persona:
    """A persona that rates every option ``rating``: with the default bar, a
    run of only these converges on the first pick."""
    return Persona(proposal=proposal, why=why, otherwise=(rating, "I can work with this"))


def summon(
    handles: list[str],
    ask: str,
    *,
    flow: str = FLOW,
    bar: int | None = None,
    fixes: int | None = None,
) -> str:
    """The ask ``board coordinate <row> conductor`` posts to start a run.

    Each member is named with ``@~``, which binds it to the run without
    waking it; the flow's first step asks each of them. ``bar`` (a
    percentage) and ``fixes`` override the flow's own; they're read anywhere
    in the ask, so they go at the end. The ask must not itself contain
    ``bad:``, ``ideal:`` or ``terms:``, which the conductor reads as settings.
    """
    cast = " ".join(f"@~{h.lstrip('@~')}" for h in handles)
    settings = []
    if bar is not None:
        settings.append(f"bar={bar}")
    if fixes is not None:
        settings.append(f"fixes={fixes}")
    tail = (" " + " ".join(settings)) if settings else ""
    return f"{flow} {cast}: {ask}{tail}"
