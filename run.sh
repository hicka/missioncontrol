#!/usr/bin/env bash
# Open the native macOS host. Background services belong to this app.
set -e
cd "$(dirname "$0")"

exec ./venv/bin/python desktop.py
