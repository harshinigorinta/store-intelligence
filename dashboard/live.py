"""
dashboard/live.py
Live terminal dashboard — polls the API every 2 seconds and displays
real-time store metrics using the rich library.
"""

import time
import json
import urllib.request
from datetime import datetime

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.columns import Columns
    from rich import box
except ImportError:
    print("Installing rich...")
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "rich"])
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.columns import Columns
    from rich import box

API_BASE = "http://localhost:8000"
STORE_ID = "ST1008"
REFRESH_SECONDS = 2

console = Console()


def fetch(path: str) -> dict:
    try:
        req = urllib.request.Request(f"{API_BASE}{path}")
        resp = urllib.request.urlopen(req, timeout=3)
        return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def make_dashboard() -> Panel:
    metrics = fetch(f"/stores/{STORE_ID}/metrics")
    funnel  = fetch(f"/stores/{STORE_ID}/funnel")
    anomalies = fetch(f"/stores/{STORE_ID}/anomalies")
    health  = fetch("/health")

    now = datetime.now().strftime("%H:%M:%S")

    # ── Metrics panel ──────────────────────────────────────────────────────────
    m_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    m_table.add_column("Metric", style="cyan")
    m_table.add_column("Value", style="bold green")

    if "error" not in metrics:
        m_table.add_row("Unique Visitors",   str(metrics.get("unique_visitors", 0)))
        m_table.add_row("Conversion Rate",   f"{metrics.get('conversion_rate', 0)*100:.1f}%")
        m_table.add_row("Queue Depth",       str(metrics.get("current_queue_depth", 0)))
        m_table.add_row("Abandonment Rate",  f"{metrics.get('abandonment_rate', 0)*100:.1f}%")
        m_table.add_row("Billing Visitors",  str(metrics.get("billing_zone_visitors", 0)))
    else:
        m_table.add_row("Status", "[red]API unavailable[/red]")

    metrics_panel = Panel(m_table, title="[bold]Store Metrics[/bold]", border_style="green")

    # ── Funnel panel ───────────────────────────────────────────────────────────
    f_table = Table(box=box.SIMPLE, show_header=True, padding=(0, 2))
    f_table.add_column("Stage",     style="cyan")
    f_table.add_column("Visitors",  style="bold white")
    f_table.add_column("Drop-off",  style="yellow")

    if "funnel" in funnel:
        for stage in funnel["funnel"]:
            drop = f"{stage['drop_off_pct']}%" if stage['drop_off_pct'] > 0 else "-"
            f_table.add_row(stage["stage"], str(stage["visitors"]), drop)

    funnel_panel = Panel(f_table, title="[bold]Conversion Funnel[/bold]", border_style="blue")

    # ── Anomalies panel ────────────────────────────────────────────────────────
    a_table = Table(box=box.SIMPLE, show_header=True, padding=(0, 2))
    a_table.add_column("Type",     style="cyan")
    a_table.add_column("Severity", style="bold")
    a_table.add_column("Detail",   style="white")

    if "anomalies" in anomalies and anomalies["anomalies"]:
        for a in anomalies["anomalies"]:
            sev = a["severity"]
            colour = "red" if sev == "CRITICAL" else "yellow" if sev == "WARN" else "blue"
            a_table.add_row(a["type"], f"[{colour}]{sev}[/{colour}]", a["detail"][:50])
    else:
        a_table.add_row("None", "[green]OK[/green]", "No active anomalies")

    anomaly_panel = Panel(a_table, title="[bold]Anomalies[/bold]", border_style="red")

    # ── Health panel ───────────────────────────────────────────────────────────
    h_status = health.get("status", "UNKNOWN")
    h_colour = "green" if h_status == "OK" else "yellow"
    h_total  = health.get("total_events_ingested", 0)
    health_panel = Panel(
        f"[{h_colour}]Status: {h_status}[/{h_colour}]\n"
        f"Total events: {h_total}\n"
        f"Updated: {now}",
        title="[bold]Health[/bold]",
        border_style="magenta"
    )

    # Compose layout
    top_row = Columns([metrics_panel, funnel_panel], equal=True)
    bottom_row = Columns([anomaly_panel, health_panel], equal=True)

    from rich.console import Group
    return Panel(
        Group(top_row, bottom_row),
        title=f"[bold yellow]Store Intelligence Dashboard — {STORE_ID}[/bold yellow]",
        border_style="yellow",
    )


def main():
    console.print("\n[bold yellow]Store Intelligence Live Dashboard[/bold yellow]")
    console.print(f"Monitoring: [cyan]{STORE_ID}[/cyan] | Refresh: {REFRESH_SECONDS}s | Ctrl+C to exit\n")

    with Live(make_dashboard(), refresh_per_second=1, screen=True) as live:
        while True:
            time.sleep(REFRESH_SECONDS)
            live.update(make_dashboard())


if __name__ == "__main__":
    main()