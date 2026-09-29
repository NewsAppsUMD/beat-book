"""A draft must start at its title, even when the model (GLM-5.3 on Ollama)
writes its reasoning into the answer first."""

import asyncio

from agent import _without_preamble, strip_preamble

BOOK = "# Chicago Housing Beat Book\n\n## Beat Overview\n\nThe CHA board voted in March.\n"


def test_book_that_starts_with_its_title_is_untouched():
    assert strip_preamble(BOOK) == (BOOK, "")


def test_reasoning_on_its_own_lines_is_removed():
    draft = "The user wants a beat book. Let me plan the sections first.\n\nOkay, writing it now.\n\n" + BOOK
    book, preamble = strip_preamble(draft)
    assert book == BOOK and preamble.startswith("The user wants")


def test_title_glued_onto_the_reasoning_is_found():
    # The shape GLM-5.3 produced: reasoning, then the answer with no break.
    draft = "I should reply with only the Markdown document, no preamble.# Chicago Housing Beat Book\n\n## Beat Overview\n\nText."
    book, preamble = strip_preamble(draft)
    assert book.startswith("# Chicago Housing Beat Book") and preamble.endswith("no preamble.")


def test_second_level_heading_is_used_when_there_is_no_title():
    draft = "Let me write this.\n\n## Beat Overview\n\nText."
    assert strip_preamble(draft)[0].startswith("## Beat Overview")


def test_hashtags_and_draft_without_headings_are_left_alone():
    assert strip_preamble("The Bears were the #1 story. #Hammond was trending.") == \
        ("The Bears were the #1 story. #Hammond was trending.", "")


def test_hand_off_passes_the_clean_book_and_reports_the_cut():
    got, messages = [], []

    async def on_beat_book(name, md):
        got.append(md)

    async def on_message(text):
        messages.append(text)

    hand_off = _without_preamble(on_beat_book, on_message)
    asyncio.run(hand_off("x.md", "Let me think.\n\n" + BOOK))
    assert got == [BOOK] and "Removed 13 characters" in messages[0]
    asyncio.run(hand_off("x.md", BOOK))
    assert got[-1] == BOOK and len(messages) == 1


def test_agent_loop_hands_off_the_draft_without_reasoning():
    from agent import run_agent
    from chat_provider import ChatResponse
    from pipeline import PipelineResult

    pr = PipelineResult(stories=[{"title": "CHA vote", "content": "The CHA board voted. " * 20}],
                        topics={"CHA": [0]}, story_topics=[["CHA"]],
                        broad_topics={"CHA": [0]}, specific_topics={})

    class Scripted:
        explore_model = agent_model = label_model = normalize_model = "glm-5.3:cloud"

        def create(self, model, messages, tools=None, tool_choice=None, **kw):
            if tool_choice and tool_choice.get("type") == "none":      # the final write
                return ChatResponse(content=[{"type": "text", "text":
                    "The user wants only Markdown. I should start with the title.# CHA Beat Book\n\n## Beat Overview\n\nThe CHA board voted.\n"}],
                    stop_reason="end_turn")
            return ChatResponse(content=[
                {"type": "tool_use", "id": "a", "name": "read_stories_in_topic", "input": {"topic": "CHA"}},
                {"type": "tool_use", "id": "b", "name": "read_story", "input": {"index": 0}}],
                stop_reason="tool_use")

    books, messages = [], []

    async def on_beat_book(name, md):
        books.append(md)

    async def on_message(text):
        messages.append(text)

    asyncio.run(run_agent(pipeline_result=pr, provider=Scripted(), on_message=on_message,
                          on_beat_book=on_beat_book, selected_topics=["CHA"]))
    assert books == ["# CHA Beat Book\n\n## Beat Overview\n\nThe CHA board voted."]   # the agent trims the draft
    assert any("Removed" in m for m in messages)
