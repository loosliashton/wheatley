"""Wheatley Command Line Interface (CLI)"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from wheatley.audit import AuditLogger
from wheatley.chat.telegram import TelegramBot
from wheatley.config import WheatleyConfig, load_config

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
        self.bot: TelegramBot | None = None
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
            f"• *Budget Cap:* `${self.config.budget.monthly_grant_usd:.2f}/mo`",
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

        if self.config.chat.telegram:
            self.bot = TelegramBot(
                config=self.config.chat.telegram,
                audit=self.audit,
                status_provider=self.get_status_overview,
            )
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
        if self.bot:
            await self.bot.stop()
        self.audit.log_event("SYSTEM", "daemon", "shutdown", "SUCCESS")
        logger.info("Wheatley stopped cleanly.")


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Wheatley - Homelab Monitoring & AI Agent")
    subparsers = parser.add_subparsers(dest="command")

    # Run
    p_run = subparsers.add_parser("run", help="Run the Wheatley monitoring agent daemon")
    p_run.add_argument("--config", "-c", default="/etc/wheatley/config.yaml", help="Path to config.yaml")
    p_run.add_argument("--secrets", "-s", default="/etc/wheatley/secrets.env", help="Path to secrets.env")

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

    elif args.command == "config" and getattr(args, "config_command", None) == "validate":
        cmd_validate(args)

    elif args.command == "audit":
        cmd_audit(args)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
