"""Kavak Travel Assistant - Streamlit web UI.

Optional per the submission spec, shipped because this is where the
**live agent trace sidebar** lives - the differentiator that lets a
reviewer watch the agent reason in real time: routed intent, extracted
filters, retrieved chunks with relevance scores, prompt versions, and
per-turn cost rollup.

Run with::

    streamlit run streamlit_app.py
"""

from __future__ import annotations

import streamlit as st

from app import __version__
from app.config import get_settings
from app.graph.builder import build_agent, default_substrate
from app.llm.tracing import Tracer
from app.memory.conversation import Conversation
from app.schemas.intent import Intent

st.set_page_config(
    page_title="Kavak Travel Assistant",
    layout="wide",
    initial_sidebar_state="expanded",
)


# Each badge is (label, accent-colour). No emoji - colour + lowercase text
# does the visual work without the noise of decorative glyphs.
_INTENT_BADGE = {
    Intent.FLIGHT_SEARCH: ("flight search", "#0284c7"),
    Intent.POLICY_QA: ("policy", "#7c3aed"),
    Intent.CLARIFY: ("clarify", "#b45309"),
    # Fallback only - the OOS sub-category badges below are preferred.
    # Used when the trace doesn't carry a sub-category (legacy traces or
    # the rare case where the OOS node never emitted an event).
    Intent.OUT_OF_SCOPE: ("off topic", "#475569"),
}

# Sub-badges for the OOS node. The router classifies as OUT_OF_SCOPE; the
# LLM-driven OOS node then sub-classifies into greeting / info / redirect.
# These badges surface that decision in the UI so the user sees what
# actually happened (info-grounded reply vs friendly ack vs decline)
# instead of the unhelpful umbrella "out of scope" label.
_INFO_BADGE = ("info", "#0d9488")  # teal - capability / meta query
_GREETING_BADGE = ("greeting", "#16a34a")  # green - friendly ack
_REDIRECT_BADGE = ("off topic", "#475569")  # gray - declined off-domain


