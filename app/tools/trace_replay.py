"""Trace replay CLI - pretty-print any saved JSONL trace.

The tracer writes one ``{turn_id}.jsonl`` file per turn under ``.traces/``.
This tool is the human-friendly reader. It's the operational-maturity
signal most candidates skip: logs nobody can read are debt, not data.

Usage::

    python -m app.tools.trace_replay <path-to-trace.jsonl>
    python -m app.tools.trace_replay --latest        # pick the most recent
    python -m app.tools.trace_replay --list          # list all traces
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.config import get_settings
from app.llm.tracing import read_trace

console = Console()


def _list_traces(trace_dir: Path) -> list[Path]:
    return sorted(trace_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def _format_path(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _render_trace(path: Path) -> int:
    if not path.exists():
        console.print(f"[red]not found:[/red] {path}")
        return 2
    events = read_trace(path)
    if not events:
        console.print(f"[yellow]empty trace:[/yellow] {path}")
        return 0

    turn_id = events[0].turn_id
    console.print()
    console.print(Panel(
        Text.from_markup(
            f"[bold]turn:[/bold] {turn_id}\n"
            f"[dim]{path}[/dim]\n"
            f"[dim]{len(events)} node events[/dim]"
        ),
        border_style="cyan",
    ))

    table = Table(show_lines=False, header_style="bold dim")
    table.add_column("#", justify="right", style="dim")
    table.add_column("node")
    table.add_column("prompt", style="cyan")
    table.add_column("hash", style="dim")
    table.add_column("ms", justify="right")
    table.add_column("tok in", justify="right", style="dim")
    table.add_column("tok out", justify="right", style="dim")
    table.add_column("$", justify="right", style="green")
    total_latency = 0.0
    total_cost = 0.0
    total_tokens = 0
    for i, ev in enumerate(events, start=1):
        table.add_row(
            str(i),
            ev.node,
            ev.prompt_id or "-",
            (ev.prompt_hash or "-")[:8],
            f"{ev.latency_ms:.0f}",
            str(ev.tokens_in),
            str(ev.tokens_out),
            f"{ev.cost_usd:.4f}",
        )
        total_latency += ev.latency_ms
        total_cost += ev.cost_usd
        total_tokens += ev.tokens_in + ev.tokens_out

    console.print(table)
    console.print(
        f"  [bold]Total:[/bold] {total_latency:.0f} ms · "
        f"{total_tokens} tokens · "
        f"[green]${total_cost:.4f}[/green]"
    )
    console.print()

    # Per-event detail panels
    for i, ev in enumerate(events, start=1):
        console.print(
            Panel(
                _format_output(ev.output),
                title=f"[bold]{i}. {ev.node}[/bold]"
                + (f"  ·  [cyan]{ev.prompt_id}[/cyan]" if ev.prompt_id else ""),
                border_style="dim",
            )
        )
    return 0


def _format_output(output: dict) -> str:
    """Compact YAML-ish rendering, friendlier than raw JSON for humans."""
    lines: list[str] = []

    def _walk(value, indent: int) -> None:
        prefix = "  " * indent
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, (dict, list)) and v:
                    lines.append(f"{prefix}{k}:")
                    _walk(v, indent + 1)
                else:
                    lines.append(f"{prefix}{k}: {_short(v)}")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{prefix}[]")
            else:
                for item in value:
                    if isinstance(item, (dict, list)):
                        lines.append(f"{prefix}-")
                        _walk(item, indent + 1)
                    else:
                        lines.append(f"{prefix}- {_short(item)}")
        else:
            lines.append(f"{prefix}{_short(value)}")

    _walk(output, 0)
    return "\n".join(lines) if lines else "(no output)"


def _short(v) -> str:
    s = str(v)
    if len(s) > 240:
        s = s[:240] + "…"
    return s


def main() -> int:
    parser = argparse.ArgumentParser(description="Pretty-print a Kavak trace JSONL.")
    parser.add_argument("path", nargs="?", help="Path to a trace .jsonl file")
    parser.add_argument("--latest", action="store_true", help="Open the most recent trace.")
    parser.add_argument("--list", action="store_true", help="List all available traces.")
    args = parser.parse_args()

    trace_root = get_settings().trace_dir

    if args.list:
        traces = _list_traces(trace_root)
        if not traces:
            console.print(f"[dim]no traces under {trace_root}[/dim]")
            return 0
        table = Table(title=f"Traces under {trace_root}", show_lines=False)
        table.add_column("path")
        table.add_column("size", justify="right", style="dim")
        for t in traces[:50]:
            table.add_row(_format_path(t, trace_root), f"{t.stat().st_size}B")
        console.print(table)
        return 0

    if args.latest:
        traces = _list_traces(trace_root)
        if not traces:
            console.print(f"[dim]no traces under {trace_root}[/dim]")
            return 0
        return _render_trace(traces[0])

    if not args.path:
        parser.print_help()
        return 1
    return _render_trace(Path(args.path))


if __name__ == "__main__":
    sys.exit(main())
