"""
render_entrypoint.py
Render Web Service Entrypoint for Strategy v3.2.5 Autonomous Cloud Runner.
Starts a minimal HTTP healthcheck server on $PORT.
Defaults to STANDBY mode (GitHub Actions is Primary) to prevent duplicate alerts.
Set RENDER_SCAN_ENABLED=1 to activate active scanning on Render.
"""
import os
import sys
import time
import threading
import json
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import cloud runner logic
from scripts.v325_nse_cloud_runner import run_0935_scan, run_monitoring_loop, get_ist_now, send_telegram

IST = timezone(timedelta(hours=5, minutes=30))
RENDER_SCAN_ENABLED = os.environ.get("RENDER_SCAN_ENABLED", "0") == "1"
START_TIME = datetime.now(IST)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        now_dt = datetime.now(IST)
        now_ist = now_dt.strftime("%Y-%m-%d %H:%M:%S IST")
        uptime_sec = int((now_dt - START_TIME).total_seconds())
        mode = "active" if RENDER_SCAN_ENABLED else "standby"
        primary = "Render" if RENDER_SCAN_ENABLED else "GitHub Actions"
        git_commit = os.environ.get("RENDER_GIT_COMMIT", "manual-or-local")
        git_branch = os.environ.get("RENDER_GIT_BRANCH", "main")
        service_id = os.environ.get("RENDER_SERVICE_ID", "v325-trading-runner")

        payload = {
            "status": "running",
            "service": "Strategy v3.2.5 Runner",
            "version": "v3.2.5",
            "mode": mode,
            "primary": primary,
            "git_commit": git_commit,
            "git_branch": git_branch,
            "service_id": service_id,
            "uptime_seconds": uptime_sec,
            "time_ist": now_ist,
        }
        resp = json.dumps(payload, indent=2)
        self.wfile.write(resp.encode("utf-8"))

    def log_message(self, format, *args):
        return

def trading_daemon_loop():
    mode = "ACTIVE" if RENDER_SCAN_ENABLED else "STANDBY (GitHub Actions is Primary)"
    print(f"[DAEMON] Strategy v3.2.5 Trading Daemon started in mode: {mode}", flush=True)

    if not RENDER_SCAN_ENABLED:
        print("[DAEMON] Render scanning disabled to prevent duplicate alerts. Running health server only.", flush=True)
        return

    last_scanned_date = None

    while True:
        try:
            now_ist = get_ist_now()
            today = now_ist.date()
            weekday = now_ist.weekday()  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday

            # Check if trading day Mon-Fri
            if weekday < 5:
                target_hour = 9
                target_min = 34

                if (now_ist.hour == target_hour and now_ist.minute >= target_min) or (now_ist.hour > target_hour and now_ist.hour < 15):
                    if last_scanned_date != today:
                        print(f"[DAEMON] Triggering 09:35 NSE scan for {today} at {now_ist}", flush=True)
                        cand = run_0935_scan(today)
                        last_scanned_date = today
                        if cand:
                            print(f"[DAEMON] Candidate locked: {cand['symbol']}. Starting monitoring loop.", flush=True)
                            run_monitoring_loop(cand, is_test=False)
                        else:
                            print("[DAEMON] No candidate locked or market not open yet.", flush=True)

            time.sleep(30)
        except Exception as e:
            print(f"[DAEMON ERROR] {e}", flush=True)
            time.sleep(60)

def main():
    port = int(os.environ.get("PORT", 10000))
    print(f"[RENDER] Starting HTTP server on port {port}...", flush=True)

    # Start background trading thread
    t = threading.Thread(target=trading_daemon_loop, daemon=True)
    t.start()

    # Serve HTTP for Render health checks
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()
