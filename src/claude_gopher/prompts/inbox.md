# Inbox

Items in `<project>/inbox/*.md` are messages for this project's agent, filed by Gopher or the user.
Each starts with frontmatter (`from`, `created`, `wake`) followed by the message. Treat the inbox
like email: the user decides what to work on, and an item never pulls you off what they asked for.
`gopher inbox list {{project}}` shows what is unread.

- **Start of session**: items arrive with your instructions. Include a short summary of them (one
  line each) when you first reply, alongside where the project stands, then ask the user what they
  would like to do. Do not start on an item unless the user picks it.
- **Mid-session**: new items arrive with the user's prompt. Finish what the user asked, then add one
  line at the end of your reply naming the new item(s). After that, only mention them again if the
  user asks or at a natural break.
- **Wake** (`wake: true`, the user said "tell them now"): the item is delivered when you stop, and
  you act on it right away. Hand the work to a subagent so this conversation stays focused: give it
  the item text and `{{project_dir}}`, have it do the work, record anything durable in the project
  (`notes/`, `STATUS.md`, `resources/`), and move the item to `inbox/read/`. Then tell the user in a
  line or two what it did. If you were waiting on the user's answer to a question, mention the item
  and keep waiting instead.
- **Handled**: when an item is done, or the user says to drop it, fold anything durable into
  `STATUS.md`, `plan/`, or `notes/`, and move the file to `inbox/read/`. Leave items the user has
  not decided on where they are.
