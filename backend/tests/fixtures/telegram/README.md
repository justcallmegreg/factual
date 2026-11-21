# Telegram fixtures

Raw `getUpdates` items, one update per file, shaped after the Bot API reference
(`core.telegram.org/bots/api`). They were built by hand for the contract tests
because no bot token was available while the connector was written; when a
real capture chat exists, replace them with recorded updates of the same scenes.

| File | Scene |
| --- | --- |
| `message_text.json` | A member sends a note to the capture chat |
| `message_edit.json` | The member edits that note |
| `message_edit_unchanged.json` | Telegram re-sends the message with no text change |
| `message_reply.json` | A reply to the note |
| `message_forward.json` | A forwarded message with its original sender and time |
| `message_photo_1.json`, `message_photo_2.json` | Two photos sent as one album |
| `message_document.json` | A PDF with a caption |
| `message_voice.json` | A voice note |
| `message_location.json` | A location (floating-point coordinates) |
| `command_log_pending.json` | `/log pending` as a bot command |
| `command_with_mention.json` | `/help@FactualThothBot` |
| `callback_query.json` | A tap on a project button |
| `message_private.json` | A private message to Thoth |
| `message_other_group.json` | A message in a group that is not the capture chat |
| `my_chat_member_added.json` | Thoth is added to another group |
| `message_from_bot.json` | Thoth's own message |
| `message_service.json` | A service message (a member joined) |
