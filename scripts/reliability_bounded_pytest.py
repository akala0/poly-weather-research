"""One full-suite attempt with a hard outer limit; terminate only our child tree.

No daemon, scheduler, external request, or environment repair operations.
Output is printed for the caller's evidence log, never written to data/.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
           "-o", "faulthandler_timeout=15"]
started = datetime.now(UTC).isoformat()
child = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
print(json.dumps({"command": command, "started_at": started, "owned_test_pid": child.pid}), flush=True)
try:
    output, _ = child.communicate(timeout=60)
    print(output, flush=True)
    print(json.dumps({"exit_code": child.returncode, "timed_out": False}), flush=True)
    sys.exit(child.returncode)
except subprocess.TimeoutExpired:
    if os.name == "nt":
        cleanup = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                 capture_output=True, text=True, timeout=10)
        print(json.dumps({"owned_tree_cleanup_exit": cleanup.returncode}), flush=True)
    else:
        child.kill()
    output, _ = child.communicate(timeout=10)
    print(output, flush=True)
    print(json.dumps({"exit_code": 124, "timed_out": True, "owned_test_pid": child.pid}), flush=True)
    sys.exit(124)
