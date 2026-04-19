from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class OpenVpnController:
    """Maintains an OpenVPN tunnel using either openvpn CLI or openvpn-gui."""

    enabled: bool
    config_file: str
    reconnect_seconds: int
    openvpn_executable: str = "openvpn"
    auth_file: str = ""
    username: str = ""
    password: str = ""
    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _generated_auth_file: Path | None = field(default=None, init=False, repr=False)

    def _gui_profile_candidates(self) -> list[str]:
        """Returns likely profile identifiers accepted by openvpn-gui --command."""
        raw = self.config_file.strip()
        if not raw:
            return []
        base = os.path.basename(raw)
        stem, _ = os.path.splitext(base)
        # Use the profile name that OpenVPN GUI registers from config directories.
        candidates = [base, stem]
        seen: set[str] = set()
        ordered: list[str] = []
        for item in candidates:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(item)
        return ordered

    def _is_gui_mode(self) -> bool:
        """Detects whether the configured executable is the Windows GUI client."""
        name = os.path.basename(self.openvpn_executable).lower()
        return "openvpn-gui" in name

    def _requires_auth_file(self) -> bool:
        """Returns true when profile uses auth-user-pass without embedded file path."""
        try:
            with open(self.config_file, "r", encoding="utf-8", errors="ignore") as handle:
                for raw in handle:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith("auth-user-pass"):
                        parts = line.split(maxsplit=1)
                        # auth-user-pass with no argument requires interactive input or --auth-user-pass file.
                        return len(parts) == 1
        except OSError:
            # Path existence is checked separately by ensure_connected.
            return False
        return False

    def _has_inline_credentials(self) -> bool:
        """Returns true when username/password env vars are available."""
        return bool(self.username.strip() and self.password.strip())

    def _resolve_auth_file(self) -> str:
        """Returns an auth file path, generating one from env credentials if needed."""
        if self.auth_file:
            return self.auth_file

        if not self._has_inline_credentials():
            return ""

        if self._generated_auth_file and self._generated_auth_file.exists():
            return str(self._generated_auth_file)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="kudan-openvpn-", suffix=".auth", delete=False) as handle:
            handle.write(f"{self.username}\n{self.password}\n")
            auth_path = Path(handle.name)

        self._generated_auth_file = auth_path
        return str(auth_path)

    def _gui_config_dir(self) -> Path:
        """Returns the per-user OpenVPN config directory used by OpenVPN GUI."""
        return Path.home() / "OpenVPN" / "config"

    def _is_in_gui_config_dir(self, path: str) -> bool:
        """Checks whether a config file already lives in a GUI-visible config directory."""
        target = Path(path).resolve()
        for candidate_dir in (self._gui_config_dir(), Path(r"C:\Program Files\OpenVPN\config")):
            try:
                if target.is_relative_to(candidate_dir.resolve()):
                    return True
            except AttributeError:
                # Python < 3.9 compatibility path: string-prefix fallback.
                resolved_dir = str(candidate_dir.resolve()).lower().rstrip("\\/")
                if str(target).lower().startswith(resolved_dir):
                    return True
        return False

    def _stage_gui_profile(self) -> str:
        """Copies the profile into the user OpenVPN config directory when needed."""
        source = Path(self.config_file)
        target_dir = self._gui_config_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        self.config_file = str(target)
        return target.stem

    async def _run(self, *command: str) -> tuple[int, str, str]:
        """Executes a command and returns code/stdout/stderr for diagnostics."""
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def _vpn_adapter_snapshot(self) -> list[dict[str, str]]:
        """Returns a summary of OpenVPN-related adapters on Windows."""
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'OpenVPN|TAP|Wintun|ProtonVPN' } | "
            "Select-Object Name,Status,InterfaceDescription | ConvertTo-Json -Compress",
        ]
        code, out, err = await self._run(*command)
        if code != 0 or not out:
            return []
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return []
        return [
            {
                "Name": str(item.get("Name") or ""),
                "Status": str(item.get("Status") or ""),
                "InterfaceDescription": str(item.get("InterfaceDescription") or ""),
            }
            for item in payload
            if isinstance(item, dict)
        ]

    async def _wait_for_vpn_up(self, logger: logging.Logger, timeout_seconds: int = 30) -> None:
        """Polls adapter state until an OpenVPN adapter reports Up or times out."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_snapshot: list[dict[str, str]] = []
        while True:
            last_snapshot = await self._vpn_adapter_snapshot()
            if any(entry.get("Status", "").lower() == "up" for entry in last_snapshot):
                logger.info("OpenVPN adapter is up: %s", last_snapshot)
                return
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(1)

        cause = ""
        if self._requires_auth_file() and not self.auth_file:
            cause = (
                " The profile contains auth-user-pass and no OPENVPN_AUTH_FILE is configured, "
                "so the GUI may be waiting for credentials or a saved session."
            )

        raise RuntimeError(
            "OpenVPN did not establish a tunnel within the timeout." + cause + " "
            f"Adapter snapshot: {last_snapshot or 'no OpenVPN/TAP/Wintun adapters detected'}"
        )

    async def _connect_gui(self, logger: logging.Logger) -> None:
        """Connects OpenVPN tunnel through openvpn-gui command mode."""
        last_error = ""
        for profile in self._gui_profile_candidates():
            code, out, err = await self._run(
                self.openvpn_executable,
                "--command",
                "connect",
                profile,
            )
            if code == 0:
                logger.info("OpenVPN GUI connect command issued for profile %s", profile)
                return
            last_error = err or out
        raise RuntimeError(f"openvpn-gui connect failed: {last_error or 'unknown error'}")

    async def _disconnect_gui(self, logger: logging.Logger) -> None:
        """Disconnects OpenVPN tunnel through openvpn-gui command mode."""
        for profile in self._gui_profile_candidates():
            code, out, err = await self._run(
                self.openvpn_executable,
                "--command",
                "disconnect",
                profile,
            )
            if code == 0:
                return
            logger.warning("openvpn-gui disconnect failed for profile %s: %s", profile, err or out)

    async def _connect_cli(self, logger: logging.Logger) -> None:
        """Starts a managed OpenVPN CLI process for the configured profile file."""
        cmd = [self.openvpn_executable, "--config", self.config_file]
        auth_file = self._resolve_auth_file()
        if auth_file:
            cmd.extend(["--auth-user-pass", auth_file])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._proc = proc
        logger.info("OpenVPN process started (pid=%s) using config %s", proc.pid, self.config_file)

    async def _disconnect_cli(self, logger: logging.Logger) -> None:
        """Stops the managed OpenVPN CLI process if it is running."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._proc = None
            return

        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        finally:
            logger.info("OpenVPN process stopped")
            self._proc = None

    async def ensure_connected(self, logger: logging.Logger) -> None:
        """Ensures VPN connectivity and reconnects automatically when disconnected."""
        if not self.enabled:
            return

        if not self.config_file:
            raise RuntimeError("OPENVPN_CONFIG_FILE is required when VPN_ENABLED=true")

        if not os.path.exists(self.config_file):
            raise RuntimeError(f"OPENVPN_CONFIG_FILE not found: {self.config_file}")

        if not self._is_gui_mode() and self._requires_auth_file() and not self.auth_file:
            if not self._has_inline_credentials():
                raise RuntimeError(
                    "OPENVPN_AUTH_FILE or OPENVPN_USERNAME/OPENVPN_PASSWORD is required for CLI mode when profile contains auth-user-pass"
                )

        if self.auth_file and not os.path.exists(self.auth_file):
            raise RuntimeError(f"OPENVPN_AUTH_FILE not found: {self.auth_file}")

        if self._is_gui_mode():
            if not self._is_in_gui_config_dir(self.config_file):
                profile_name = self._stage_gui_profile()
                logger.info("Staged OpenVPN profile into %s as %s", self.config_file, profile_name)
            await self._connect_gui(logger)
            await self._wait_for_vpn_up(logger)
            return

        if self._proc is None or self._proc.returncode is not None:
            if self._proc and self._proc.returncode is not None:
                logger.warning("OpenVPN process exited with code %s; reconnecting", self._proc.returncode)
            await self._connect_cli(logger)
            await self._wait_for_vpn_up(logger)

    async def reconnect(self, logger: logging.Logger) -> None:
        """Performs an explicit disconnect-then-connect cycle for tunnel recovery."""
        if not self.enabled:
            return

        if self._is_gui_mode():
            await self._disconnect_gui(logger)
            await asyncio.sleep(1)
            await self._connect_gui(logger)
            return

        await self._disconnect_cli(logger)
        await asyncio.sleep(1)
        await self._connect_cli(logger)

    async def shutdown(self, logger: logging.Logger) -> None:
        """Gracefully tears down VPN session during application shutdown."""
        if not self.enabled:
            return

        if self._is_gui_mode():
            await self._disconnect_gui(logger)
        else:
            await self._disconnect_cli(logger)

        if self._generated_auth_file and self._generated_auth_file.exists():
            try:
                self._generated_auth_file.unlink()
            except OSError:
                logger.warning("Unable to remove generated OpenVPN auth file: %s", self._generated_auth_file)
            finally:
                self._generated_auth_file = None

    async def watch_loop(self, logger: logging.Logger, stop_event: asyncio.Event) -> None:
        """Runs a periodic keepalive loop that restores VPN connectivity if needed."""
        try:
            while not stop_event.is_set():
                try:
                    await self.ensure_connected(logger)
                except Exception as exc:
                    logger.exception("OpenVPN watch loop error: %s", exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.reconnect_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.shutdown(logger)


# Backward-compatible alias for older imports.
ProtonVpnController = OpenVpnController
