# Antigravity SQLite Database Inspector

A lightweight, zero-dependency local web dashboard and diagnosis tool tailored specifically for **Google Antigravity** SQLite databases (`~/.gemini/antigravity/conversations/*.db`).

## Features
- **Conversation Browser**: Discovers and inspects all conversation databases across Desktop and CLI instances.
- **Bloat Scanner 🚨**: 1-click detection of oversized steps (`> 1 MB`) that threaten WebChannel remote connection stability.
- **1-Click Safe Prune**: Automatically prunes bloated file diffs and tool snapshots while preserving 100% of conversation context, user prompts, agent reasoning, and history.
- **Protobuf Wire Decoder**: Inspects raw binary BLOBs in the `steps` table and unpacks nested protobuf fields down to wire types and string previews.
- **Interactive SQL Console**: Execute ad-hoc queries, check integrity (`PRAGMA integrity_check`), and inspect table schemas.
- **Database Maintenance**: 1-click `VACUUM` to eliminate page fragmentation and reclaim disk space immediately.

## Directory Structure
- `server.py`: Standalone Python web server and REST API (pure Python standard library, zero pip dependencies).
- `~/.local/bin/antigravity-db-inspector`: Global CLI shortcut command.

## Quick Start
Open a terminal and run:
```bash
antigravity-db-inspector
```
Then open your browser to [http://localhost:8990](http://localhost:8990).
