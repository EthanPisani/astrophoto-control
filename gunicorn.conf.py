# gunicorn.conf.py — production settings for AstroCap.
#
# Values are overridable via environment variables:
#   GUNICORN_WORKERS   (default: 2)
#   GUNICORN_THREADS   (default: 4)
#   GUNICORN_TIMEOUT   (default: 120)
#   ASTROCAP_PORT      (default: 7777)

import os

bind = f"0.0.0.0:{os.environ.get('ASTROCAP_PORT', '7777')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))

# gthread: native threads per worker — good for I/O-bound camera calls.
# (gevent would need monkey-patching the entire app; not worth it here.)
worker_class = "gthread"

# Log to stdout so Docker picks it up.
accesslog = "-"
errorlog = "-"
capture_output = True

# Preload the app before forking workers.  This shares the catalog
# data structures (loaded once) across workers, saving ~200 MB RAM.
#
# NOTE: preload_app means any daemon threads created at import time
# will be DEAD after fork (POSIX only preserves the forking thread).
# app.py's SyncWorker uses a lazy proxy that detects the fork via PID
# change and re-creates itself in each worker, so this is safe.
preload_app = True
