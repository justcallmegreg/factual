"""The four cases of the specification's change table, as record fixtures plus scripted outputs."""

from __future__ import annotations

from tests.chat_harness import Chat

GREG = {"id": 51234568, "is_bot": False, "first_name": "Greg", "last_name": "S."}

VERDICTS = {
    "superseded_acknowledged": "Superseded, acknowledged",
    "superseded_not_acknowledged": "Superseded, not acknowledged",
    "not_on_record": "Not on record",
    "original_stands": "Original stands",
}

PHRASES = {
    "superseded_acknowledged": "I confirmed the new date",
    "superseded_not_acknowledged": "I cannot find a reply that confirms the new date",
    "not_on_record": "I cannot find a message that confirms 12 February",
    "original_stands": "Can you show me where it was shared?",
}


def expected_phrase(case: str) -> str:
    return PHRASES[case]


async def _forward(chat: Chat, text: str, sent_days_ago: int = 0) -> int:
    from app.connectors.telegram.mapping import map_update
    from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

    raw = load("message_forward")
    chat.next_update += 1
    chat.next_message += 1
    raw["update_id"] = chat.next_update
    raw["message"]["message_id"] = chat.next_message
    raw["message"]["text"] = text
    raw["message"]["date"] = int(chat.clock().timestamp())
    raw["message"]["forward_origin"]["date"] = int(chat.clock().timestamp()) - sent_days_ago * 86400
    raw["message"]["forward_date"] = raw["message"]["forward_origin"]["date"]
    await chat.ingestion.handle(
        map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    return seq


async def build_case(chat: Chat, case: str) -> dict:
    commitment = await _forward(chat, "Let's do the kickoff on 5 February, 10:00 at your office.")
    chat.clock.advance(4 * 86400)
    change = None
    ack = None
    if case in ("superseded_acknowledged", "superseded_not_acknowledged"):
        change = await _forward(chat, "The kickoff moves to 12 February. Same time, same place.")
        chat.clock.advance(3600)
        if case == "superseded_acknowledged":
            await chat.say("Confirmed, 12 February works for us.")
            ack = chat.rows("SELECT max(seq) FROM record")[0][0]
            await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    elif case == "not_on_record":
        await chat.say(
            "Greg called. He may move the kickoff to 12 February; he will confirm by mail."
        )
        change = chat.rows("SELECT max(seq) FROM record")[0][0]
        await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    chat.clock.advance(14 * 86400)
    claim_message = await chat.say(
        "We agreed on 12 February weeks ago. You prepared for the wrong date.", user=GREG
    )
    claim = chat.rows("SELECT max(seq) FROM record")[0][0]
    chat.clock.advance(60)
    await chat.sealer.seal_once()
    chat.clock.advance(60)
    chat.connector.sent.clear()
    cited = [c for c in (commitment, change, ack) if c]
    verdict = VERDICTS[case]
    shows = [f"- Thu 29 Oct 2026, 13:40 CET · you wrote: [[quote:{commitment}]] (E{commitment})"]
    if case in ("superseded_acknowledged", "superseded_not_acknowledged"):
        shows.append(f"- Mon 2 Nov 2026, 13:03 CET · you wrote: [[quote:{change}]] (E{change})")
    if case == "superseded_acknowledged":
        shows.append(f"- Mon 2 Nov 2026, 14:03 CET · I wrote: [[quote:{ack}]] (E{ack})")
    if case == "not_on_record":
        shows.append(
            f"- Mon 2 Nov 2026, 14:03 CET · my note of our call: [[quote:{change}]] (E{change})"
        )
    body = {
        "superseded_acknowledged": [f"I confirmed the new date on 2 November (E{ack or 0}).\n"],
        "superseded_not_acknowledged": [
            f"On 2 Nov, you wrote that the kickoff moves to 12 February (E{change}).\n",
            "I cannot find a reply that confirms the new date. "
            "(2 Nov to 18 Nov, confirm, 12 February)\n",
        ],
        "not_on_record": [
            f"My note of 2 Nov says that the date may move (E{change}).\n",
            "I cannot find a message that confirms 12 February. (29 Oct to 18 Nov, 12 February)\n",
        ],
        "original_stands": [
            "I cannot find a message that changes this between 29 Oct and 18 Nov. "
            "(kickoff, February)\n",
            "Can you show me where it was shared?\n",
        ],
    }[case]
    draft = (
        "Hi Greg,\n\nI want to understand what happened with the kickoff date.\n\n"
        "What the records show:\n" + "\n".join(shows) + "\n\n"
        "What I cannot find:\n"
        + "".join("- " + line if "cannot find" in line else "" for line in body)
        + "\nQuestions:\n- Did I miss a message?\n\n"
        + "".join(line for line in body if "cannot find" not in line)
        + "For the next changes, can we confirm each one in a message?\n"
        + "I attach the evidence pack for these records.\n"
    )
    brief = {
        "claim": f"[[quote:{claim}]]",
        "assertions": [{"text": "We agreed on 12 February", "verdict": verdict}],
        "timeline": [{"seq": seq, "role": "record"} for seq in cited],
        "shifts": (
            [
                {
                    "before": f"kickoff on 5 February (E{commitment})",
                    "after": f"12 February (E{change})",
                    "changed_by": "Greg S.",
                    "communicated": "yes" if ack else "no record",
                }
            ]
            if change and case != "not_on_record"
            else []
        ),
        "gaps": []
        if case == "superseded_acknowledged"
        else ["a confirmation of 12 February · searched 29 Oct–18 Nov · check the source chat"],
        "for_claim": [{"seq": change, "note": "the change is on record"}]
        if case.startswith("superseded")
        else [],
        "evidence": cited,
        "next_step": "Send the draft.",
    }
    return {
        "claim_message": claim_message,
        "claim": claim,
        "cited": cited,
        "verdict": verdict,
        "brief": brief,
        "draft": draft,
    }
