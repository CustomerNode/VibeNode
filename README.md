# VibeNode

A local web interface for managing Claude Code sessions — built by [CustomerNode](https://customernode.com) and [Claude Code](https://claude.ai/download).

## What it does

- Lists all your Claude Code sessions with live state (Working / Idle / Question / Sleeping)
- Live terminal panel — watch Claude work in real time
- Answer Claude's questions directly from the browser (with clickable option buttons)
- Send commands to running sessions
- Session tools: auto-name, duplicate, fork, rewind, delete, summarize, extract code, compare sessions

## Requirements

- Python 3.10+
- Claude Code installed and at least one session created
- Windows (uses PowerShell for process detection and input)

## Setup (AI-assisted — recommended)

If you have [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed, open your terminal and tell Claude:

> Get me set up with https://github.com/CustomerNode/VibeNode

Claude handles the rest — cloning the repo, installing Python and Flask if needed, creating a desktop shortcut, and launching VibeNode for you.

See [FileTaskNode](https://github.com/CustomerNode/FileTaskNode) for an example of a Claude Code workspace built around this kind of AI-assisted setup.

## Setup (manual)

### 1. Clone and install

```bash
git clone https://github.com/CustomerNode/VibeNode.git
cd VibeNode
pip install flask
```

### 2. Run

```bash
python session_manager.py
```

The browser opens automatically to http://localhost:5050.

### 3. Desktop shortcut (Windows)

Run this once in PowerShell to create a desktop shortcut that launches VibeNode with one click:

```powershell
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\VibeNode.lnk")
$Shortcut.TargetPath = "python"
$Shortcut.Arguments = "session_manager.py"
$Shortcut.WorkingDirectory = "$env:USERPROFILE\Documents\VibeNode"
$Shortcut.IconLocation = "$env:USERPROFILE\Documents\VibeNode\vibenode.ico,0"
$Shortcut.WindowStyle = 7
$Shortcut.Save()
```

This starts the server minimized and opens the browser automatically.

## Notes

- Sessions are read from `~/.claude/projects/`
- Input is sent to Claude terminals via PowerShell SendKeys
- No data leaves your machine
