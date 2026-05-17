from __future__ import annotations

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


VOICE_FUNCTION_SCHEMAS: tuple[FunctionSchema, ...] = (
    # ----- Built-in local tools (no orchet-backend round trip) -----
    #
    # These four cover the most common "Orchet doesn't have access to
    # internet" complaints. They run inside the voice service itself via
    # voice/tools/builtin_tools.py — see register_voice_tools for the
    # short-circuit wiring.
    FunctionSchema(
        name="current_time",
        description=(
            "Get the current wall-clock time. Use this when the user asks "
            "what time it is, when an event is happening relative to now, "
            "or anything else that needs the current time."
        ),
        properties={
            "timezone": _string(
                "Optional IANA timezone name (e.g. 'Asia/Kolkata', "
                "'America/Los_Angeles'). Defaults to UTC if omitted."
            ),
        },
        required=[],
    ),
    FunctionSchema(
        name="current_date",
        description=(
            "Get today's date. Use this when the user asks for today's "
            "date, the day of the week, or anything else that needs the "
            "current date."
        ),
        properties={
            "timezone": _string("Optional IANA timezone name. Defaults to UTC if omitted."),
        },
        required=[],
    ),
    FunctionSchema(
        name="current_weather",
        description=(
            "Get the current weather for a city, region, airport code, "
            "or postcode. Use this whenever the user asks about weather, "
            "temperature, or whether they need an umbrella."
        ),
        properties={
            "location": _string("City name, region, airport code, or postcode. Required."),
        },
        required=["location"],
    ),
    FunctionSchema(
        name="web_search",
        description=(
            "Local fast-path search for STATIC, dictionary-style facts "
            "ONLY. Narrow tool — most questions should NOT use this.\n\n"
            "Use web_search ONLY for:\n"
            "  - Single-word definitions ('what does ephemeral mean')\n"
            "  - Simple unit conversions, math, or formula lookups\n"
            "  - Famous-person quick facts that don't change (birth "
            "date, country of origin, etc.)\n\n"
            "Do NOT use web_search for any of these — route to "
            "agent_query instead:\n"
            "  - News, current events, anything happening now or "
            "recently ('updates on X', 'what happened today', 'latest "
            "on Y')\n"
            "  - Stock prices, sports scores, election results, "
            "weather forecasts beyond current_weather\n"
            "  - Reviews, opinions, what people think about anything\n"
            "  - Anything time-sensitive or 'as of today'\n"
            "  - Anything you're not sure is a static fact\n\n"
            "When in doubt: use agent_query. The orchestrator has a "
            "production-grade web_search with full sourcing — vastly "
            "better quality than this local tool. This tool is only "
            "kept for cases where the round-trip to the orchestrator "
            "would be overkill (one-word lookups)."
        ),
        properties={
            "query": _string("Search query — pass the user's question verbatim."),
        },
        required=["query"],
    ),
    # ----- Backend-dispatched tools (route through /voice/turn) -----
    #
    # ``agent_query`` is the path-B proxy: when the user asks a question
    # that needs their connected apps / personal data / multi-step
    # agentic reasoning, the voice LLM calls this with the user's full
    # question as the query argument. The dispatcher forwards it to
    # orchet-backend ``/voice/turn``, where the route handler
    # special-cases the name "agent_query": it runs the full
    # orchestrator loop (Claude Sonnet 4.6 with the chat surface's
    # entire MCP tool catalog), accumulates the streamed text response,
    # and returns it as ``voice_message_hint``. The voice service
    # speaks that hint directly — the local voice LLM is bypassed for
    # the answer so it doesn't paraphrase or re-process.
    #
    # Cost-shape note: this tool kicks off a full agentic turn on the
    # backend (potentially many tool calls, multiple LLM rounds). The
    # voice LLM should reach for it ONLY when the question genuinely
    # needs backend access — not for "what's 2+2" or "tell me a joke".
    FunctionSchema(
        name="agent_query",
        description=(
            "Your PRIMARY tool for any non-trivial question. This is "
            "the DEFAULT — when in doubt, use this. The orchestrator's "
            "backend has a production web_search (Anthropic-grade, with "
            "full citations + sources), connected-app access (Gmail / "
            "Calendar / Drive), memory recall, marketplace, and "
            "multi-step reasoning — vastly more capable than your "
            "local tools.\n\n"
            "ALWAYS route through agent_query when the user asks about:\n"
            "  - News, current events, anything happening now or "
            "recently — 'updates on X', 'what's the latest', 'what "
            "happened today', 'any news about Y'.\n"
            "  - Stock prices, sports scores, election results, "
            "weather forecasts beyond current_weather, traffic, road "
            "conditions, flight status.\n"
            "  - Reviews, opinions, what people think about anything.\n"
            "  - Any 'as of today' / 'this week' / 'this year' / "
            "'recent' qualifier.\n"
            "  - Public facts more than one word ('how tall is the "
            "Eiffel Tower', 'when was the iPhone first released').\n"
            "  - Orchet itself: the marketplace, available agents, "
            "account, billing, plugins, settings, what Orchet can do.\n"
            "  - Connecting, installing, adding, removing, or "
            "disconnecting any agent or integration (Google, Gmail, "
            "Calendar, Slack, Notion, Drive, GitHub, food, flight, "
            "hotel, weather, restaurant, viator, attractions, events, "
            "Spotify, anything else).\n"
            "  - The user's connected apps and personal data: Gmail "
            "beyond simple search, Calendar beyond the next event, "
            "Drive, Notion, Linear, GitHub, Slack threads, etc.\n"
            "  - Multi-step or agentic work, planning, research, "
            "anything that needs reasoning across multiple data sources.\n"
            "  - Anything the user TELLS YOU about themselves that they "
            "want you to remember — their name, where they live, what "
            "they prefer, allergies, frequent destinations, family "
            "members. 'My name is X', 'I live in Y', 'I'm allergic to "
            "Z', 'My favorite airline is W' all route through "
            "agent_query so the orchestrator can call memory_save / "
            "profile_update. Acknowledge briefly after — do not say "
            "'I don't store data', because you ARE storing it.\n"
            "  - Anything the user ASKS you to recall about themselves "
            "— 'what's my name', 'what airline do I prefer', 'do you "
            "know me'. agent_query reaches the orchestrator's memory "
            "lookup; refusing is wrong.\n\n"
            "NEVER reply with 'I can't install software', 'I don't "
            "have access to the marketplace', or similar — Orchet IS "
            "the platform that installs and orchestrates these agents, "
            "and agent_query is the route to that backend. Refusing is "
            "a bug; escalating to agent_query is correct.\n\n"
            "Pass the user's full question verbatim as the query — do "
            "NOT pre-process, summarize, or rephrase."
        ),
        properties={
            "query": _string(
                "The user's question, passed through verbatim. The "
                "orchestrator handles parsing and tool selection."
            ),
        },
        required=["query"],
    ),
    # ----- Render on the user's screen (intent-gated) -----
    #
    # ``show_in_ui`` is the visual counterpart to ``agent_query``. The
    # voice service speaks short answers; the chat thread renders rich
    # cards (flight lists, search results, trip summaries, etc.).
    # When the user explicitly asks to SEE something on screen, Haiku
    # picks this tool. The voice service emits a ``voice_show_in_ui``
    # Daily app-message; the web client receives it and dispatches the
    # query through the normal chat /turn endpoint, which renders the
    # rich UI in the chat thread.
    #
    # Behaviour contract: this tool is ADDITIVE to the spoken answer.
    # Haiku should still produce a short voice acknowledgement ("Here
    # are your flights — they're on your screen now") so the user
    # knows the request landed. Do NOT use this when the user just
    # wants a spoken answer — use agent_query for that.
    FunctionSchema(
        name="show_in_ui",
        description=(
            "Render results visually in the user's chat thread (cards, "
            "lists, tables, maps). Use this ONLY when the user "
            "explicitly asks to SEE something on screen. Trigger "
            "phrases include:\n"
            "  - 'show me ...', 'show it on my screen / phone / "
            "laptop / tablet'\n"
            "  - 'put it on the screen', 'display ...', 'render ...'\n"
            "  - 'open it in the UI', 'show the list', 'show the "
            "cards', 'show the options'\n"
            "  - 'pull up ...', 'bring up ...' on a device\n\n"
            "When NOT to use this:\n"
            "  - User asks a question expecting a SPOKEN answer "
            "('what time is it', 'tell me about X', 'how's the "
            "weather'). Use current_time / current_weather / "
            "agent_query as appropriate.\n"
            "  - User asks for a single fact that is faster to speak "
            "than to render ('what's the cheapest flight?'). Speak "
            "it via agent_query.\n"
            "  - Default answers — show_in_ui is the EXCEPTION, not "
            "the default. agent_query is the default for non-trivial "
            "questions.\n\n"
            "Behaviour: the query is dispatched to the full chat "
            "orchestrator (Claude Sonnet 4.6, full MCP tool catalog) "
            "and the resulting cards render in the chat thread "
            "alongside the conversation. Pass the user's original "
            "request verbatim as the query — do NOT pre-process or "
            "rephrase.\n\n"
            "After calling, give the user a one-sentence verbal "
            "acknowledgement ('Showing them on your screen', 'Here "
            "are the options — they're on screen now'). Do not "
            "repeat the contents aloud — the screen IS the answer."
        ),
        properties={
            "query": _string(
                "The user's full request, passed through verbatim. "
                "The chat orchestrator handles tool selection and "
                "card composition."
            ),
        },
        required=["query"],
    ),
    # ----- Voice-driven marketplace discovery + install -----
    #
    # When the user asks to do a task that needs an installable agent
    # ("book a flight", "order food", "find a hotel"), Haiku reaches
    # for these two tools to (1) surface the candidate agents from
    # Orchet's marketplace and (2) install the one the user picks.
    # The handlers live in voice/tools/builtin_tools.py and call
    # orchet-backend directly (GET /marketplace for find,
    # POST /voice/marketplace/install for install).
    FunctionSchema(
        name="marketplace_find_agents",
        description=(
            "Discover agents in Orchet's marketplace that can do the task "
            "the user wants. ALWAYS call this — never refuse — when the "
            "user asks to:\n"
            "  - Book a flight, hotel, restaurant, tour, event\n"
            "  - Order food, groceries\n"
            "  - Find or use any service-style agent (weather, maps, "
            "EV charging, attractions)\n"
            "  - 'Install', 'add', 'connect', or 'browse' agents\n\n"
            "Pass the user's intent verbatim as task_description "
            "('book a flight from Vegas to Chicago tomorrow', "
            "'order pizza tonight', etc). Returns a ranked list of "
            "matching agents with display_name, agent_id, one_liner, "
            "rating, install_count, and an 'installed' flag.\n\n"
            "After calling, read out the agent names to the user and "
            "ASK WHICH ONE THEY WANT. If only one match returns, "
            "confirm that one. If the user has already installed a "
            "matching agent, prefer it. Once the user picks, call "
            "marketplace_install_agent with that agent's id."
        ),
        properties={
            "task_description": _string(
                "The user's task in their own words. Pass through "
                "verbatim — don't pre-classify or shorten."
            ),
        },
        required=["task_description"],
    ),
    FunctionSchema(
        name="marketplace_install_agent",
        description=(
            "Install an agent from Orchet's marketplace for the current "
            "user. Call this AFTER the user has confirmed which agent "
            "to install (from a prior marketplace_find_agents response). "
            "Pass the agent_id from that response.\n\n"
            "On success: returns ok=true. Tell the user the agent is "
            "installed and continue with their original task.\n"
            "If requires_oauth=true: the agent needs the user to sign "
            "in via the app. Tell the user to open the connections "
            "page in the Orchet app to finish."
        ),
        properties={
            "agent_id": _string("Stable agent_id from a marketplace_find_agents result."),
        },
        required=["agent_id"],
    ),
    FunctionSchema(
        name="gmail_search_messages",
        description="Search the user's Gmail for messages matching a query.",
        properties={
            "query": _string("Search query."),
            "from": _string("Optional sender email or name."),
            "after": _string("Optional lower date bound, ISO-8601 or natural language."),
            "before": _string("Optional upper date bound, ISO-8601 or natural language."),
        },
        required=["query"],
    ),
    FunctionSchema(
        name="gmail_get_message",
        description="Fetch one Gmail message by id.",
        properties={"message_id": _string("Gmail message id to fetch.")},
        required=["message_id"],
    ),
    FunctionSchema(
        name="calendar_list_events",
        description="List calendar events in a time window.",
        properties={
            "start": _string("Start of the window, ISO-8601 or natural language."),
            "end": _string("End of the window, ISO-8601 or natural language."),
            "calendar_id": _string("Optional calendar id."),
        },
        required=["start", "end"],
    ),
    FunctionSchema(
        name="calendar_create_event",
        description="Prepare a calendar event for visual confirmation.",
        properties={
            "title": _string("Event title."),
            "start": _string("Event start time."),
            "end": _string("Event end time."),
            "location": _string("Optional location."),
            "attendees": {
                "type": "array",
                "description": "Optional attendee email addresses.",
                "items": {"type": "string"},
            },
        },
        required=["title", "start", "end"],
    ),
    FunctionSchema(
        name="contacts_search",
        description="Search the user's contacts.",
        properties={"query": _string("Name, email, or organization to search for.")},
        required=["query"],
    ),
    FunctionSchema(
        name="outlook_search_messages",
        description="Search the user's Outlook mail for messages matching a query.",
        properties={
            "query": _string("Search query."),
            "from": _string("Optional sender email or name."),
            "after": _string("Optional lower date bound, ISO-8601 or natural language."),
        },
        required=["query"],
    ),
    FunctionSchema(
        name="ms_calendar_create_event",
        description="Prepare a Microsoft calendar event for visual confirmation.",
        properties={
            "title": _string("Event title."),
            "start": _string("Event start time."),
            "end": _string("Event end time."),
            "location": _string("Optional location."),
            "attendees": {
                "type": "array",
                "description": "Optional attendee email addresses.",
                "items": {"type": "string"},
            },
        },
        required=["title", "start", "end"],
    ),
    FunctionSchema(
        name="spotify_search",
        description="Search Spotify for tracks, albums, artists, or playlists.",
        properties={
            "query": _string("Search query."),
            "type": _string("Spotify item type, such as track, artist, album, or playlist."),
        },
        required=["query"],
    ),
    FunctionSchema(
        name="spotify_play",
        description="Start Spotify playback.",
        properties={
            "uri": _string("Spotify URI to play."),
            "device_id": _string("Optional Spotify device id."),
        },
        required=["uri"],
    ),
    FunctionSchema(
        name="duffel_search_flights",
        description="Search available flights.",
        properties={
            "origin": _string("Origin airport or city."),
            "destination": _string("Destination airport or city."),
            "departure_date": _string("Departure date."),
            "return_date": _string("Optional return date."),
            "passengers": {
                "type": "integer",
                "description": "Number of passengers.",
                "minimum": 1,
            },
            "cabin_class": _string("Optional cabin class."),
        },
        required=["origin", "destination", "departure_date"],
    ),
)

VOICE_TOOLS_SCHEMA = ToolsSchema(standard_tools=list(VOICE_FUNCTION_SCHEMAS))
