"""Safe Linux network configuration with staged apply and rollback."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config_manager import ConfigError


NET_CONF = {
    "eth0": Path("/etc/net.conf"),
    "eth1": Path("/etc/net2.conf"),
}
DNS_CONF = Path("/etc/htnet/dns/resolv.conf")
DHCP_SCRIPT = Path("/usr/share/udhcpc/htnice_eth.script")
INTERFACES = ("eth0", "eth1")


class NetworkError(RuntimeError):
    pass


def netmask_to_prefix(netmask: str) -> int:
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except ValueError as exc:
        raise ConfigError("invalid netmask") from exc


def prefix_to_netmask(prefix: int) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{int(prefix)}").netmask)


class NetworkManager:
    def __init__(
        self,
        app_dir: Path,
        logger: logging.Logger,
        *,
        timeout_sec: int = 180,
    ):
        self.app_dir = Path(app_dir)
        self.logger = logger
        self.timeout_sec = int(timeout_sec)
        self.pending_path = self.app_dir / "network-pending.json"
        self.baseline_marker = self.app_dir / ".network-baseline-v2"
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._pending: Optional[Dict[str, Any]] = None
        self._dhcp_procs: Dict[str, subprocess.Popen] = {}
        self._ip = shutil.which("ip") or "/sbin/ip"
        self._udhcpc = shutil.which("udhcpc") or "/sbin/udhcpc"
        self._recover_pending()
        self._ensure_baseline()

    def _run(self, args: List[str], *, timeout: int = 10) -> subprocess.CompletedProcess:
        self.logger.debug("network command: %s", " ".join(args))
        try:
            return subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(args, 1, "", str(exc))

    def _run_ignore(self, args: List[str], *, timeout: int = 10) -> None:
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            text = (result.stdout + result.stderr).strip()
            if "File exists" not in text and "Cannot assign" not in text:
                raise NetworkError(text or f"command failed: {' '.join(args)}")

    def _recover_pending(self) -> None:
        if not self.pending_path.exists():
            return
        try:
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
            self.logger.warning(
                "Recovering unfinished network change for %s", data.get("interface")
            )
            self._rollback_locked(data, "service restart recovery")
        except Exception as exc:
            self.logger.exception("Failed to recover pending network change: %s", exc)
            try:
                self.pending_path.unlink()
            except OSError:
                pass

    def _ensure_baseline(self) -> None:
        if self.baseline_marker.exists():
            return
        failed = False
        for name in INTERFACES:
            live = self.live(name)
            if not live.get("ip"):
                continue
            config = {
                "method": "static",
                "ip": live["ip"],
                "prefix": live["prefix"],
                "gateway": live.get("gateway", ""),
                "dns": self.dns_servers(),
            }
            try:
                self._write_persistent(name, config)
                self.logger.info(
                    "Network baseline persisted for %s: %s/%s via %s",
                    name,
                    live["ip"],
                    live["prefix"],
                    live.get("gateway") or "-",
                )
            except Exception as exc:
                failed = True
                self.logger.warning("Could not persist network baseline for %s: %s", name, exc)
        if failed:
            self.logger.warning(
                "Network baseline remains incomplete; it will be retried on next startup"
            )
            return
        try:
            self.baseline_marker.write_text(
                datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
            )
        except OSError:
            pass

    def live(self, interface: str) -> Dict[str, Any]:
        if interface not in INTERFACES:
            raise ConfigError("unknown network interface")
        result = self._run([self._ip, "-4", "-o", "addr", "show", "dev", interface])
        text = result.stdout if result.returncode == 0 else ""
        matches = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", text)
        addresses = [
            {
                "ip": address,
                "prefix": int(prefix),
                "netmask": prefix_to_netmask(int(prefix)),
            }
            for address, prefix in matches
        ]
        ip = addresses[0]["ip"] if addresses else ""
        prefix = addresses[0]["prefix"] if addresses else 0
        gateway = ""
        routes = self._run([self._ip, "route", "show", "default", "dev", interface])
        if routes.returncode == 0:
            route_match = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", routes.stdout)
            if route_match:
                gateway = route_match.group(1)
        mac = ""
        try:
            mac = Path(f"/sys/class/net/{interface}/address").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            pass
        link = self._run([self._ip, "link", "show", "dev", interface])
        flags_match = re.search(r"<([^>]+)>", link.stdout) if link.returncode == 0 else None
        flags = set(flags_match.group(1).split(",")) if flags_match else set()
        up = "UP" in flags
        carrier = up and "NO-CARRIER" not in flags
        return {
            "name": interface,
            "ip": ip,
            "prefix": prefix,
            "netmask": prefix_to_netmask(prefix) if prefix else "",
            "addresses": addresses,
            "gateway": gateway,
            "mac": mac,
            "up": up,
            "carrier": carrier,
        }

    def dns_servers(self) -> List[str]:
        result: List[str] = []
        try:
            for line in DNS_CONF.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    result.append(parts[1])
        except OSError:
            pass
        return result

    def _persistent(self, interface: str) -> Dict[str, Any]:
        path = NET_CONF[interface]
        result: Dict[str, Any] = {"method": "static", "ip": "", "gateway": ""}
        try:
            values = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
            method = values.get("METHOD", "STATIC").upper()
            result["method"] = "dhcp" if method.startswith("DHCP") else "static"
            result["ip"] = values.get("IPADDR", "")
            result["netmask"] = values.get("NETMASK", "")
            result["gateway"] = values.get("GATEWAY", "")
            result["prefix"] = (
                netmask_to_prefix(result["netmask"]) if result.get("netmask") else 0
            )
        except (OSError, ConfigError):
            pass
        return result

    def status(self) -> Dict[str, Any]:
        addresses = self._run([self._ip, "-4", "addr", "show"])
        routes = self._run([self._ip, "route"])
        interfaces = []
        for name in INTERFACES:
            live = self.live(name)
            persistent = self._persistent(name)
            live["method"] = persistent.get("method", "static")
            live["persistent"] = persistent
            interfaces.append(live)
        with self._lock:
            pending = self._public_pending_locked()
        return {
            "interfaces": interfaces,
            "dns": self.dns_servers(),
            "pending": pending,
            "addresses": addresses.stdout if addresses.returncode == 0 else "",
            "routes": routes.stdout if routes.returncode == 0 else "",
            "error": "",
        }

    def _normalise(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        method = str(payload.get("method", "static")).lower()
        if method not in ("static", "dhcp"):
            raise ConfigError("network method must be static or dhcp")
        dns = payload.get("dns")
        if dns is None:
            dns = self.dns_servers()
        if isinstance(dns, str):
            dns = [item.strip() for item in re.split(r"[,;\s]+", dns) if item.strip()]
        if not isinstance(dns, list) or len(dns) > 4:
            raise ConfigError("dns must be a list of at most four IPv4 addresses")
        clean_dns = []
        for item in dns:
            try:
                clean_dns.append(str(ipaddress.IPv4Address(str(item))))
            except ipaddress.AddressValueError as exc:
                raise ConfigError("invalid DNS server address") from exc
        if method == "dhcp":
            return {"method": "dhcp", "dns": clean_dns}
        try:
            address = ipaddress.IPv4Address(str(payload.get("ip", "")))
        except ipaddress.AddressValueError as exc:
            raise ConfigError("invalid IPv4 address") from exc
        prefix = int(payload.get("prefix", 0) or 0)
        if not prefix:
            prefix = netmask_to_prefix(str(payload.get("netmask", "")))
        if not 1 <= prefix <= 32:
            raise ConfigError("invalid prefix length")
        gateway = str(payload.get("gateway", "")).strip()
        if gateway:
            try:
                gateway_obj = ipaddress.IPv4Address(gateway)
            except ipaddress.AddressValueError as exc:
                raise ConfigError("invalid gateway address") from exc
            if gateway_obj not in ipaddress.IPv4Network(f"{address}/{prefix}", strict=False):
                raise ConfigError("gateway must be inside the configured subnet")
            gateway = str(gateway_obj)
        return {
            "method": "static",
            "ip": str(address),
            "prefix": prefix,
            "netmask": prefix_to_netmask(prefix),
            "gateway": gateway,
            "dns": clean_dns,
        }

    def stage(self, interface: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if interface not in INTERFACES:
            raise ConfigError("unknown network interface")
        with self._lock:
            if self._pending:
                raise ConfigError("another network change is pending confirmation")
            old = self.live(interface)
            old["method"] = self._persistent(interface).get("method", "static")
            new = self._normalise(payload)
            change_id = secrets.token_hex(8)
            data = {
                "change_id": change_id,
                "interface": interface,
                "old": old,
                "new": new,
                "created": datetime.now().isoformat(timespec="seconds"),
                "expires_at": time.time() + self.timeout_sec,
            }
            self._pending = data
            self._write_pending_file(data)
            try:
                if new["method"] == "static":
                    self._apply_static_new(interface, old, new)
                else:
                    threading.Thread(
                        target=self._delayed_dhcp,
                        args=(change_id,),
                        daemon=True,
                        name=f"dhcp-{interface}",
                    ).start()
            except Exception as exc:
                self._pending = None
                self._delete_pending_file()
                self._rollback_locked(data, f"stage failed: {exc}")
                raise NetworkError(str(exc)) from exc
            self._timer = threading.Timer(
                self.timeout_sec, self._expire, args=(change_id,)
            )
            self._timer.daemon = True
            self._timer.start()
            self.logger.info(
                "Network change staged interface=%s old=%s/%s new=%s/%s method=%s",
                interface,
                old.get("ip"),
                old.get("prefix"),
                new.get("ip"),
                new.get("prefix"),
                new.get("method"),
            )
            return self._public_pending_locked()

    def _apply_static_new(
        self,
        interface: str,
        old: Dict[str, Any],
        new: Dict[str, Any],
    ) -> None:
        self._run_ignore([self._ip, "link", "set", interface, "up"])
        self._run_ignore(
            [
                self._ip,
                "addr",
                "add",
                f"{new['ip']}/{new['prefix']}",
                "dev",
                interface,
            ]
        )
        if new.get("gateway") and new.get("gateway") != old.get("gateway"):
            self._run_ignore(
                [
                    self._ip,
                    "route",
                    "replace",
                    "default",
                    "via",
                    new["gateway"],
                    "dev",
                    interface,
                    "metric",
                    "50",
                ]
            )

    def _delayed_dhcp(self, change_id: str) -> None:
        time.sleep(1.0)
        with self._lock:
            if not self._pending or self._pending.get("change_id") != change_id:
                return
            interface = self._pending["interface"]
        try:
            proc = subprocess.Popen(
                [
                    self._udhcpc,
                    "-n",
                    "-q",
                    "-i",
                    interface,
                    "-s",
                    str(DHCP_SCRIPT),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            with self._lock:
                self._dhcp_procs[interface] = proc
            output = proc.communicate(timeout=25)[0]
            if proc.returncode != 0:
                raise NetworkError(f"DHCP failed: {output.strip()[-300:]}")
            self.logger.info("DHCP lease obtained interface=%s", interface)
        except Exception as exc:
            self.logger.warning("DHCP staging failed interface=%s error=%s", interface, exc)
            with self._lock:
                if self._pending and self._pending.get("change_id") == change_id:
                    self._rollback_locked(self._pending, f"DHCP failed: {exc}")

    def confirm(self, change_id: str) -> Dict[str, Any]:
        with self._lock:
            data = self._require_pending(change_id)
            self._cancel_timer()
            interface = data["interface"]
            new = data["new"]
            old = data["old"]
            snapshot = self._snapshot_text_files(
                [NET_CONF[interface], DNS_CONF]
            )
            try:
                if new["method"] == "dhcp":
                    self._start_dhcp_daemon(interface)
                    current = self.live(interface)
                else:
                    self._commit_static_route(interface, old, new)
                    current = new
                self._write_persistent(interface, new)
                if new.get("dns") is not None:
                    self._write_dns(new["dns"])
                if (
                    old.get("ip")
                    and old.get("prefix")
                    and old.get("ip") != current.get("ip")
                ):
                    threading.Timer(
                        2.0,
                        self._remove_old_address,
                        args=(
                            interface,
                            old["ip"],
                            int(old["prefix"]),
                        ),
                    ).start()
            except Exception:
                self._restore_text_files(snapshot)
                self._rollback_locked(data, "confirm failed")
                raise
            self._pending = None
            self._delete_pending_file()
            self.logger.info(
                "Network change confirmed interface=%s method=%s ip=%s",
                interface,
                new.get("method"),
                new.get("ip") or "dhcp",
            )
            return {
                "ok": True,
                "interface": interface,
                "message": "network configuration committed",
            }

    def rollback(self, change_id: str) -> Dict[str, Any]:
        with self._lock:
            data = self._require_pending(change_id)
            self._rollback_locked(data, "user rollback")
            return {"ok": True, "message": "network change rolled back"}

    def _expire(self, change_id: str) -> None:
        with self._lock:
            if not self._pending or self._pending.get("change_id") != change_id:
                return
            self._rollback_locked(self._pending, "confirmation timeout")

    def _rollback_locked(self, data: Dict[str, Any], reason: str) -> None:
        self._cancel_timer()
        interface = data["interface"]
        old = data.get("old", {})
        new = data.get("new", {})
        if new.get("method") == "dhcp":
            self._stop_dhcp(interface)
            current = self.live(interface)
            if current.get("ip") and current.get("ip") != old.get("ip"):
                self._run_ignore(
                    [
                        self._ip,
                        "addr",
                        "del",
                        f"{current['ip']}/{current['prefix']}",
                        "dev",
                        interface,
                    ]
                )
        elif new.get("ip") and new.get("ip") != old.get("ip"):
            self._run_ignore(
                [
                    self._ip,
                    "addr",
                    "del",
                    f"{new['ip']}/{new['prefix']}",
                    "dev",
                    interface,
                ]
            )
        self._rollback_route(interface, old, new)
        if old.get("ip") and old.get("prefix"):
            self._run_ignore(
                [
                    self._ip,
                    "addr",
                    "add",
                    f"{old['ip']}/{old['prefix']}",
                    "dev",
                    interface,
                ]
            )
        self._pending = None
        self._delete_pending_file()
        self.logger.warning(
            "Network change rolled back interface=%s reason=%s",
            interface,
            reason,
        )

    def _commit_static_route(
        self,
        interface: str,
        old: Dict[str, Any],
        new: Dict[str, Any],
    ) -> None:
        old_gateway = str(old.get("gateway", ""))
        new_gateway = str(new.get("gateway", ""))
        if old_gateway == new_gateway:
            return
        if new_gateway:
            self._run_ignore(
                [
                    self._ip,
                    "route",
                    "replace",
                    "default",
                    "via",
                    new_gateway,
                    "dev",
                    interface,
                    "metric",
                    "0",
                ]
            )
            self._delete_default_route(interface, new_gateway, 50)
        elif old_gateway:
            self._delete_default_route(interface, old_gateway, 0)

    def _rollback_route(
        self,
        interface: str,
        old: Dict[str, Any],
        new: Dict[str, Any],
    ) -> None:
        old_gateway = str(old.get("gateway", ""))
        new_gateway = str(new.get("gateway", ""))
        if old_gateway == new_gateway:
            return
        if new_gateway:
            self._delete_default_route(interface, new_gateway, 50)
        if old_gateway:
            self._run_ignore(
                [
                    self._ip,
                    "route",
                    "replace",
                    "default",
                    "via",
                    old_gateway,
                    "dev",
                    interface,
                    "metric",
                    "0",
                ]
            )

    def _delete_default_route(
        self, interface: str, gateway: str, metric: int
    ) -> None:
        self._run(
            [
                self._ip,
                "route",
                "del",
                "default",
                "via",
                gateway,
                "dev",
                interface,
                "metric",
                str(metric),
            ]
        )

    @staticmethod
    def _snapshot_text_files(paths: List[Path]) -> Dict[Path, Optional[str]]:
        snapshot: Dict[Path, Optional[str]] = {}
        for path in paths:
            try:
                snapshot[path] = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                snapshot[path] = None
        return snapshot

    def _restore_text_files(
        self, snapshot: Dict[Path, Optional[str]]
    ) -> None:
        for path, content in snapshot.items():
            if content is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                self._atomic_write(path, content)

    def _remove_old_address(self, interface: str, ip: str, prefix: int) -> None:
        try:
            self._run_ignore(
                [self._ip, "addr", "del", f"{ip}/{prefix}", "dev", interface]
            )
            self.logger.info("Removed previous address %s/%s from %s", ip, prefix, interface)
        except Exception as exc:
            self.logger.warning("Could not remove previous address %s: %s", ip, exc)

    def _start_dhcp_daemon(self, interface: str) -> None:
        self._stop_dhcp(interface)
        log_path = self.app_dir / "logs" / f"udhcpc-{interface}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        proc = subprocess.Popen(
            [
                self._udhcpc,
                "-i",
                interface,
                "-s",
                str(DHCP_SCRIPT),
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._dhcp_procs[interface] = proc
        self.logger.info("DHCP daemon started interface=%s pid=%s", interface, proc.pid)

    def _stop_dhcp(self, interface: str) -> None:
        proc = self._dhcp_procs.pop(interface, None)
        if not proc:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _write_persistent(self, interface: str, config: Dict[str, Any]) -> None:
        path = NET_CONF[interface]
        backup = path.with_name(path.name + ".ntp-gateway.bak")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        if config["method"] == "dhcp":
            lines = ["METHOD=DHCP"]
        else:
            netmask = str(config.get("netmask", "")).strip()
            if not netmask and config.get("prefix"):
                netmask = prefix_to_netmask(int(config["prefix"]))
            if not netmask:
                raise NetworkError("static network configuration requires a netmask")
            lines = [
                "METHOD=STATIC",
                f"IPADDR={config['ip']}",
                f"NETMASK={netmask}",
            ]
            if config.get("gateway"):
                lines.append(f"GATEWAY={config['gateway']}")
        self._atomic_write(path, "\n".join(lines) + "\n")

    def _write_dns(self, servers: List[str]) -> None:
        content = "".join(f"nameserver {item}\n" for item in servers)
        self._atomic_write(DNS_CONF, content)
        script = Path("/etc/htnet/dns/setdns.script")
        if script.exists():
            self._run([str(script)], timeout=10)
        else:
            self._atomic_write(Path("/run/resolv.conf"), content)
            self._atomic_write(Path("/run/htnice_ethdns"), content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o644)

    def _write_pending_file(self, data: Dict[str, Any]) -> None:
        self._atomic_write(
            self.pending_path,
            json.dumps(data, ensure_ascii=False, indent=2),
        )

    def _delete_pending_file(self) -> None:
        try:
            self.pending_path.unlink()
        except OSError:
            pass

    def _require_pending(self, change_id: str) -> Dict[str, Any]:
        if not self._pending:
            raise ConfigError("no network change is pending")
        if self._pending.get("change_id") != change_id:
            raise ConfigError("network change id does not match")
        return self._pending

    def _public_pending_locked(self) -> Optional[Dict[str, Any]]:
        if not self._pending:
            return None
        remaining = max(
            0, int(self._pending.get("expires_at", time.time()) - time.time())
        )
        return {
            "change_id": self._pending["change_id"],
            "interface": self._pending["interface"],
            "old": self._pending.get("old", {}),
            "new": self._pending.get("new", {}),
            "remaining_sec": remaining,
            "expires_at": self._pending.get("expires_at"),
            "created": self._pending.get("created"),
        }

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
