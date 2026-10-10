#!/usr/bin/env python3
"""Break-and-restore check (audit Phase 8): re-introduce each Critical bug in a TEMPORARY copy and
confirm the matching tests fail. The real files are never edited.

    py -3.11 tools/break_restore.py            (needs Node, and Java 21+ via JAVA_HOME for the rules tests)

Exit code 0 only if every re-introduced bug was caught.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KIOSK = Path(__file__).resolve().parents[1]
WEB = Path(os.environ.get("ECOREWARD_WEB_DIR") or KIOSK.parents[1] / "CAPSTONE-1-WEB-main" / "CAPSTONE-1-WEB-main")
WIN = os.name == "nt"


def failed_tests(r):
    """Failing tests reported by unittest or node --test; -1 if the suite did not run at all."""
    out = (r.stdout or "") + (r.stderr or "")
    node = re.findall(r"fail (\d+)", out)
    py = re.search(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", out)
    if node:
        return int(node[-1])
    if re.search(r"^Ran \d+ test", out, re.M):
        return (int(py.group(1) or 0) + int(py.group(2) or 0)) if py else 0
    return -1


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def mutate(text, old, new):
    if old not in text:
        raise SystemExit(f"[ERROR] mutation anchor not found (code changed?): {old[:60]!r}")
    return text.replace(old, new, 1)


def kiosk_case(old, new, test_pattern, file="app.py", tests="test_kiosk_phase1.py"):
    """Mutate one file of a temporary kiosk copy (app.py or a module under kiosk/) and run matching tests."""
    def go(broken=True):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for folder in ("config", "kiosk", "assets", "tests"):
                shutil.copytree(KIOSK / folder, d / folder, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy(KIOSK / "kiosk_firestore.py", d)
            shutil.copy(KIOSK / "app.py", d)
            target = d / file
            src = target.read_text(encoding="utf-8")
            target.write_text(mutate(src, old, new) if broken else src, encoding="utf-8")
            env = {**os.environ, "KIOSK_APP_DIR": str(d)}
            return failed_tests(run([sys.executable, "-I", "-m", "unittest", "discover", "-s", str(d / "tests"),
                                     "-p", tests, "-k", test_pattern], env=env, cwd=d))
    return go


def rules_case(old, new):
    def go(broken=True):
        with tempfile.TemporaryDirectory() as d:
            rules = Path(d) / "firestore.rules"
            src = (KIOSK / "firebase/firestore.rules").read_text(encoding="utf-8")
            rules.write_text(mutate(src, old, new) if broken else src, encoding="utf-8")
            env = {**os.environ, "RULES_FILE": str(rules)}
            return failed_tests(run("npx firebase emulators:exec --config ../firebase.json --only firestore "
                                    "--project demo-ecoreward \"node --test\"", shell=True, cwd=KIOSK / "firebase/tests", env=env))
    return go


def web_hook_case(replacements):
    def go(broken=True):
        hook = WEB / "lib/useLoadTimeout.ts"
        mutant = WEB / "lib/useLoadTimeout.mutant.ts"          # temporary, deleted below
        text = hook.read_text(encoding="utf-8")
        for old, new in (replacements if broken else []):
            text = mutate(text, old, new)
        mutant.write_text(text, encoding="utf-8")
        try:
            return failed_tests(run("node --test lib/useLoadTimeout.test.ts", shell=True, cwd=WEB,
                                    env={**os.environ, "HOOK_FILE": "./useLoadTimeout.mutant.ts"}))
        finally:
            mutant.unlink(missing_ok=True)
    return go


CASES = [
    ("C-03", "SPACE is not refused while Firebase is offline",
     kiosk_case("flow.set_context(online=db is not None,", "flow.set_context(online=True,", "OfflineTests")),
    ("C-04", "kiosk never notices the phone's scan",
     kiosk_case("_scan_event.set()\n    _watcher", "pass\n    _watcher", "ScanDetectionTests")),
    ("C-05", "web page flips to 'Failed to load' after 15 s although data arrived",
     web_hook_case([("}, [loading, ms]);", "}, []); // eslint-disable-line"), ("return loading && timedOut;", "return timedOut;")])),
    ("C-01", "a customer can change their own role to admin",
     rules_case("changedKeys().hasOnly(['username', 'points', 'lifetimePoints',",
                "changedKeys().hasOnly(['role', 'username', 'points', 'lifetimePoints',")),
    ("C-02", "the kiosk identity can read customers' user documents",
     rules_case("allow get:  if isOwner(uid) || isAdmin();", "allow get:  if isOwner(uid) || isAdmin() || isKiosk();")),
    ("K-01", "token dates written as ISO strings (rules reject / expiry unchecked)",
     kiosk_case('"expiresAt": expires_at,', '"expiresAt": expires_at.isoformat(),', "TokenContractTests",
                tests="test_kiosk_product.py")),
    ("K-07", "the token write runs on the UI thread and freezes the screen",
     kiosk_case('threading.Thread(target=fn, daemon=True, name="kiosk-worker").start()', "fn()",
                "NonBlockingTests", tests="test_kiosk_product.py")),
    ("K-10", "the scan watcher keeps polling after the QR ends",
     kiosk_case("            _watcher.unsubscribe()\n        except Exception:\n            pass\n    _watcher, _active_token",
                "            pass\n        except Exception:\n            pass\n    _watcher, _active_token",
                "QrLifetimeInTheLoop", tests="test_kiosk_product.py")),
    ("K-06", "a queued servo command is replayed after the ESP32 is plugged back in",
     kiosk_case("            self._drain_tx()                                 # never replay stale commands",
                "            pass", "unplug", file="kiosk/hardware.py", tests="test_kiosk_flow.py")),
    ("H-02", "a QR token can be claimed twice",
     rules_case("&& resource.data.get('isUsed', false) == false\n                        && resource.data.usedBy.size() == 0",
                "&& true")),
]


def main():
    caught_all = True
    print(f"{'item':6} {'re-introduced bug':62} {'unbroken':>9} {'broken':>7}  result")
    for item, what, go in CASES:
        control, broken = go(broken=False), go(broken=True)
        caught = control == 0 and broken > 0
        caught_all &= caught
        print(f"{item:6} {what:62} {control:>4} fail {broken:>3} fail  {'CAUGHT' if caught else 'MISSED'}")
    print("\nAll re-introduced bugs were caught." if caught_all else "\nSOME BUGS WERE NOT CAUGHT.")
    sys.exit(0 if caught_all else 1)


if __name__ == "__main__":
    main()
