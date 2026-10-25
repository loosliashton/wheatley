"""Wheatley Command Line Interface (CLI)"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from wheatley.audit import AuditLogger
from wheatley.budget import BudgetTracker
from wheatley.chat.telegram import TelegramBot
from wheatley.checks.engine import CheckEngine
from wheatley.config import WheatleyConfig, load_config
from wheatley.llm import LLMAgent, UsageTracker
from wheatley.webhook import WebhookServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wheatley")


class WheatleyApp:
    def __init__(self, config_path: str, secrets_path: str):
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.config: WheatleyConfig = load_config(config_path, secrets_path)
        self.audit = AuditLogger()
        self.usage = UsageTracker(self.audit)
        self.budget = BudgetTracker(self.config.budget, self.audit)
        self.bot: TelegramBot | None = None
        self.check_engine: CheckEngine | None = None
        self.llm_agent: LLMAgent | None = None
        self.gemini_agent: LLMAgent | None = None
        self.webhook_server: WebhookServer | None = None
        self._stop_event = asyncio.Event()

    async def get_status_overview(self) -> str:
        from wheatley.tools.proxmox import ProxmoxClient

        lines = ["🤖 *Wheatley System Status*\n"]

        for node_cfg in self.config.nodes:
            # Check if token is placeholder
            token_val = node_cfg.tokens.monitor or ""
            if "xxxx" in token_val or not token_val:
                lines.append(f"⚪️ *Node: {node_cfg.name}*\n   ⚠️ Token unconfigured in secrets.env\n")
                continue

            try:
                client = ProxmoxClient(node_cfg)
                loop = asyncio.get_running_loop()
                status = await loop.run_in_executor(None, client.get_node_status)
                guests = await loop.run_in_executor(None, client.get_guests)

                cpu_pct = round(status.get("cpu", 0) * 100, 1)
                mem_data = status.get("memory", {})
                mem_used_gb = round(mem_data.get("used", 0) / (1024**3), 1)
                mem_total_gb = round(mem_data.get("total", 0) / (1024**3), 1)
                mem_pct = round((mem_used_gb / mem_total_gb) * 100, 1) if mem_total_gb else 0

                uptime_s = int(status.get("uptime", 0))
                days, rem = divmod(uptime_s, 86400)
                hours, mins = divmod(rem, 3600)
                uptime_str = f"{days}d {hours}h {mins//60}m" if days else f"{hours}h {mins//60}m"

                running_guests = [g for g in guests if g.get("status") == "running"]
                stopped_guests = [g for g in guests if g.get("status") == "stopped"]

                lines.append(
                    f"🟢 *Node: {node_cfg.name}*\n"
                    f"   • *CPU:* `{cpu_pct}%` | *RAM:* `{mem_used_gb}/{mem_total_gb} GB ({mem_pct}%)`\n"
                    f"   • *Uptime:* `{uptime_str}`\n"
                    f"   • *Guests:* `{len(running_guests)} running`, `{len(stopped_guests)} stopped`"
                )

                # Show running guests
                if running_guests:
                    guest_bullets = []
                    for g in running_guests:
                        vmid = g.get("vmid")
                        name = g.get("name", "unnamed")
                        gtype = "CT" if g.get("type") == "lxc" else "VM"
                        guest_bullets.append(f"`{gtype} {vmid} ({name})`")
                    lines.append(f"   • *Active:* {', '.join(guest_bullets)}")
                lines.append("")

            except Exception as e:
                logger.exception("Failed to query status for node %s", node_cfg.name)
                lines.append(f"🔴 *Node: {node_cfg.name}*\n   ❌ Error querying API: `{e}`\n")

        lines.extend([
            "📊 *Monitoring & Controls*",
            f"• *Checks Active:* `{len([c for c in self.config.checks if c.enabled])}`",
            f"• *Self-Protection:* `{self.config.self_guest or 'disabled'}`",
            f"• *LLM:* `{self.config.llm.provider}` (`{self.config.llm.model}`)",
        ])
        return "\n".join(lines)

    async def run(self) -> None:
        import fcntl
        lock_path = Path("/var/lib/wheatley/wheatley.lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._lock_file = open(lock_path, "w")
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            print("❌ Error: Another Wheatley daemon is already running!", file=sys.stderr)
            print("   (The systemd service 'wheatley.service' may be active. Use 'systemctl restart wheatley' or 'systemctl stop wheatley' first.)", file=sys.stderr)
            sys.exit(1)

        logger.info("Starting Wheatley daemon...")
        self.audit.log_event("SYSTEM", "daemon", "startup", "SUCCESS")

        # 1. Initialize Telegram bot if configured
        if self.config.chat.telegram:
            self.bot = TelegramBot(
                config=self.config.chat.telegram,
                audit=self.audit,
                status_provider=self.get_status_overview,
                discovery_provider=self.run_discovery_summary,
            )

        alert_sink = self.bot.send_alert if self.bot else None

        # 2. Initialize and start Check Engine
        self.check_engine = CheckEngine(
            config=self.config,
            audit=self.audit,
            alert_sink=alert_sink,
        )
        self.check_engine.start()
        logger.info("Check Engine active with %d checks.", len(self.check_engine.checks))

        # 3. Initialize LLM Agent
        self.llm_agent = LLMAgent(
            config=self.config,
            audit=self.audit,
            usage=self.usage,
            check_engine=self.check_engine,
            proxmox_clients=self.check_engine.clients,
        )
        self.gemini_agent = self.llm_agent  # Backwards compatibility

        if self.bot:
            self.bot.check_engine = self.check_engine
            self.bot.agent = self.llm_agent
            self.bot.llm_agent = self.llm_agent
            self.bot.gemini_agent = self.llm_agent
            self.bot.budget = self.usage

        # 4. Initialize and start Webhook receiver
        if self.config.webhook and self.config.webhook.enabled:
            self.webhook_server = WebhookServer(
                config=self.config.webhook,
                audit=self.audit,
                alert_sink=alert_sink,
            )
            await self.webhook_server.start()

        # 5. Start Telegram bot
        if self.bot:
            await self.bot.start()
            logger.info("Telegram interface active.")
        else:
            logger.warning("No chat backend configured in chat section!")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                pass

        logger.info("Wheatley is running. Press Ctrl+C or send SIGTERM to terminate.")
        await self._stop_event.wait()

        logger.info("Shutting down Wheatley...")
        if self.webhook_server:
            await self.webhook_server.stop()
        if self.check_engine:
            self.check_engine.stop()
        if self.bot:
            await self.bot.stop()
        self.audit.log_event("SYSTEM", "daemon", "shutdown", "SUCCESS")
        logger.info("Wheatley stopped cleanly.")

    async def run_discovery_summary(self) -> str:
        """Run inventory discovery and return formatted markdown."""
        from wheatley.discovery.discover import InventoryDiscovery
        discovery = InventoryDiscovery(self.config)
        guests = await discovery.scan_all()
        return discovery.format_summary_markdown(guests)


def cmd_validate(args: argparse.Namespace) -> None:
    try:
        cfg = load_config(args.config, args.secrets)
        print(f"✅ Configuration in '{args.config}' is valid!")
        print(f"   Nodes configured: {len(cfg.nodes)}")
        for n in cfg.nodes:
            print(f"     - {n.name} ({n.api_url})")
        print(f"   Checks configured: {len(cfg.checks)}")
        print(f"   Telegram configured: {'yes' if cfg.chat.telegram else 'no'}")
        if cfg.chat.telegram:
            print(f"     Allowed User IDs: {cfg.chat.telegram.allowed_user_ids}")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Configuration error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_discover(args: argparse.Namespace) -> None:
    from wheatley.discovery.discover import InventoryDiscovery
    import json
    import yaml

    try:
        cfg = load_config(args.config, args.secrets)
        print("🔍 Scanning Proxmox nodes and probing guests for active services...")
        discovery = InventoryDiscovery(cfg)
        guests = asyncio.run(discovery.scan_all())

        if getattr(args, "json", False):
            out = [g.model_dump() for g in guests]
            print(json.dumps(out, indent=2))
            return

        if getattr(args, "output", None):
            draft = discovery.generate_draft_config(guests)
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(draft, f, sort_keys=False, default_flow_style=False)
            print(f"✅ Recommended draft configuration written to: {out_path}\n")

        print(discovery.format_summary_markdown(guests))
        sys.exit(0)
    except Exception as e:
        print(f"❌ Discovery error: {e}", file=sys.stderr)
        logger.exception("Discovery failed: %s", e)
        sys.exit(1)


def cmd_audit(args: argparse.Namespace) -> None:
    db_path = args.db or "/var/lib/wheatley/wheatley.db"
    audit = AuditLogger(db_path)
    logs = audit.get_recent_logs(limit=args.limit)
    if not logs:
        print("No audit logs found.")
        return
    print(f"=== Last {len(logs)} Audit Events ===")
    for row in logs:
        ts = row["timestamp"][:19]
        print(f"[{ts}] [{row['event_type']}] {row['action']} ({row['status']}) - {row['source']}")
        if row.get("details"):
            print(f"       Details: {row['details']}")


def cmd_check(args: argparse.Namespace) -> None:
    from wheatley.checks.engine import CheckEngine
    from wheatley.checks.base import CheckStatus

    try:
        cfg = load_config(args.config, args.secrets)
        audit = AuditLogger(getattr(args, "db", "/var/lib/wheatley/wheatley.db"))
        engine = CheckEngine(cfg, audit)

        subcmd = getattr(args, "check_subcommand", "list") or "list"

        if subcmd == "list":
            states = engine.get_check_states()
            print(f"=== Configured Checks ({len(states)}) ===")
            for s in states:
                chk = engine.checks.get(s["id"])
                interval = chk.config.interval_seconds if chk else 60
                print(f"• [{s['type']}] {s['id']} (interval: {interval}s)")
                if chk and chk.params:
                    print(f"    Params: {chk.params}")
            return

        elif subcmd == "run":
            check_id = args.check_id
            if check_id not in engine.checks:
                print(f"❌ Check '{check_id}' not found in configuration.", file=sys.stderr)
                sys.exit(1)

            print(f"Executing check '{check_id}'...")
            res = asyncio.run(engine.execute_check(check_id))
            status_icon = "🟢" if res.status == CheckStatus.OK else ("⚠️" if res.status == CheckStatus.WARN else "🚨")
            print(f"{status_icon} Status: {res.status.value}")
            print(f"Summary: {res.summary}")
            if res.metrics:
                print(f"Metrics: {res.metrics}")
            if res.details:
                print(f"Details: {res.details}")
            sys.exit(0 if res.is_healthy else 1)

        elif subcmd == "run-all":
            print(f"Executing all {len(engine.checks)} configured checks...\n")

            async def _run_all():
                results = []
                for cid in engine.checks:
                    r = await engine.execute_check(cid)
                    results.append(r)
                return results

            results = asyncio.run(_run_all())
            print(f"{'ID':<20} {'STATUS':<8} {'SUMMARY'}")
            print("-" * 70)
            has_failure = False
            for r in results:
                if not r.is_healthy:
                    has_failure = True
                status_icon = "🟢" if r.status == CheckStatus.OK else ("⚠️" if r.status == CheckStatus.WARN else "🚨")
                print(f"{r.check_id:<20} {status_icon} {r.status.value:<6} {r.summary}")
            sys.exit(1 if has_failure else 0)

    except Exception as e:
        print(f"❌ Check command error: {e}", file=sys.stderr)
        logger.exception("Check command failed: %s", e)
        sys.exit(1)


def cmd_usage(args: argparse.Namespace) -> None:
    try:
        cfg = load_config(args.config, args.secrets)
        audit = AuditLogger(getattr(args, "db", "/var/lib/wheatley/wheatley.db"))
        usage = UsageTracker(audit)
        print(usage.format_usage_markdown(provider=cfg.llm.provider, model=cfg.llm.model))
    except Exception as e:
        print(f"❌ Usage command error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_budget(args: argparse.Namespace) -> None:
    # Retained as backwards-compatible alias to usage
    cmd_usage(args)


def cmd_ask(args: argparse.Namespace) -> None:
    try:
        cfg = load_config(args.config, args.secrets)
        audit = AuditLogger(getattr(args, "db", "/var/lib/wheatley/wheatley.db"))
        usage = UsageTracker(audit)
        check_engine = CheckEngine(cfg, audit)

        agent = LLMAgent(
            config=cfg,
            audit=audit,
            usage=usage,
            check_engine=check_engine,
            proxmox_clients=check_engine.clients,
        )

        if not agent.is_configured:
            print(
                f"❌ LLM provider '{agent.provider_name}' is not configured. "
                "Update 'llm:' in config.yaml and credentials in secrets.env.",
                file=sys.stderr,
            )
            sys.exit(1)

        question = " ".join(args.question)
        print(f"🤖 Wheatley thinking with {agent.provider_name} ({agent.model_name})...\n")
        response = asyncio.run(agent.chat(question))
        print(response)
    except Exception as e:
        print(f"❌ Ask error: {e}", file=sys.stderr)
        logger.exception("Ask failed: %s", e)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wheatley - Homelab Monitoring & AI Agent")
    subparsers = parser.add_subparsers(dest="command")

    # Run
    p_run = subparsers.add_parser("run", help="Run the Wheatley monitoring agent daemon")
    p_run.add_argument("--config", "-c", default="/etc/wheatley/config.yaml", help="Path to config.yaml")
    p_run.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env", help="Path to secrets.env")

    # Ask
    p_ask = subparsers.add_parser("ask", help="Ask Wheatley AI a homelab question directly")
    p_ask.add_argument("--config", "-c", default="/etc/wheatley/config.yaml")
    p_ask.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env")
    p_ask.add_argument("--db", default="/var/lib/wheatley/wheatley.db")
    p_ask.add_argument("question", nargs="+", help="Question to ask Wheatley")

    # Usage
    p_use = subparsers.add_parser("usage", help="Show LLM token consumption and activity")
    p_use.add_argument("--config", "-c", default="/etc/wheatley/config.yaml")
    p_use.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env")
    p_use.add_argument("--db", default="/var/lib/wheatley/wheatley.db")

    # Check
    p_chk = subparsers.add_parser("check", help="Check engine utilities")
    p_chk.add_argument("--config", "-c", default="/etc/wheatley/config.yaml", help="Path to config.yaml")
    p_chk.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env", help="Path to secrets.env")
    p_chk.add_argument("--db", default="/var/lib/wheatley/wheatley.db", help="Path to audit DB")
    p_chk_sub = p_chk.add_subparsers(dest="check_subcommand")
    p_chk_sub.add_parser("list", help="List all configured health checks")
    p_chk_run = p_chk_sub.add_parser("run", help="Run a specific check immediately")
    p_chk_run.add_argument("check_id", help="ID of the check to run")
    p_chk_sub.add_parser("run-all", help="Run all configured checks immediately")

    # Budget (alias to usage)
    p_bud = subparsers.add_parser("budget", help="Deprecated alias for 'usage'")
    p_bud.add_argument("--config", "-c", default="/etc/wheatley/config.yaml")
    p_bud.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env")
    p_bud.add_argument("--db", default="/var/lib/wheatley/wheatley.db")
    p_bud_sub = p_bud.add_subparsers(dest="budget_subcommand")
    p_bud_sub.add_parser("usage", help="Show token consumption")
    p_bud_bank = p_bud_sub.add_parser("set-bank", help="Legacy command (no-op)")
    p_bud_bank.add_argument("amount", type=float, help="Amount in USD (e.g. 15.00)")

    # Discover
    p_disc = subparsers.add_parser("discover", help="Scan Proxmox nodes and auto-discover guests/services")
    p_disc.add_argument("--config", "-c", default="/etc/wheatley/config.yaml", help="Path to config.yaml")
    p_disc.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env", help="Path to secrets.env")
    p_disc.add_argument("--output", "-o", help="Path to write recommended draft config.yaml")
    p_disc.add_argument("--json", action="store_true", help="Output discovered inventory as JSON")

    # Config validate
    p_val = subparsers.add_parser("config", help="Configuration utilities")
    p_val_sub = p_val.add_subparsers(dest="config_command")
    p_check = p_val_sub.add_parser("validate", help="Validate config syntax and schema")
    p_check.add_argument("--config", "-c", default="/etc/wheatley/config.yaml")
    p_check.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env")

    # Audit
    p_aud = subparsers.add_parser("audit", help="Inspect audit logs")
    p_aud.add_argument("--db", default="/var/lib/wheatley/wheatley.db")
    p_aud.add_argument("--limit", "-n", type=int, default=15)

    args = parser.parse_args()

    if args.command == "run":
        config_path = getattr(args, "config", "/etc/wheatley/config.yaml")
        secrets_path = getattr(args, "secrets", "/etc/wheatley/secrets.env")
        try:
            app = WheatleyApp(config_path, secrets_path)
            asyncio.run(app.run())
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.exception("Fatal startup error: %s", e)
            sys.exit(1)

    elif args.command == "ask":
        cmd_ask(args)

    elif args.command in ("usage", "budget"):
        cmd_usage(args)

    elif args.command == "check":
        cmd_check(args)

    elif args.command == "discover":
        cmd_discover(args)

    elif args.command == "config" and getattr(args, "config_command", None) == "validate":
        cmd_validate(args)

    elif args.command == "audit":
        cmd_audit(args)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
