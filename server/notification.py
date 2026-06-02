"""
Windows Toast notification for new tasks.

Sends a toast via PowerShell (BurntToast module preferred, balloon-tip fallback).
Windows-only.  All public functions are async so they never block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import webbrowser
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Task:
    title: str
    priority: str
    agent_id: str


async def send_toast_notification(task: Task) -> bool:
    """Show a Windows Toast notification for a new task.

    Runs PowerShell as a subprocess (non-blocking) and returns True on success.
    On non-Windows platforms this is a no-op that returns True.
    """
    if platform.system() != "Windows":
        logger.debug("Toast notifications are Windows-only; skipping.")
        return True

    title = f"New Task: {task.title}"[:50]
    body = f"{task.priority} priority from {task.agent_id}"
    dashboard_url = "http://localhost:4350/dashboard"

    script = _build_powershell_script(title, body, dashboard_url)

    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=15.0
        )

        if proc.returncode == 0:
            logger.info("Toast notification sent successfully: title=%r", title)
            return True
        else:
            logger.warning(
                "PowerShell toast failed (rc=%d). stderr=%r, stdout=%r",
                proc.returncode,
                stderr.decode("utf-8", errors="replace").strip(),
                stdout.decode("utf-8", errors="replace").strip(),
            )
            return False
    except asyncio.TimeoutError:
        logger.error("PowerShell toast timed out after 15s.")
        return False
    except OSError as exc:
        logger.error("PowerShell toast invocation failed: %s", exc)
        return False


def _open_dashboard() -> None:
    """Open the task dashboard in the default browser."""
    webbrowser.open("http://localhost:4350/dashboard")


def _build_powershell_script(title: str, body: str, url: str) -> str:
    """Return a self-contained PowerShell script string.

    Uses BurntToast if the module is available; otherwise falls back to a
    .NET Windows.Forms.NotifyIcon balloon tip that opens the URL on click.
    """
    # Escape single quotes for the PowerShell string literals below.
    title_escaped = title.replace("'", "''")
    body_escaped = body.replace("'", "''")
    url_escaped = url.replace("'", "''")

    return f'''
$title = '{title_escaped}'
$body  = '{body_escaped}'
$url   = '{url_escaped}'

if (Get-Module -ListAvailable -Name BurntToast) {{
    try {{
        Import-Module BurntToast -ErrorAction Stop
        $btn = New-BTButton -Content "Open Dashboard" -Arguments "$url"
        New-BurntToastNotification -Text $title, $body -Button $btn -ErrorAction Stop
        exit 0
    }} catch {{
        Write-Warning "BurntToast failed: $_"
    }}
}}

# Fallback: .NET balloon tip
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.BalloonTipTitle = $title
$notify.BalloonTipText  = "$body`n`nClick to open dashboard"
$notify.Visible = $true
$notify.ShowBalloonTip(5000)

# Open the URL when the balloon is clicked
Register-ObjectEvent -InputObject $notify -EventName BalloonTipClicked -Action {{
    Start-Process "$url"
    $sender.Visible = $false
    $sender.Dispose()
}} | Out-Null

Start-Sleep -Seconds 6
$notify.Visible = $false
$notify.Dispose()
exit 0
'''.strip()


# ---------------------------------------------------------------------------
# Quick self-test (run this file directly)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    async def _test():
        dummy = Task(
            title="Example task with a very long name that gets truncated",
            priority="High",
            agent_id="agent-7",
        )
        ok = await send_toast_notification(dummy)
        print("Result:", ok)
        if ok:
            _open_dashboard()

    asyncio.run(_test())
