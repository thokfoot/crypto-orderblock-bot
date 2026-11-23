"""
render_entrypoint.py
Render Web Service Entrypoint for Strategy v3.2.5 Autonomous Cloud Runner.
Starts a minimal HTTP healthcheck server on $PORT and runs the trading engine in a background thread.
"""
import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import cloud runner logic
from scripts.v325_nse_cloud_runner import run_0935_scan, run_monitoring_loop, get_ist_now, send_telegram

IST = timezone(timedelta(hours=5, minutes=30))

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        resp = f'{{"status": "running", "service": "Strategy v3.2.5 Runner", "time_ist": "{now_ist}"}}'
        self.wfile.write(resp.encode("utf-8"))

    def log_message(self, format, *args):
        # Silence routine access logs
        return

def trading_daemon_loop():
    print("[DAEMON] Strategy v3.2.5 Trading Daemon started in background.", flush=True)
    send_telegram("🟢 <b>Render Cloud Service Online</b>\nStrategy v3.2.5 background runner initialized.")

    last_scanned_date = None

    while True:
        try:
            now_ist = get_ist_now()
            today = now_ist.date()
            weekday = now_ist.weekday()  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday

            # Check if trading day Mon-Fri
            if weekday < 5:
                # Target window: 09:34 to 09:37 IST
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
