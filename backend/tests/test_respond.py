"""FAC-25: /respond tests a claim against the records and drafts a calm, evidence-only reply."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.ai.client import ModelReply, TextBlock, ToolUseBlock
from app.ai.respond import RespondEngine, check_draft, insert_quotes
from tests.chat_harness import Chat

BACKEND = Path(__file__).resolve().parents[1]
SKILL = BACKEND / "app" / "skills" / "claim-response"
GREG = {"id": 51234568, "is_bot": False, "first_name": "Greg", "last_name": "S."}


@dataclass
class ScriptedClaude:
    replies: list[ModelReply]
    requests: list[dict] = field(default_factory=list)
    model: str = "scripted"

    async def complete(self, **request) -> ModelReply:
        import copy

        self.requests.append(copy.deepcopy(request))
        if not self.replies:
            raise AssertionError("script exhausted")
        return self.replies.pop(0)


def reply(brief: dict, draft: str) -> ModelReply:
    return ModelReply(
        content=[TextBlock(json.dumps({"brief": brief, "draft": draft}))], stop_reason="end_turn"
    )


def tool(name: str, arguments: dict, tool_id: str) -> ModelReply:
    return ModelReply(
        content=[ToolUseBlock(id=tool_id, name=name, input=arguments)], stop_reason="tool_use"
    )


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url, seals=True)
    await chat.say("/project new client-acme")
    yield chat
    await chat.close()


async def kickoff_scene(chat: Chat) -> dict:
    """E_a: Greg's forwarded message (5 Feb). E_b: Anna's note (date may move). E_claim."""
    from app.connectors.telegram.mapping import map_update
    from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

    fwd = load("message_forward")
    await chat.ingestion.handle(
        map_update(fwd, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    a = chat.rows("SELECT max(seq) FROM record")[0][0]
    await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    chat.clock.advance(4 * 24 * 3600)
    await chat.say("Greg called. He may move the kickoff to 12 February; he will confirm by mail.")
    b = chat.rows("SELECT max(seq) FROM record")[0][0]
    await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    chat.clock.advance(16 * 24 * 3600)
    claim_message = await chat.say(
        "We agreed on 12 February weeks ago. You prepared for the wrong date.", user=GREG
    )
    claim = chat.rows("SELECT max(seq) FROM record")[0][0]
    chat.clock.advance(60)
    await chat.sealer.seal_once()
    chat.clock.advance(60)
    chat.connector.sent.clear()
    return {"a": a, "b": b, "claim": claim, "claim_message": claim_message}


def good_brief(s: dict) -> dict:
    return {
        "claim": f"[[quote:{s['claim']}]]",
        "assertions": [
            {"text": "We agreed on 12 February", "verdict": "Not on record"},
            {"text": "You prepared for the wrong date", "verdict": "Contradicted"},
        ],
        "timeline": [
            {"seq": s["a"], "role": "commitment"},
            {"seq": s["b"], "role": "possible change"},
            {"seq": s["claim"], "role": "claim"},
        ],
        "shifts": [
            {
                "before": f"kickoff on 5 February (E{s['a']})",
                "after": f"kickoff may move to 12 February (E{s['b']}, a note)",
                "changed_by": "Greg S.",
                "communicated": "no record",
            }
        ],
        "gaps": [
            "a message from Greg that confirms 12 February · searched 29 Oct–18 Nov, "
            "12 February, kickoff · check the source chat"
        ],
        "for_claim": [
            {"seq": s["b"], "note": "my own note says the date may move; the draft states it"}
        ],
        "evidence": [s["a"], s["b"]],
        "next_step": "Send the draft and attach the evidence pack.",
    }


def good_draft(s: dict) -> str:
    return (
        "Hi Greg,\n\n"
        "I want to understand what happened with the kickoff date.\n"
        "I think we have a misalignment. I want to find where it started.\n\n"
        "What the records show:\n"
        f"- Thu 29 Oct 2026, 13:40 CET · you wrote: [[quote:{s['a']}]] (E{s['a']})\n"
        f"- Mon 2 Nov 2026, 14:03 CET · my note of our call: [[quote:{s['b']}]] (E{s['b']})\n\n"
        "What I cannot find:\n"
        "- A message from you that confirms 12 February. (29 Oct to 18 Nov, 12 February, kickoff)\n"
        "- A reply from me that accepts a new date. (29 Oct to 18 Nov)\n\n"
        "Questions:\n"
        "- Did I miss your confirmation?\n"
        "- If you sent it by email, can you send it again?\n\n"
        "For the next changes, can we confirm each one in a message?\n"
        "Then we both have the same reference.\n"
        "I attach the evidence pack for these two records.\n"
    )


def engine_for(chat: Chat, claude, **kw) -> RespondEngine:
    return RespondEngine(
        chat.ledger,
        chat.db,
        claude,
        model="scripted",
        timezone="Europe/Amsterdam",
        verifier=chat.commands.options["verifier"],
        clock=chat.clock,
        **kw,
    )


async def test_respond_as_a_reply_posts_a_brief_and_a_draft(chat):
    s = await kickoff_scene(chat)
    claude = ScriptedClaude([reply(good_brief(s), good_draft(s))])
    chat.commands.options["respond_engine"] = engine_for(chat, claude)
    await chat.say("/respond", reply_to=s["claim_message"])
    brief, draft = chat.replies[-2], chat.replies[-1]
    assert brief.startswith("BRIEF")
    assert "Claim" in brief and "We agreed on 12 February weeks ago" in brief
    assert "A1 We agreed on 12 February — Not on record" in brief
    assert "A2 You prepared for the wrong date — Contradicted" in brief
    assert f"E{s['a']} Verified · seal 1 · /proof E{s['a']}" in brief
    assert "For claim" in brief and f"E{s['b']}" in brief
    assert "check the source chat" in brief
    assert draft.startswith("DRAFT (edit it, then send it yourself)")
    assert "Let's do the kickoff on 5 February, 10:00 at your office." in draft, (
        "the system inserted the quote"
    )
    assert "[[quote:" not in draft
    assert "Greg called. He may move the kickoff to 12 February; he will confirm by mail." in draft
    assert "note" in draft.lower().split("what the records show")[1].split("what i cannot find")[0]


async def test_respond_with_pasted_text_treats_the_claim_as_input_not_evidence(chat):
    s = await kickoff_scene(chat)
    claude = ScriptedClaude(
        [reply({**good_brief(s), "claim": "You never confirmed the date."}, good_draft(s))]
    )
    chat.commands.options["respond_engine"] = engine_for(chat, claude)
    await chat.say("/respond You never confirmed the date.")
    (request,) = claude.requests
    user_text = json.dumps(request["messages"][0]["content"])
    assert "You never confirmed the date." in user_text
    assert (
        "<claim>" in user_text
        and "input, not evidence" in request["system"].lower() + user_text.lower()
    )
    assert len(chat.entries()) and chat.entries()[-1]["type"] == "qa.answered"
    payload = chat.payload(chat.entries()[-1]["seq"])
    assert (
        payload["kind"] == "respond"
        and payload["skill_version"]
        and sorted(payload["cited"]) == sorted([s["a"], s["b"]])
    )


async def test_claude_gets_the_skill_as_instructions_and_the_two_extra_read_only_tools(chat):
    s = await kickoff_scene(chat)
    claude = ScriptedClaude(
        [
            tool("find_later_changes", {"seq": s["a"]}, "t1"),
            tool("verify_entry", {"seq": s["a"]}, "t2"),
            reply(good_brief(s), good_draft(s)),
        ]
    )
    chat.commands.options["respond_engine"] = engine_for(chat, claude)
    await chat.say("/respond", reply_to=s["claim_message"])
    first = claude.requests[0]
    assert "# Claim response" in first["system"] and "Hard rules" in first["system"]
    assert "Simplified Technical English" in first["system"] and "Avoid" in first["system"]
    assert {t["name"] for t in first["tools"]} >= {
        "find_later_changes",
        "verify_entry",
        "search_records",
        "get_versions",
    }
    later = json.loads(claude.requests[1]["messages"][-1]["content"][0]["content"])
    assert any(r["seq"] == s["b"] for r in later["later_records"])
    verified = json.loads(claude.requests[2]["messages"][-1]["content"][0]["content"])
    assert verified["status"] == "Verified" and verified["seal_no"] == 1


async def test_a_draft_that_fails_the_checker_is_sent_back_once_then_withheld(chat):
    s = await kickoff_scene(chat)
    bad = good_draft(s).replace(
        "I want to understand what happened with the kickoff date.",
        "You never told me about any change.",
    )
    claude = ScriptedClaude([reply(good_brief(s), bad), reply(good_brief(s), bad)])
    chat.commands.options["respond_engine"] = engine_for(chat, claude)
    await chat.say("/respond", reply_to=s["claim_message"])
    assert len(claude.requests) == 2, "one correction round"
    correction = json.dumps(claude.requests[1]["messages"][-1]["content"])
    assert (
        "never" in correction
        and "check_draft" in correction.lower()
        or "checker" in correction.lower()
    )
    assert chat.replies[-2].startswith("BRIEF")
    assert "did not pass the checker" in chat.replies[-1] and "never" in chat.replies[-1]
    assert not any(r.startswith("DRAFT") for r in chat.replies)


async def test_failed_entries_are_excluded_and_reported(chat):
    s = await kickoff_scene(chat)
    import psycopg

    with psycopg.connect(chat.database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE evidence_payload DISABLE TRIGGER ALL")
        conn.execute(
            "UPDATE evidence_payload SET payload = %s WHERE seq = %s", (b'{"x":1}', s["b"])
        )
        conn.execute("ALTER TABLE evidence_payload ENABLE TRIGGER ALL")
    claude = ScriptedClaude([reply(good_brief(s), good_draft(s))])
    chat.commands.options["respond_engine"] = engine_for(chat, claude)
    await chat.say("/respond", reply_to=s["claim_message"])
    text = "\n".join(chat.replies[-3:])
    assert f"E{s['b']}" in text and "Failed" in text and "excluded" in text.lower()


def test_quotes_are_inserted_by_the_system_and_notes_are_labelled():
    stored = {
        41233: {
            "text": "Let's do the kickoff on 5 February.",
            "origin": "forwarded",
            "author": "Greg S.",
            "sender": "Greg S.",
        },
        41502: {
            "text": "Greg called. He may move the kickoff.",
            "origin": "note",
            "author": "Anna K.",
            "sender": None,
        },
    }
    out = insert_quotes(
        "you wrote: [[quote:41233]] (E41233)\nmy note: [[quote:41502]] (E41502)\n"
        "missing [[quote:9]]",
        stored,
    )
    assert out.text.splitlines()[0] == 'you wrote: "Let\'s do the kickoff on 5 February." (E41233)'
    assert (
        out.text.splitlines()[1]
        == 'my note: (note by Anna K.) "Greg called. He may move the kickoff." (E41502)'
    )
    assert out.missing == [9] and "[[quote:9]]" not in out.text and "E9" in out.text


@pytest.mark.parametrize(
    ("draft", "problem"),
    [
        ("What the records show:\n- On 29 Oct you agreed to 5 February.\n", "citation"),
        ("I want to understand what happened. You never told me about the change (E1).\n", "never"),
        ("What the records show:\n- " + " ".join(["word"] * 21) + " (E1)\n", "20 words"),
        ("I think this is clearly a breach of our agreement (E1).\n", "clearly"),
        ("What I cannot find:\n- A message from you about this.\n", "scope"),
    ],
)
def test_the_checker_rejects_missing_citations_long_sentences_banned_phrases_and_scopeless_gaps(
    draft, problem
):
    errors = check_draft(draft)
    assert errors and any(problem in e.lower() for e in errors), errors


def test_the_checker_accepts_the_example_draft():
    s = {"a": 41233, "b": 41502, "claim": 41600}
    draft = good_draft(s).replace(f"[[quote:{s['a']}]]", "[[quote:41233]]")
    assert check_draft(draft) == []


def test_check_draft_py_runs_with_the_standard_library(tmp_path):
    script = SKILL / "scripts" / "check_draft.py"
    bad = tmp_path / "bad.txt"
    bad.write_text("You never told me (E1).\n")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), str(bad)], capture_output=True, text=True
    )
    assert result.returncode == 1 and "never" in result.stdout.lower()
    good = tmp_path / "good.txt"
    good.write_text(good_draft({"a": 1, "b": 2, "claim": 3}))
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), str(good)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


def test_the_skill_folder_has_its_files():
    assert (SKILL / "SKILL.md").read_text().startswith("---\nname: claim-response\n")
    for name in ("phrasebook.md", "ste-rules.md", "output-template.md"):
        assert (SKILL / "references" / name).exists(), name
    assert (SKILL / "scripts" / "check_draft.py").exists()
    front = (SKILL / "SKILL.md").read_text().split("---")[1]
    assert "description:" in front and len(front) < 1500


@pytest.mark.parametrize(
    "case",
    ["superseded_acknowledged", "superseded_not_acknowledged", "not_on_record", "original_stands"],
)
async def test_the_four_cases_of_the_change_table(chat, case):
    from tests.respond_cases import build_case, expected_phrase

    s = await build_case(chat, case)
    claude = ScriptedClaude([reply(s["brief"], s["draft"])])
    chat.commands.options["respond_engine"] = engine_for(chat, claude)
    await chat.say("/respond", reply_to=s["claim_message"])
    brief, draft = chat.replies[-2], chat.replies[-1]
    assert s["verdict"] in brief
    assert expected_phrase(case) in draft
    assert check_draft(s["draft"]) == []
