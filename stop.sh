#!/usr/bin/env bash
# Stop Quant Web Console
pkill -f "python.*-m backend" 2>/dev/null && echo "Backend stopped" || echo "Backend not running"
pkill -f "next.*dev" 2>/dev/null && echo "Frontend stopped" || echo "Frontend not running"
