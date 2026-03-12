#!/usr/bin/env python3
"""
CLI utility for managing the Bot Manager server.

Usage:
    python -m bot_manager.cli status
    python -m bot_manager.cli workers
    python -m bot_manager.cli start  <user_id>
    python -m bot_manager.cli stop   <user_id>
    python -m bot_manager.cli restart <user_id>
    python -m bot_manager.cli close  <user_id>
    python -m bot_manager.cli info   <user_id>
    python -m bot_manager.cli logs   <user_id> [--limit 50]
    python -m bot_manager.cli shutdown
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import click
import requests

DEFAULT_URL = os.getenv("MANAGER_URL", "http://127.0.0.1:6800")
DEFAULT_KEY = os.getenv("MANAGER_SECRET", "your-secret-key-here")


class ManagerClient:
    def __init__(self, base_url: str, secret: str) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {"X-Manager-Key": secret}

    def get(self, path: str, **kwargs) -> dict:
        r = requests.get(f"{self.base}{path}", headers=self.headers, params=kwargs, timeout=15)
        return self._handle(r)

    def post(self, path: str) -> dict:
        r = requests.post(f"{self.base}{path}", headers=self.headers, timeout=30)
        return self._handle(r)

    def _handle(self, r: requests.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            click.echo(f"HTTP {r.status_code}: {r.text}", err=True)
            sys.exit(1)
        if not data.get("ok"):
            click.echo(f"ERROR: {data.get('error', 'unknown')}", err=True)
            sys.exit(1)
        return data["data"]


def _pp(obj, title: str = "") -> None:
    if title:
        click.secho(f"\n  {title}", fg="cyan", bold=True)
        click.echo("  " + "─" * 50)
    if isinstance(obj, list):
        for item in obj:
            _pp_dict(item)
            click.echo()
    elif isinstance(obj, dict):
        _pp_dict(obj)
    else:
        click.echo(f"  {obj}")


def _pp_dict(d: dict, indent: int = 4) -> None:
    for k, v in d.items():
        if isinstance(v, dict):
            click.echo(f"{' ' * indent}{click.style(k, fg='yellow')}:")
            _pp_dict(v, indent + 4)
        else:
            click.echo(f"{' ' * indent}{click.style(k, fg='yellow')}: {v}")


# -----------------------------------------------------------------------
# CLI group
# -----------------------------------------------------------------------

@click.group()
@click.option("--url", default=DEFAULT_URL, envvar="MANAGER_URL", help="Manager server URL")
@click.option("--key", default=DEFAULT_KEY, envvar="MANAGER_SECRET", help="Manager API key")
@click.pass_context
def cli(ctx, url, key):
    """PairTrading Bot Manager — console interface."""
    ctx.ensure_object(dict)
    ctx.obj["client"] = ManagerClient(url, key)


@cli.command()
@click.pass_context
def status(ctx):
    """Show manager health and overview."""
    data = ctx.obj["client"].get("/api/health")
    _pp(data, "Manager Status")


@cli.command()
@click.pass_context
def workers(ctx):
    """List all active workers."""
    data = ctx.obj["client"].get("/api/workers")
    if not data:
        click.echo("  Нет активных воркеров")
        return
    click.secho(f"\n  Активных воркеров: {len(data)}", fg="cyan", bold=True)
    click.echo("  " + "─" * 50)
    for w in data:
        alive = "✓" if w.get("alive") else "✗"
        color = "green" if w.get("alive") else "red"
        uptime = int(w.get("uptime_seconds", 0))
        m, s = divmod(uptime, 60)
        h, m = divmod(m, 60)
        click.echo(
            f"  [{click.style(alive, fg=color)}] "
            f"user_id={w['user_id']}  "
            f"pid={w.get('pid', '?')}  "
            f"uptime={h:02d}:{m:02d}:{s:02d}  "
            f"state={w.get('actual_state', '?')}  "
            f"spread={w.get('current_spread_pct', '-')}  "
            f"pnl={w.get('pnl_total_pct', '-')}  "
            f"pos={'OPEN' if w.get('position_open') else 'closed'}"
        )


@cli.command()
@click.argument("user_id", type=int)
@click.pass_context
def start(ctx, user_id):
    """Start a bot for USER_ID."""
    data = ctx.obj["client"].post(f"/api/workers/{user_id}/start")
    click.secho(f"  → {data.get('status', '?')}  pid={data.get('pid', '?')}", fg="green")


@cli.command()
@click.argument("user_id", type=int)
@click.pass_context
def stop(ctx, user_id):
    """Stop a bot for USER_ID."""
    data = ctx.obj["client"].post(f"/api/workers/{user_id}/stop")
    click.secho(f"  → {data.get('status', '?')}", fg="yellow")


@cli.command()
@click.argument("user_id", type=int)
@click.pass_context
def restart(ctx, user_id):
    """Restart a bot for USER_ID."""
    data = ctx.obj["client"].post(f"/api/workers/{user_id}/restart")
    click.secho(f"  → {data.get('status', '?')}  pid={data.get('pid', '?')}", fg="green")


@cli.command("close")
@click.argument("user_id", type=int)
@click.pass_context
def close_positions(ctx, user_id):
    """Close all positions for USER_ID's bot."""
    data = ctx.obj["client"].post(f"/api/workers/{user_id}/close-positions")
    click.secho(f"  → {data.get('status', '?')}", fg="yellow")


@cli.command()
@click.argument("user_id", type=int)
@click.pass_context
def info(ctx, user_id):
    """Show detailed info about a specific worker."""
    data = ctx.obj["client"].get(f"/api/workers/{user_id}")
    _pp(data, f"Worker user_id={user_id}")


@cli.command()
@click.argument("user_id", type=int)
@click.option("--limit", default=20, help="Number of log entries")
@click.pass_context
def logs(ctx, user_id, limit):
    """Show recent event logs for USER_ID."""
    data = ctx.obj["client"].get(f"/api/logs/{user_id}", limit=limit)
    if not data:
        click.echo("  Логов нет")
        return
    click.secho(f"\n  Последние {len(data)} событий (user_id={user_id})", fg="cyan", bold=True)
    click.echo("  " + "─" * 60)
    for e in data:
        lvl = e.get("level", "info")
        colors = {"info": "white", "trade": "green", "warning": "yellow", "error": "red"}
        ts = e.get("created_at", "")
        msg = e.get("message", "")
        click.echo(f"  {ts}  [{click.style(lvl.upper(), fg=colors.get(lvl, 'white'))}]  {msg}")


@cli.command("shutdown")
@click.confirmation_option(prompt="Точно остановить менеджер и все боты?")
@click.pass_context
def shutdown_manager(ctx):
    """Gracefully shut down the Manager and all workers."""
    data = ctx.obj["client"].post("/api/shutdown")
    click.secho(f"  → {data.get('status', '?')}", fg="red")


if __name__ == "__main__":
    cli()
