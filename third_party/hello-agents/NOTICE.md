# Hello Agents / StockSage message helpers

`src/runtime/chat_messages.py` adapts the message dictionary conversion and
conversation-to-LLM-message conversion from:

- Repository: https://github.com/datawhalechina/hello-agents
- Commit: 2a506bc7e0e3e2235435559b7f2825057ce9a09c
- Paths: `Co-creation-projects/lcyting-StockSage-agent/HelloAgents Optimized/hello_agents/core/message.py`
  and `conversation.py`.
- Attribution: Datawhale Hello Agents contributors; StockSage contributor lcy.
- License: Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International,
  as declared by `HelloAgents Optimized/LICENSE`; the license text is included here.

Changes: mappings replace framework objects; IDs and timestamps belong to SQLite;
branching, global active-conversation state, JSON persistence and automatic deletion
are omitted. The adapted helper module retains CC BY-NC-SA 4.0.

`ConversationManager` and chapter 9 context examples informed the design; they are
not installed as dependencies. The enclosing StockSage directory's MIT declaration
is not substituted for the selected nested directory's license.
