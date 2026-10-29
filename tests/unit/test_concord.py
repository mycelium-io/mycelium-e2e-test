"""Unit tests for the scripted concord answers and the nightly personas.

The prompts below are what the hub's conductor renders for a ``concord`` run
(``fastapi-backend/app/services/conductor.py`` with the flow in
``protocols.py``), copied from a run of the ``counter`` personas.
"""

from __future__ import annotations

from libs import concord, personas
from libs.concord import DEFAULT_BAR, Liking, Persona

ANCHOR = """concord · anchor · turn 1 of 20 · stub-counter

Before anyone proposes anything for Decide audit log retention: decide how long to keep audit logs

Two outcomes everyone rates the same way:

bad: no agreement: nothing is decided and the task stays open
ideal: your own pick: the option you'd choose if it were up to you alone

How good would each be for your role? Rate each from 0 to 100 on your own scale, one line each, with why:

bad: <0-100> - <why>
ideal: <0-100> - <why>
"""

PROPOSE = """concord · propose · turn 2 of 20 · stub-counter

decide how long to keep audit logs

Propose the one option you think best serves your role for Decide audit log retention: the concrete terms in one or two sentences, then a line saying why, with the facts behind it:

<the option>
why: <the reasons>
"""

SCORE = """concord · score · turn 3 of 20 · stub-counter

The options for Decide audit log retention:

A. Keep audit logs for 30 days, then delete them.
   why (stub-proposer): 30 days keeps storage cost down
B. Keep audit logs for 60 days as a middle ground.
   why (stub-counter): the monthly audit needs two full months of logs

The bar: the option picked has to be at least 60% of the way from no agreement to each member's own pick, for every member.

Rate each option for your role, on the scale you rated the two outcomes on, one line each with your main reason or concern in one short sentence:

A (Keep audit logs for 30…): <0-100> - <why>
B (Keep audit logs for 60…): <0-100> - <why>

Keep the option's name in brackets, so the thread can follow: `B (Ship on Friday): 40 - the Friday window breaks certification`. The reason matters more than the number: it's what the others will have to answer.
"""

REPAIR = """concord · repair · turn 4 of 20 · stub-proposer

The best option so far for Decide audit log retention is:

B. Keep audit logs for 60 days as a middle ground.

Propose something new that answers these objections, not a small change to it:

- stub-proposer: 60 days doubles what we pay for log storage

<the option>
why: <what it answers>
"""

RESCORE = """concord · rescore · turn 5 of 20 · stub-counter

A new option for Decide audit log retention:

C. Keep audit logs for 45 days, then export them to cold storage for the audit.
   why (stub-proposer): the export answers the audit window without paying for 60 days of hot storage

Rate it the same way, one line with your reason in one short sentence:

C (Keep audit logs for 45…): <0-100> - <why>
"""


def _persona() -> Persona:
    return Persona(
        proposal="Keep audit logs for 60 days as a middle ground.",
        why="the monthly audit needs two full months",
        likes=(Liking("60 days", 95, "it covers two months"), Liking("45 days", 75, "the export covers it")),
        otherwise=(20, "30 days misses the monthly audit"),
        fixes=(("first fix", "answers one"), ("second fix", "answers two")),
    )


# ── reading a turn ────────────────────────────────────────────────────────────


def test_parse_step_reads_the_header() -> None:
    ask = concord.parse_step(SCORE)
    assert ask == concord.StepAsk(flow="concord", step="score", again=False, turn=3, cap=20)


def test_parse_step_reads_a_second_asking() -> None:
    ask = concord.parse_step("concord · score (again) · turn 4 of 20 · stub-a\n\nI couldn't read…")
    assert ask is not None and ask.step == "score" and ask.again


def test_parse_step_is_none_for_other_messages() -> None:
    assert concord.parse_step("Running concord with stub-a, stub-b as members.") is None
    assert concord.parse_step("") is None


def test_turn_prompt_is_the_last_ask() -> None:
    turn = {"prompt": "older", "asks": [{"prompt": "older"}, {"prompt": SCORE}]}
    assert concord.turn_prompt(turn) == SCORE
    assert concord.turn_prompt({"prompt": PROPOSE}) == PROPOSE


def test_rating_targets_and_option_texts() -> None:
    assert [label for label, _line in concord.rating_targets(SCORE)] == ["A", "B"]
    texts = concord.option_texts(SCORE)
    assert texts["B"] == "Keep audit logs for 60 days as a middle ground."
    # The example in backticks is not an option to rate.
    assert "Ship on Friday" not in "".join(line for _l, line in concord.rating_targets(SCORE))


