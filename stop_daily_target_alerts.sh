#!/usr/bin/env bash
set +e
screen -S sniper-targets -X quit 2>/dev/null || true
screen -wipe >/dev/null 2>&1 || true
echo "✅ sniper-targets stopped"
screen -ls || true
