"""The stub personas the nightly suite negotiates audit-log retention with.

Each scenario returns fresh :class:`~libs.concord.Persona` objects keyed by
handle, in the order the summon names them (the conductor letters options and
breaks ties in that order). With every persona's anchors at ``(0, 100)`` a
rating is placed exactly where it reads, so the outcome of each run can be
worked out by hand against the default bar of 60:

``happy``
    Each rates its own option 95 and the other's 70. Both options sit at 70 or
    above for everyone, so the first pick (A, the earlier letter on a tie)
    converges.
``counter``
    The two first options leave someone at 20 or 40. The pick is B (its
    lowest, 40, beats A's 20), and stub-proposer, at 40, is asked for a fix.
    Its fix (45 days) is rated 80 and 75, so the re-pick converges.
``stalemate``
    Each rates only its own number above 10, fixes included. Every option's
    lowest rating is 10, the pick stays A, stub-reject stays least happy, and
    once the summon's fixes are used up the best found stands: ``resolved``.
"""

from __future__ import annotations

from libs.concord import Liking, Persona, agreeable

THIRTY = "Keep audit logs for 30 days, then delete them."
NINETY = "Keep audit logs for 90 days to cover the quarterly audit."
SIXTY = "Keep audit logs for 60 days as a middle ground."


def happy() -> dict[str, Persona]:
    return {
        "stub-a": Persona(
            proposal=THIRTY,
            why="30 days keeps storage cost down and covers incident review",
            likes=(Liking("30 days", 95, "it keeps storage cost down"),),
            otherwise=(70, "it costs more, but the budget can carry it"),
        ),
        "stub-b": Persona(
            proposal=NINETY,
            why="the quarterly audit reads logs up to 90 days back",
            likes=(Liking("90 days", 95, "it covers the whole audit window"),),
            otherwise=(70, "the audit can be served from the monthly exports"),
        ),
    }


def counter() -> dict[str, Persona]:
    return {
        "stub-proposer": Persona(
            proposal=THIRTY,
            why="30 days keeps storage cost down",
            likes=(
                Liking("30 days", 95, "it keeps storage cost down"),
                Liking("45 days", 80, "the cold-storage export keeps the cost close to 30 days"),
            ),
            otherwise=(40, "60 days doubles what we pay for log storage"),
            fixes=(
                (
                    "Keep audit logs for 45 days, then export them to cold storage for the audit.",
                    "the export answers the audit window without paying for 60 days of hot storage",
                ),
            ),
        ),
        "stub-counter": Persona(
            proposal=SIXTY,
            why="the monthly audit needs two full months of logs",
            likes=(
                Liking("60 days", 95, "it covers two full months"),
                Liking("45 days", 75, "the cold-storage export covers the second month"),
            ),
            otherwise=(20, "30 days misses the monthly audit"),
        ),
    }


def stalemate() -> dict[str, Persona]:
    return {
        "stub-accept": Persona(
            proposal=THIRTY,
            why="anything longer breaks our storage budget",
            likes=(Liking("30 days", 95, "it fits the storage budget"),),
            otherwise=(10, "anything longer than 30 days breaks the storage budget"),
            fixes=(
                ("Delete audit logs after 30 days and keep only an incident index.", "it stays in budget"),
                ("Purge audit logs at 30 days, with no archive.", "it is the only option in budget"),
                ("Hold audit logs for 30 days on the cheapest storage tier.", "it stays in budget"),
            ),
        ),
        "stub-reject": Persona(
            proposal=NINETY,
            why="the audit obligation is 90 days and we will not accept less",
            likes=(Liking("90 days", 95, "it meets the audit obligation"),),
            otherwise=(10, "anything shorter than 90 days fails the audit obligation"),
            fixes=(
                (
                    "Retain audit logs for 90 days, with read access limited to the compliance team.",
                    "limiting access answers the cost of reviewing them, not the cost of storing them",
                ),
                (
                    "Retain audit logs for 90 days and charge the extra storage to the compliance budget.",
                    "the compliance budget pays, so the storage budget is not touched",
                ),
                (
                    "Store 90 days of audit logs compressed on the archive tier.",
                    "compression cuts the cost while keeping the full audit window",
                ),
            ),
        ),
    }


def hub_spoke(handles: list[str]) -> dict[str, Persona]:
    """Agreeable personas for the hub-and-spoke tests, one retention number each."""
    proposals = [THIRTY, NINETY, SIXTY]
    return {
        h: agreeable(proposals[i % len(proposals)], "it is the window my team can support")
        for i, h in enumerate(handles)
    }