# ── answering each step ───────────────────────────────────────────────────────


def test_anchor_reply_rates_bad_and_ideal() -> None:
    step, text = _persona().reply(ANCHOR)
    assert step == "anchor"
    lines = text.splitlines()
    assert lines[0].startswith("bad: 0 - ")
    assert lines[1].startswith("ideal: 100 - ")


def test_propose_reply_is_the_option_then_why() -> None:
    step, text = _persona().reply(PROPOSE)
    assert step == "propose"
    assert text == "Keep audit logs for 60 days as a middle ground.\nwhy: the monthly audit needs two full months"


def test_score_reply_fills_in_every_template_line_from_the_full_text() -> None:
    # The names in brackets are cut at five words ("for 30…"), so the rating
    # has to come from the option's full text listed above them.
    step, text = _persona().reply(SCORE)
    assert step == "score"
    assert text.splitlines() == [
        "A (Keep audit logs for 30…): 20 - 30 days misses the monthly audit",
        "B (Keep audit logs for 60…): 95 - it covers two months",
    ]


def test_rescore_reply_rates_only_the_new_option() -> None:
    step, text = _persona().reply(RESCORE)
    assert step == "rescore"
    assert text == "C (Keep audit logs for 45…): 75 - the export covers it"


def test_repair_reply_takes_the_next_fix_then_keeps_the_last() -> None:
    persona = _persona()
    assert persona.reply(REPAIR) == ("repair", "first fix\nwhy: answers one")
    again = REPAIR.replace("· repair ·", "· repair (again) ·")
    assert persona.reply(again) == ("repair", "second fix\nwhy: answers two")
    assert persona.reply(REPAIR) == ("repair", "second fix\nwhy: answers two")


def test_repair_without_fixes_proposes_again() -> None:
    persona = Persona(proposal="P", why="W")
    assert persona.reply(REPAIR) == ("repair", "P\nwhy: W")


def test_other_flows_and_messages_get_no_reply() -> None:
    persona = _persona()
    assert persona.reply("accord · frame · turn 1 of 9 · stub-a\n\nSay what you understand…") is None
    assert persona.reply("✓ Everyone meets the bar: going with A.") is None


# ── the summon ────────────────────────────────────────────────────────────────


def test_summon_names_members_silently_with_settings_last() -> None:
    assert concord.summon(["stub-a", "@stub-b"], "decide retention") == (
        "concord @~stub-a @~stub-b: decide retention"
    )
    assert concord.summon(["a", "b"], "decide retention", bar=70, fixes=2) == (
        "concord @~a @~b: decide retention bar=70 fixes=2"
    )


# ── the nightly scenarios work out as personas.py says ────────────────────────


def _placed(p: Persona, text: str) -> float:
    bad, ideal = p.anchors
    return (p.rate(text)[0] - bad) / (ideal - bad)


def test_happy_everyone_meets_the_bar_on_every_proposal() -> None:
    cast = personas.happy()
    for rater in cast.values():
        for proposer in cast.values():
            assert _placed(rater, proposer.proposal) >= DEFAULT_BAR


def test_counter_first_options_leave_the_proposer_short_and_its_fix_meets_the_bar() -> None:
    cast = personas.counter()
    proposer, counter = cast["stub-proposer"], cast["stub-counter"]
    assert _placed(proposer, counter.proposal) < DEFAULT_BAR
    assert _placed(counter, proposer.proposal) < DEFAULT_BAR
    fix, _why = proposer.fixes[0]
    assert all(_placed(p, fix) >= DEFAULT_BAR for p in cast.values())


def test_stalemate_nobody_meets_the_bar_on_the_others_options_or_fixes() -> None:
    cast = personas.stalemate()
    for name, rater in cast.items():
        for other_name, other in cast.items():
            if other_name == name:
                continue
            assert _placed(rater, other.proposal) < DEFAULT_BAR
            assert all(_placed(rater, fix) < DEFAULT_BAR for fix, _why in other.fixes)
            # Its own options it rates above the bar, so it never gives in.
            assert all(_placed(rater, fix) >= DEFAULT_BAR for fix, _why in rater.fixes)


def test_hub_spoke_personas_are_agreeable() -> None:
    cast = personas.hub_spoke(["h", "s1", "s2"])
    assert list(cast) == ["h", "s1", "s2"]
    for rater in cast.values():
        assert all(_placed(rater, p.proposal) >= DEFAULT_BAR for p in cast.values())
