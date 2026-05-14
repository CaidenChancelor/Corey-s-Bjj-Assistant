"""Regression check for _infer_injury_time_bucket precedence.

Run with:
    python3 tests/test_injury_time_bucket.py

Covers the bug Codex QA caught: generic "pm" matching ahead of evening words
caused "7:45 PM competition class" to bucket as afternoon.
"""
import os
import sys

# Import bot.py directly without triggering scheduler / network calls.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
sys.path.insert(0, ROOT)

# Stub minimal env so bot.py imports cleanly without real credentials.
os.environ.setdefault("DB_PATH", os.path.join(ROOT, "tests", "_throwaway.db"))

from bot import _infer_injury_time_bucket, scheduler  # noqa: E402


CASES = [
    # (when_happened, created_at_or_None, expected_bucket)
    # --- explicit night beats everything ---
    ("got hurt late night during open mat", None, "night"),
    ("late roll", None, "night"),

    # --- evening words win over generic pm ---
    ("7:45 PM competition class", None, "evening"),
    ("evening class 7 PM", None, "evening"),
    ("open mat at 8pm", None, "evening"),
    ("rolls after class", None, "evening"),
    ("competition class felt rough", None, "evening"),

    # --- 5-9 PM with no words → evening ---
    ("hurt around 6 pm", None, "evening"),
    ("hurt around 7:45 PM", None, "evening"),
    ("9pm", None, "evening"),
    ("5pm session", None, "evening"),

    # --- 10/11 PM → night ---
    ("10 pm wind down", None, "night"),
    ("10:30pm wind down", None, "night"),
    ("11pm late", None, "night"),

    # --- afternoon words ---
    ("bruno private", None, "afternoon"),
    ("lunch session got tweaked", None, "afternoon"),
    ("noon roll", None, "afternoon"),
    ("midday drill", None, "afternoon"),

    # --- 12-4 PM with no words → afternoon ---
    ("1pm", None, "afternoon"),
    ("2:15 PM", None, "afternoon"),
    ("4 PM", None, "afternoon"),
    ("12pm warmup", None, "afternoon"),

    # --- morning words ---
    ("morning drilling tweaked it", None, "morning"),
    ("s&c with roy", None, "morning"),
    ("strength session", None, "morning"),

    # --- 1-11 AM → morning ---
    ("6am drill", None, "morning"),
    ("6:30am drill", None, "morning"),
    ("11 am stretch", None, "morning"),

    # --- 12 AM → night ---
    ("12 am late roll", None, "night"),

    # --- "am"/"pm" not spoofed by casual phrasing ---
    ("I am hurt", "2026-04-29T14:30:00", "afternoon"),  # falls back to created_at hour 14 → afternoon
    ("spammed too hard", "2026-04-29T06:00:00", "morning"),  # falls back to 06 → morning

    # --- pure fallback to created_at ---
    (None, "2026-04-29T08:00:00", "morning"),
    ("", "2026-04-29T13:00:00", "afternoon"),
    (None, "2026-04-29T19:00:00", "evening"),
    (None, "2026-04-29T23:00:00", "night"),
    (None, "2026-04-29T02:00:00", "night"),

    # --- bad created_at falls through to None ---
    (None, "garbage", None),
    (None, None, None),
]


def main():
    failures = []
    for when, created_at, expected in CASES:
        got = _infer_injury_time_bucket(when, created_at)
        ok = got == expected
        marker = "PASS" if ok else "FAIL"
        line = f"[{marker}] when={when!r:<48} created_at={created_at!r:<32} expected={expected!r:<10} got={got!r}"
        print(line)
        if not ok:
            failures.append((when, created_at, expected, got))

    print()
    if failures:
        print(f"{len(failures)} case(s) failed.")
        sys.exit(1)
    print(f"All {len(CASES)} cases passed.")


def cleanup():
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    db_path = os.environ["DB_PATH"]
    for path in (db_path, f"{db_path}-shm", f"{db_path}-wal"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
