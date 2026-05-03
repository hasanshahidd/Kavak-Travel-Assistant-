"""Kavak Travel Assistant - CLI entry point.

Spec-required entry. Two modes:

* ``python main.py``                interactive REPL with rich formatting.
* ``python main.py --demo``         scripted multi-turn demo, then exit.

The CLI is intentionally a thin wrapper around :func:`app.graph.builder.build_agent`
+ :class:`app.memory.conversation.Conversation`; everything visible here
exists so a reviewer running the project locally has a polished demo
without needing to touch the web UI.

Special commands inside the REPL:
    /trace      Show the trace events from the most recent turn
    /reset      Clear the conversation memory
    /quit       Exit
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app import __version__
from app.config import get_settings
from app.graph.builder import build_agent, default_substrate
from app.llm.tracing import Tracer
from app.memory.conversation import Conversation
from app.schemas.intent import Intent

if TYPE_CHECKING:
    from app.graph.state import AgentState

console = Console()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


_INTENT_COLOR = {
    Intent.FLIGHT_SEARCH: "cyan",
    Intent.POLICY_QA: "magenta",
    Intent.CLARIFY: "yellow",
    Intent.OUT_OF_SCOPE: "red",
}


def _intent_badge(intent: Intent | None) -> Text:
    if intent is None:
        return Text("(unknown)", style="dim")
    return Text(intent.value, style=f"bold {_INTENT_COLOR.get(intent, 'white')}")


def _render_reply(state: AgentState) -> None:
    intent = state.get("intent")
    final = state.get("final_answer", "(no reply)")
    badge = _intent_badge(intent)
    console.print()
    console.print(Panel(
        Markdown(final),
        title=Text("assistant · ", style="dim").append(badge),
        border_style="dim",
        padding=(1, 2),
    ))


def _render_trace(tracer: Tracer) -> None:
    if not tracer.events:
        console.print("[dim]No trace events for the last turn.[/dim]")
        return
    table = Table(title="Trace · most recent turn", title_style="bold", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("node")
    table.add_column("prompt", style="cyan")
    table.add_column("ms", justify="right")
    table.add_column("tok in", justify="right", style="dim")
    table.add_column("tok out", justify="right", style="dim")
    table.add_column("$", justify="right", style="green")
    for i, ev in enumerate(tracer.events, start=1):
        table.add_row(
            str(i),
            ev.node,
            ev.prompt_id or "-",
            f"{ev.latency_ms:.0f}",
            str(ev.tokens_in),
            str(ev.tokens_out),
            f"{ev.cost_usd:.4f}",
        )
    summary = tracer.summary()
    console.print()
    console.print(table)
    console.print(
        f"  Total: [bold]{summary['total_latency_ms']:.0f} ms[/bold] · "
        f"[bold]{summary['total_tokens']} tokens[/bold] · "
        f"[bold green]${summary['total_cost_usd']:.4f}[/bold green]"
    )


def _print_banner() -> None:
    settings = get_settings()
    console.print()
    console.print(Panel(
        Text.from_markup(
            f"[bold]Kavak Travel Assistant[/bold]  v{__version__}\n"
            f"[dim]provider: {settings.llm_provider} · model: {settings.llm_model} · "
            f"seed: {settings.llm_seed}[/dim]\n\n"
            "Ask about [cyan]flights[/cyan], [magenta]visa rules[/magenta], or "
            "[magenta]refund policies[/magenta].\n"
            "Slash commands: [yellow]/trace[/yellow]  [yellow]/reset[/yellow]  [yellow]/quit[/yellow]"
        ),
        border_style="cyan",
    ))
    console.print()


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------


def _run_turn(message: str, agent, conversation: Conversation, turn_seq: int) -> Tracer:
    tracer = Tracer.for_turn(turn_id=f"cli-t{turn_seq:03d}")
    conversation.add_user_message(message)
    state = agent.invoke(
        {
            "user_message": message,
            "summary": conversation.summary(),
            "prior_query": conversation.prior_query,
            "tracer": tracer,
            "turn_id": tracer.turn_id,
        }
    )
    final = state.get("final_answer", "(the agent produced no reply)")
    conversation.add_assistant_message(final)
    if state.get("flight_query") is not None:
        conversation.commit_query(state["flight_query"])
    _render_reply(state)
    return tracer


# ---------------------------------------------------------------------------
# REPL + demo
# ---------------------------------------------------------------------------


_DEMO_TURNS: tuple[str, ...] = (
    "Round-trip from Dubai to Tokyo in August, Star Alliance only, no overnight layovers",
    "actually move it to September",
    "now show me flights to Paris",
    "Do UAE passport holders need a visa for Japan?",
    "what's the weather in Tokyo right now?",
)


def run_demo(agent) -> None:
    convo = Conversation()
    last_tracer: Tracer | None = None
    for i, msg in enumerate(_DEMO_TURNS, start=1):
        console.rule(f"[bold]Turn {i}[/bold]", style="dim")
        console.print(f"[bold]you:[/bold] {msg}")
        last_tracer = _run_turn(msg, agent, convo, turn_seq=i)
        time.sleep(0.05)  # tiny delay so the rules are easier to scan
    console.rule("[bold]Demo trace (last turn)[/bold]", style="dim")
    if last_tracer is not None:
        _render_trace(last_tracer)


def run_repl(agent) -> None:
    convo = Conversation()
    turn_seq = 0
    last_tracer: Tracer | None = None
    while True:
        try:
            line = console.input("[bold cyan]you ›[/bold cyan] ").strip()  # noqa: RUF001 - typographic right-pointing arrow used as prompt glyph
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]goodbye.[/dim]")
            return
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            console.print("[dim]goodbye.[/dim]")
            return
        if line == "/reset":
            convo.reset()
            console.print("[dim]conversation reset.[/dim]")
            continue
        if line == "/trace":
            if last_tracer is None:
                console.print("[dim]no turn yet.[/dim]")
            else:
                _render_trace(last_tracer)
            continue
        turn_seq += 1
        try:
            last_tracer = _run_turn(line, agent, convo, turn_seq)
        except Exception as exc:  # surface, don't crash the REPL
            console.print(f"[red]error:[/red] {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kavak-travel-assistant",
        description="Conversational travel assistant - flights, visas, refunds.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a scripted 5-turn demo conversation and exit.",
    )
    parser.add_argument(
        "--self-critique",
        action="store_true",
        help="Enable the responder's self-critique loop (extra LLM call per turn).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kavak-travel-assistant {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _print_banner()
    sub = default_substrate(self_critique=args.self_critique)
    agent = build_agent(sub)

    if args.demo:
        run_demo(agent)
    else:
        run_repl(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