# Light, designer-y page styling. Loaded once.
st.markdown(
    """
    <style>
      .block-container { padding-top: 2rem; max-width: 1100px; }
      h1 { font-weight: 600; letter-spacing: -0.02em; }
      .stChatMessage { border-radius: 10px; }
      .badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.05em;
        text-transform: uppercase;
        color: white;
        margin-right: 8px;
        vertical-align: 2px;
      }
      .runtime {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 0.78rem;
        line-height: 1.55;
        color: #475569;
      }
      .step-title {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 0.78rem;
      }
      .stCaption, [data-testid="stCaptionContainer"] { color: #64748b; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _badge_html(
    intent: Intent,
    *,
    small: bool = False,
    branch: str | None = None,
) -> str:
    """Render a coloured pill badge.

    For OUT_OF_SCOPE we prefer the sub-category badge (greeting / info
    / off topic) so the user sees what the bot actually did instead of
    the unhelpful umbrella "out of scope" label. The 4 LLM router
    intents map to 4 base colours; OOS expands to 3 sub-colours via the
    ``branch`` field read from the trace.
    """
    if intent is Intent.OUT_OF_SCOPE:
        if branch == "greeting":
            label, color = _GREETING_BADGE
        elif branch == "info":
            label, color = _INFO_BADGE
        else:
            # branch == "redirect" or branch is None (legacy / missing trace)
            label, color = _REDIRECT_BADGE
    else:
        label, color = _INTENT_BADGE.get(intent, (intent.value, "#475569"))
    style_size = "font-size: 0.65rem; padding: 1px 8px;" if small else ""
    return (
        f"<span class='badge' style='background:{color};{style_size}'>"
        f"{label}</span>"
    )


def _branch_for(tracer: Tracer | None) -> str | None:
    """Pull the OOS node's sub-category from the trace, if present.

    The LLM-driven OOS node now writes ``category`` (greeting/info/redirect)
    rather than the older ``branch`` field. We read both for backwards
    compatibility with replayed older traces.
    """
    if tracer is None:
        return None
    for ev in reversed(tracer.events):
        if ev.node == "out_of_scope":
            # New schema (v3): "category"; old schema (v2): "branch"
            return ev.output.get("category") or ev.output.get("branch")
    return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def _init_state() -> None:
    if "agent" not in st.session_state:
        sub = default_substrate(self_critique=False)
        st.session_state.agent = build_agent(sub)
    if "conversation" not in st.session_state:
        st.session_state.conversation = Conversation()
    if "messages" not in st.session_state:
        st.session_state.messages = []  # [{role, content, intent, tracer}]
    if "turn_seq" not in st.session_state:
        st.session_state.turn_seq = 0


_init_state()
settings = get_settings()


# ---------------------------------------------------------------------------
# Sidebar - trace inspector + runtime info
# ---------------------------------------------------------------------------


with st.sidebar:
    st.markdown("#### Agent trace")
    st.caption(
        "Live inspector of the agent's reasoning per turn: the routed intent, "
        "the extractor's hidden chain-of-thought, retrieved chunks with "
        "relevance scores, and the prompt version that fired at each step."
    )

    # Find the latest assistant message that has a tracer attached
    last_traced = next(
        (m for m in reversed(st.session_state.messages) if m.get("tracer")),
        None,
    )

    if last_traced is None:
        st.info("Send a message to see the agent's reasoning here.", icon=None)
    else:
        tracer: Tracer = last_traced["tracer"]
        intent: Intent | None = last_traced.get("intent")
        branch = _branch_for(tracer)
        if intent is not None:
            st.markdown(_badge_html(intent, branch=branch), unsafe_allow_html=True)

        # Per-turn rollup
        s = tracer.summary()
        cols = st.columns(3)
        cols[0].metric("Latency", f"{s['total_latency_ms']:.0f} ms")
        cols[1].metric("Tokens", s["total_tokens"])
        cols[2].metric("Cost", f"${s['total_cost_usd']:.4f}")

        # Per-event accordions
        st.markdown("&nbsp;")
        st.markdown("**Reasoning steps**", help="One expander per graph node that ran this turn.")
        for ev in tracer.events:
            title = f"{ev.node}"
            if ev.prompt_id:
                title += f"  ·  {ev.prompt_id}"
            title += f"  ·  {ev.latency_ms:.0f} ms"
            with st.expander(title, expanded=False):
                st.json(ev.output)

    st.divider()
    st.markdown("**Runtime**")
    st.markdown(
        f"<div class='runtime'>"
        f"version&nbsp;&nbsp;{__version__}<br>"
        f"provider&nbsp;{settings.llm_provider}<br>"
        f"model&nbsp;&nbsp;&nbsp;&nbsp;{settings.llm_model}<br>"
        f"seed&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{settings.llm_seed}"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.divider()
    if st.button("Reset conversation", use_container_width=True):
        st.session_state.conversation.reset()
        st.session_state.messages = []
        st.session_state.turn_seq = 0
        st.rerun()


# ---------------------------------------------------------------------------
# Main column - chat
# ---------------------------------------------------------------------------


st.title("Kavak Travel Assistant")
st.caption(
    "Flight search, visa & refund Q&A, and multi-turn refinement - "
    "built on LangGraph with citation-by-construction RAG."
)

# Render existing message history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "assistant" and msg.get("intent") is not None:
            st.markdown(
                _badge_html(
                    msg["intent"],
                    small=True,
                    branch=_branch_for(msg.get("tracer")),
                ),
                unsafe_allow_html=True,
            )
        st.markdown(msg["content"])

# Chat input
prompt = st.chat_input(
    "Ask about flights, visas, or refunds…",
    key="chat_input",
)

if prompt:
    # Render user message immediately
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Run a turn
    st.session_state.turn_seq += 1
    convo: Conversation = st.session_state.conversation
    convo.add_user_message(prompt)
    tracer = Tracer.for_turn(turn_id=f"st-t{st.session_state.turn_seq:03d}")
    try:
        state = st.session_state.agent.invoke(
            {
                "user_message": prompt,
                "summary": convo.summary(),
                "prior_query": convo.prior_query,
                "tracer": tracer,
                "turn_id": tracer.turn_id,
            }
        )
        reply = state.get("final_answer", "(no reply)")
        intent = state.get("intent")
        if state.get("flight_query") is not None:
            convo.commit_query(state["flight_query"])
        convo.add_assistant_message(reply)
    except Exception as exc:  # surface, don't crash UI
        reply = f"Error: {type(exc).__name__}: {exc}"
        intent = None

    # Render assistant reply with intent badge
    st.session_state.messages.append({
        "role": "assistant",
        "content": reply,
        "intent": intent,
        "tracer": tracer,
    })
    with st.chat_message("assistant"):
        if intent is not None:
            st.markdown(
                _badge_html(intent, small=True, branch=_branch_for(tracer)),
                unsafe_allow_html=True,
            )
        st.markdown(reply)
    st.rerun()
