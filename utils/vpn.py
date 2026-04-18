from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass


@dataclass(slots=True)
class OpenVpnController:
    """Maintains an OpenVPN tunnel using either openvpn CLI or openvpn-gui."""

    enabled: bool
    config_file: str
    reconnect_seconds: int
    openvpn_executable: str = "openvpn"
    auth_file: str = ""

    def _is_gui_mode(self) -> bool:
        """Detects whether the configured executable is the Windows GUI client."""
        name = os.path.basename(self.openvpn_executable).lower()
        return "openvpn-gui" in name

    async def _run(self, *command: str) -> tuple[int, str, str]:
        """Executes a command and returns code/stdout/stderr for diagnostics."""
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def _connect_gui(self, logger: logging.Logger) -> None:
        """Connects OpenVPN tunnel through openvpn-gui command mode."""
        code, out, err = await self._run(
            self.openvpn_executable,
            "--command",
            "connect",
            self.config_file,
        )
        if code != 0:
            raise RuntimeError(f"openvpn-gui connect failed: {err or out}")
        logger.info("OpenVPN GUI connect command issued for %s", self.config_file)

    async def _disconnect_gui(self, logger: logging.Logger) -> None:
        """Disconnects OpenVPN tunnel through openvpn-gui command mode."""
        code, out, err = await self._run(
            self.openvpn_executable,
            "--command",
            "disconnect",
            self.config_file,
        )
        if code != 0:
            logger.warning("openvpn-gui disconnect failed: %s", err or out)

    async def _connect_cli(self, logger: logging.Logger) -> None:
        """Starts a managed OpenVPN CLI process for the configured profile file."""
        cmd = [self.openvpn_executable, "--config", self.config_file]
        if self.auth_file:
            cmd.extend(["--auth-user-pass", self.auth_file])

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

        if self._is_gui_mode():
            await self._connect_gui(logger)
            return

        if self._proc is None or self._proc.returncode is not None:
            if self._proc and self._proc.returncode is not None:
                logger.warning("OpenVPN process exited with code %s; reconnecting", self._proc.returncode)
            await self._connect_cli(logger)

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
