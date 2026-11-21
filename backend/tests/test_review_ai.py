"""Fix pass after the whole-branch review: the Claude integration.

Strict schemas carry only keywords the API accepts; every content block (thinking
included) is replayed in tool rounds; effort and a cut-off answer are handled; /respond
cites only records Claude was shown and shares the citation syntax with the checker; the
checker accepts the specification's own example; /respond counts toward the daily limit;
a Claude failure is logged with its cause; the skill folder explains its Claude Code use.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.client import ClaudeUnavailable, ModelReply, TextBlock, ToolUseBlock
from app.ai.respond import RespondEngine, check_draft, cited_entries
from tests.chat_harness import Chat
from tests.test_ask_claude import ScriptedClaude, seed, set_claude, verdict_reply
from tests.test_respond import GREG, good_brief, good_draft, kickoff_scene

BACKEND = Path(__file__).resolve().parents[1]
SKILL = BACKEND / "app" / "skills" / "claim-response"

FORBIDDEN = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "patternProperties",
}  # fmt: skip


def keywords(schema) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in FORBIDDEN:
                found.add(key)
            if key != "enum":
                found |= keywords(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= keywords(item)
    return found


def test_strict_schemas_use_only_keywords_the_api_accepts():
    from app.ai.respond import EXTRA_TOOLS
    from app.ai.respond import OUTPUT_SCHEMA as RESPOND_SCHEMA
    from app.ai.schema import OUTPUT_SCHEMA as ASK_SCHEMA
    from app.ai.tools import TOOLS

    for tool in TOOLS + EXTRA_TOOLS:
        assert tool["strict"] is True
        assert keywords(tool["input_schema"]) == set(), tool["name"]
    assert keywords(ASK_SCHEMA) == set() and keywords(RESPOND_SCHEMA) == set()


async def test_the_real_client_sends_effort_and_keeps_every_block_for_replay(monkeypatch):
    import anthropic
    from anthropic.types import TextBlock as SdkText
    from anthropic.types import ThinkingBlock

    from app.ai.client import AnthropicClaude

    client = AnthropicClaude("sk-ant-test", "claude-sonnet-5-5")
    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            content=[
                ThinkingBlock(type="thinking", thinking="let me look", signature="sig"),
                SdkText(type="text", text='{"a": 1}'),
            ],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            model="claude-sonnet-5-5",
        )

    monkeypatch.setattr(client.client.messages, "create", fake_create)
    reply = await client.complete(
        system="s",
        messages=[],
        tools=[],
        output_schema={"type": "object"},
        max_tokens=100,
        effort="low",
    )
    assert seen["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}},
        "effort": "low",
    }
    assert isinstance(client.client, anthropic.AsyncAnthropic)
    assert reply.raw == [
        {"type": "thinking", "thinking": "let me look", "signature": "sig"},
        {"type": "text", "text": '{"a": 1}'},
    ]
    assert [b.type for b in reply.content] == ["text"]


def thinking_tool_reply(name: str, arguments: dict, tool_id: str = "t1") -> ModelReply:
    raw = [
        {"type": "thinking", "thinking": "I should check the versions", "signature": "sig"},
        {"type": "tool_use", "id": tool_id, "name": name, "input": arguments},
    ]
    return ModelReply(
        content=[ToolUseBlock(id=tool_id, name=name, input=arguments)],
        stop_reason="tool_use",
        raw=raw,
    )


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url, seals=True)
    await chat.say("/project new client-acme")
    yield chat
    await chat.close()


async def test_ask_replays_thinking_blocks_in_the_tool_round(chat):
    s = await seed(chat)
    claude = ScriptedClaude(
        [
            thinking_tool_reply("get_versions", {"seq": s["first"]}),
            verdict_reply("supported", "ok", [{"id": s["first"], "role": "supports"}]),
        ]
    )
    set_claude(chat, claude)
    await chat.say("/ask kickoff")
    second = claude.requests[1]["messages"]
    assert second[1]["role"] == "assistant"
    assert second[1]["content"][0] == {
        "type": "thinking",
        "thinking": "I should check the versions",
        "signature": "sig",
    }
    assert second[1]["content"][1]["type"] == "tool_use"
    assert claude.requests[0]["effort"] == "low"


async def test_a_cut_off_verdict_is_reported_as_such(chat):
    await seed(chat)
    claude = ScriptedClaude([ModelReply(content=[], stop_reason="max_tokens")])
    set_claude(chat, claude)
    await chat.say("/ask kickoff")
    final = chat.connector.sent[-1][1].text
    assert "cut off" in final and "No evidence found" not in final
    assert chat.payload(chat.entries()[-1]["seq"])["reason"] == "cut_off"


def respond_engine(chat: Chat, claude, **kw) -> RespondEngine:
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


async def test_respond_replays_thinking_blocks_and_reports_a_cut_off(chat):
    s = await kickoff_scene(chat)
    claude = ScriptedClaude(
        [
            thinking_tool_reply("verify_entry", {"seq": s["a"]}),
            ModelReply(content=[], stop_reason="max_tokens"),
        ]
    )
    chat.commands.options["respond_engine"] = respond_engine(chat, claude)
    await chat.say("/respond", reply_to=s["claim_message"])
    assert claude.requests[1]["messages"][1]["content"][0]["type"] == "thinking"
    assert claude.requests[0]["effort"] == "medium"
    assert "cut off" in chat.replies[-1]
    assert chat.payload(chat.entries()[-1]["seq"])["reason"] == "cut_off"


async def test_respond_cites_only_records_claude_was_shown(chat):
    s = await kickoff_scene(chat)
    chat.clock.advance(5)
    await chat.say("Unrelated secret about the budget", user=GREG)
    secret = chat.rows("SELECT max(seq) FROM record")[0][0]
    chat.clock.advance(60)
    await chat.sealer.seal_once()
    chat.connector.sent.clear()
    brief = good_brief(s)
    brief["evidence"] = [s["a"], s["b"], secret]
    draft = good_draft(s) + f"\nYou also wrote: [[quote:{secret}]] (E{secret})\n"
    claude = ScriptedClaude(
        [ModelReply(content=[TextBlock(json.dumps({"brief": brief, "draft": draft}))])]
    )
    chat.commands.options["respond_engine"] = respond_engine(chat, claude)
    await chat.say("/respond", reply_to=s["claim_message"])
    text = "\n".join(chat.replies[-3:])
    assert "secret about the budget" not in text
    assert f"E{secret}" in text and "not shown to Claude" in text
    payload = chat.payload(chat.entries()[-1]["seq"])
    assert secret not in payload["cited"] and secret in payload["excluded"]


def test_grouped_citations_are_one_syntax_for_the_checker_and_the_engine():
    assert cited_entries("one (E1, E2) and (E3).") == [1, 2, 3]
    draft = "What the records show:\n- On both days you wrote the same (E1, E2)\n"
    assert check_draft(draft) == []


SPEC_EXAMPLE = (
    "Hi Greg,\n\n"
    "I want to understand what happened with the kickoff date.\n"
    "I think we have a misalignment. I want to find where it started.\n\n"
    "What the records show:\n"
    "- Thu 29 Oct 2026, 13:40 CET · you wrote: \"Let's do the kickoff on 5 February, 10:00 at "
    'your office." (E41233)\n'
    '- Mon 2 Nov 2026, 16:20 CET · my note of our call: "Greg called. He may move the kickoff '
    'to 12 February; he will confirm by mail." (E41502)\n\n'
    "What I cannot find:\n"
    "- A message from you that confirms 12 February.\n"
    "- A reply from me that accepts a new date.\n"
    "I checked my records from 29 Oct to 18 Nov.\n\n"
    "Questions:\n"
    "- Did I miss your confirmation?\n"
    "- If you sent it by email, can you send it again?\n\n"
    "For the next changes, can we confirm each one in a message?\n"
    "Then we both have the same reference.\n"
    "I attach the evidence pack for these two records.\n"
)


@pytest.mark.parametrize(
    "draft",
    [
        SPEC_EXAMPLE,
        "Hi Sue,\n\nI want to understand what happened.\n",
        "What the records show:\n- Mr. Smith wrote on 5 Feb that the date stands (E1)\n",
        "Your message of 29 Oct says 5 February at your office (E41233). "
        "I prepared for that date.\n",
    ],
)
def test_the_checker_accepts_legitimate_drafts(draft):
    assert check_draft(draft) == [], check_draft(draft)


def test_the_checker_still_rejects_the_real_problems():
    assert any("never" in e for e in check_draft("I want to be clear. You never told me (E1).\n"))
    assert any("sue" in e.lower() for e in check_draft("If this goes on, I will sue you (E1).\n"))


async def test_respond_counts_toward_the_daily_limit(chat):
    s = await kickoff_scene(chat)
    body = json.dumps({"brief": good_brief(s), "draft": good_draft(s)})
    claude = ScriptedClaude([ModelReply(content=[TextBlock(body)])])
    chat.commands.options["respond_engine"] = respond_engine(chat, claude, daily_limit=1)
    await chat.say("/respond", reply_to=s["claim_message"])
    assert chat.replies[-1].startswith("DRAFT")
    await chat.say("/respond", reply_to=s["claim_message"])
    assert "Daily limit of 1" in chat.replies[-1] and len(claude.requests) == 1
    set_claude(chat, ScriptedClaude([]), daily_limit=1)
    await chat.say("/ask kickoff")
    assert "Daily limit of 1" in chat.connector.sent[-1][1].text, (
        "/ask and /respond share the limit"
    )


async def test_a_claude_failure_is_logged_with_its_cause(chat, caplog):
    await seed(chat)
    set_claude(chat, ScriptedClaude([], fail=ClaudeUnavailable("API error 400: schema rejected")))
    with caplog.at_level(logging.WARNING, logger="factual.ai.ask"):
        await chat.say("/ask kickoff")
    assert any("API error 400: schema rejected" in r.getMessage() for r in caplog.records)


def test_the_skill_explains_its_use_in_claude_code_and_in_thoth():
    from app.ai.respond import skill_instructions

    text = (SKILL / "SKILL.md").read_text()
    assert (
        "## In Claude Code" in text and "/api/v1/search" in text and "Authorization: Bearer" in text
    )
    assert "python3 -I -S scripts/check_draft.py" in text
    assert "runs the checker" in skill_instructions()
