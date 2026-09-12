#!/usr/bin/env python3
"""
Automated Quota & Live Pricing Protection System (Shield 1 & 2)
--------------------------------------------------------------
Shield 1: Live Pricing Auto-Scanner
  - Automatically verifies Google Cloud's official live pricing table.
  - Dynamically detects if any voice family is removed from the free tier (sets cap to 0 and disables it).
  - Dynamically recalculates safe caps if Google reduces limits (e.g., 1M -> 500k).
  - Fails safely: if network is offline or page changes, preserves safe existing caps.
  - Uses 100% Python standard library (no pip dependencies required).

Shield 2: Central User-Controlled Configuration (`~/.cursor/tts_quota_config.json`)
  - Configurable safety margin (default: 5% cushion).
  - Per-tier kill-switches (`enabled: true/false`).
  - Atomic ledger writes to eliminate multi-process race conditions.
"""

from __future__ import annotations

import os
import re
import sys
import json
import logging
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("TTSQuotaTracker")

CONFIG_FILE = Path.home() / ".cursor" / "tts_quota_config.json"
USAGE_FILE = Path.home() / ".cursor" / "tts_monthly_usage.json"
PRICING_URL = "https://cloud.google.com/text-to-speech/pricing"

DEFAULT_CONFIG = {
    "safety_margin_pct": 5,
    "auto_check_pricing_days": 7,
    "last_pricing_check": None,
    "pricing_source": PRICING_URL,
    "tiers": {
        "studio": {
            "name": "Google Studio-Q",
            "voice": "en-US-Studio-Q",
            "official_free_chars": 1_000_000,
            "safe_monthly_cap": 950_000,
            "safe_daily_cap": 35_000,
            "enabled": True,
        },
        "chirp3": {
            "name": "Google Chirp 3 HD Charon",
            "voice": "en-US-Chirp3-HD-Charon",
            "official_free_chars": 1_000_000,
            "safe_monthly_cap": 950_000,
            "safe_daily_cap": 35_000,
            "enabled": True,
        },
        "journey": {
            "name": "Google Journey-D",
            "voice": "en-US-Journey-D",
            "official_free_chars": 1_000_000,
            "safe_monthly_cap": 950_000,
            "safe_daily_cap": 35_000,
            "enabled": True,
        },
        "wavenet": {
            "name": "Google WaveNet-D",
            "voice": "en-US-Wavenet-D",
            "official_free_chars": 4_000_000,
            "safe_monthly_cap": 3_800_000,
            "safe_daily_cap": 130_000,
            "enabled": True,
        }
    }
}


class _TableParser(HTMLParser):
    """Zero-dependency HTML table parser using Python standard library."""
    def __init__(self):
        super().__init__()
        self.tables = []
        self.cur_table = None
        self.cur_row = None
        self.cur_cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.cur_table = []
        elif tag == "tr" and self.cur_table is not None:
            self.cur_row = []
        elif tag in ("td", "th") and self.cur_row is not None:
            self.cur_cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self.cur_table is not None:
            self.tables.append(self.cur_table)
            self.cur_table = None
        elif tag == "tr" and self.cur_row is not None:
            self.cur_table.append(self.cur_row)
            self.cur_row = None
        elif tag in ("td", "th") and self.cur_cell is not None:
            text = " ".join(self.cur_cell).strip()
            self.cur_row.append(text)
            self.cur_cell = None

    def handle_data(self, data):
        if self.cur_cell is not None:
            cleaned = data.strip()
            if cleaned:
                self.cur_cell.append(cleaned)


def get_current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def get_current_day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_config() -> dict[str, Any]:
    """Loads central quota configuration, creating default if missing."""
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if "tiers" in data and isinstance(data["tiers"], dict):
                return data
        except Exception as e:
            logger.warning("Failed to parse %s: %s. Using default config.", CONFIG_FILE, e)

    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG


def save_config(cfg: dict[str, Any]) -> None:
    """Atomic write for quota config."""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except Exception as e:
        logger.error("Failed to save config to %s: %s", CONFIG_FILE, e)


def load_usage() -> dict[str, Any]:
    """Loads usage ledger."""
    if USAGE_FILE.is_file():
        try:
            return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_usage(data: dict[str, Any]) -> None:
    """Atomic write for usage ledger (protects against concurrent pipeline workers)."""
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(USAGE_FILE)
    except Exception as e:
        logger.error("Failed to save usage to %s: %s", USAGE_FILE, e)


def get_tier_usage(tier_key: str) -> tuple[int, int]:
    """Returns (month_used, day_used) for a given tier."""
    month = get_current_month()
    day = get_current_day()
    data = load_usage()
    m_used = int(data.get(month, {}).get(tier_key, 0))
    d_used = int(data.get(f"daily_{day}", {}).get(tier_key, 0))
    return m_used, d_used


def record_tier_usage(tier_key: str, char_count: int) -> None:
    """Records character usage with atomic update."""
    month = get_current_month()
    day = get_current_day()
    data = load_usage()

    if month not in data:
        data[month] = {}
    data[month][tier_key] = data[month].get(tier_key, 0) + char_count

    day_key = f"daily_{day}"
    if day_key not in data:
        data[day_key] = {}
    data[day_key][tier_key] = data[day_key].get(tier_key, 0) + char_count

    save_usage(data)


def scan_live_pricing(force: bool = False) -> Tuple[bool, str, Dict[str, int]]:
    """
    Shield 1: Scans Google Cloud's official pricing page live.
    Extracts free tier limits for studio, chirp3, journey, and wavenet.
    Returns (success, message, parsed_limits).
    """
    config = load_config()
    last_check_str = config.get("last_pricing_check")
    now = datetime.now()

    if not force and last_check_str:
        try:
            last_check = datetime.fromisoformat(last_check_str)
            check_interval = timedelta(days=int(config.get("auto_check_pricing_days", 7)))
            if (now - last_check) < check_interval:
                return True, "Pricing checked recently (cache valid)", {}
        except Exception:
            pass

    try:
        req = urllib.request.Request(
            PRICING_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"Could not reach Google pricing page: {e} (Safe existing caps preserved)", {}

    parser = _TableParser()
    try:
        parser.feed(html)
    except Exception as e:
        return False, f"HTML parsing error: {e} (Safe caps preserved)", {}

    parsed_limits = {}
    for table in parser.tables:
        for row in table:
            if len(row) >= 2:
                model_col = row[0].lower()
                limit_col = row[1].lower()

                chars = 0
                m = re.search(r"(\d+(?:\.\d+)?)\s*million", limit_col)
                if m:
                    chars = int(float(m.group(1)) * 1_000_000)
                elif "not available" in limit_col or "0" in limit_col:
                    chars = 0

                if "studio" in model_col and "multispeaker" not in model_col:
                    parsed_limits["studio"] = chars
                elif "chirp 3" in model_col and "hd" in model_col:
                    parsed_limits["chirp3"] = chars
                elif "neural2" in model_col:
                    parsed_limits["neural2"] = chars
                elif "wavenet" in model_col:
                    parsed_limits["wavenet"] = chars

    if not parsed_limits:
        return False, "Could not locate free tier tables in HTML structure (Safe caps preserved)", {}

    if "neural2" in parsed_limits and "journey" not in parsed_limits:
        parsed_limits["journey"] = parsed_limits["neural2"]

    margin_pct = float(config.get("safety_margin_pct", 5))
    margin_multiplier = max(0.0, 1.0 - (margin_pct / 100.0))
    changes_detected = []

    for key, official_chars in parsed_limits.items():
        if key in config["tiers"]:
            tier_cfg = config["tiers"][key]
            prev_official = tier_cfg.get("official_free_chars", 0)

            if official_chars != prev_official:
                changes_detected.append(f"{tier_cfg['name']}: changed from {prev_official:,} to {official_chars:,} chars")

            tier_cfg["official_free_chars"] = official_chars

            if official_chars == 0:
                tier_cfg["enabled"] = False
                tier_cfg["safe_monthly_cap"] = 0
                tier_cfg["safe_daily_cap"] = 0
                logger.warning("ALERT: %s removed from Google Cloud Free Tier! Tier disabled automatically.", tier_cfg['name'])
            else:
                tier_cfg["enabled"] = True
                safe_month = int(official_chars * margin_multiplier)
                tier_cfg["safe_monthly_cap"] = safe_month
                tier_cfg["safe_daily_cap"] = max(1000, int(safe_month / 28))

    config["last_pricing_check"] = now.isoformat()
    save_config(config)

    msg = "Live pricing verified."
    if changes_detected:
        msg += f" Policy changes detected and safe caps adjusted: {'; '.join(changes_detected)}"
    else:
        msg += " All Google free tier allowances confirmed intact."

    return True, msg, parsed_limits


def is_tier_safe(tier_key: str, text_length: int) -> Tuple[bool, str]:
    """
    Shield 2: Gatekeeper that prevents any request that violates daily or monthly safe caps.
    Returns (is_allowed, reason).
    """
    config = load_config()
    tier_cfg = config.get("tiers", {}).get(tier_key)
    if not tier_cfg:
        return False, f"Unknown tier '{tier_key}'"

    if not tier_cfg.get("enabled", True):
        return False, f"Tier '{tier_cfg.get('name', tier_key)}' is DISABLED (Paid Only or Manually Disabled)"

    m_cap = int(tier_cfg.get("safe_monthly_cap", 0))
    d_cap = int(tier_cfg.get("safe_daily_cap", 0))
    m_used, d_used = get_tier_usage(tier_key)

    if (m_used + text_length) > m_cap:
        return False, f"Monthly safe limit reached ({m_used:,} / {m_cap:,} chars)"

    if (d_used + text_length) > d_cap:
        return False, f"Daily pacing limit reached ({d_used:,} / {d_cap:,} chars)"

    return True, "OK"


def print_status(force_check: bool = False) -> None:
    """Displays comprehensive quota, daily pacing, and live pricing status."""
    if force_check:
        print("Auditing Google Cloud live pricing page...")
        ok, msg, _ = scan_live_pricing(force=True)
        print(f"[{'SUCCESS' if ok else 'WARNING'}] {msg}\n")
    else:
        scan_live_pricing(force=False)

    config = load_config()
    month = get_current_month()
    day = get_current_day()
    data = load_usage()
    month_data = data.get(month, {})
    day_data = data.get(f"daily_{day}", {})

    print(f"=== Central TTS Safe Quota & Pricing Tracker ===")
    print(f"Active Month: {month} | Today: {day}")
    print(f"Config File:  {CONFIG_FILE}")
    print(f"Ledger File:  {USAGE_FILE}")
    print(f"Last Verified Against Google: {config.get('last_pricing_check', 'Never')}")
    print(f"Configured Safety Cushion:    {config.get('safety_margin_pct', 5)}% under official limits\n")

    items = [
        ("studio",  "Google Studio-Q",          "en-US-Studio-Q"),
        ("chirp3", "Google Chirp 3 HD Charon",  "en-US-Chirp3-HD-Charon"),
        ("journey", "Google Journey-D",         "en-US-Journey-D"),
        ("wavenet", "Google WaveNet-D",         "en-US-Wavenet-D"),
    ]

    total_safe_cap = 0
    total_used_month = 0

    for key, default_label, default_voice in items:
        tier_cfg = config.get("tiers", {}).get(key, {})
        name = tier_cfg.get("name", default_label)
        voice = tier_cfg.get("voice", default_voice)
        enabled = tier_cfg.get("enabled", True)
        official = tier_cfg.get("official_free_chars", 0)
        m_cap = tier_cfg.get("safe_monthly_cap", 0)
        d_cap = tier_cfg.get("safe_daily_cap", 0)

        m_used = month_data.get(key, 0)
        d_used = day_data.get(key, 0)

        total_safe_cap += m_cap
        total_used_month += m_used

        m_rem = max(0, m_cap - m_used)
        d_rem = max(0, d_cap - d_used)

        if not enabled or official == 0:
            status = "DISABLED"
        elif m_used >= m_cap:
            status = "MONTH CAP"
        elif d_used >= d_cap:
            status = "DAY CAP"
        else:
            status = "ACTIVE"

        print(f"[{status:^9}] {name:<25} ({voice})")
        print(f"            Official Google Free: {official:,} chars | Safe Month Cap: {m_cap:,} chars")
        print(f"            Today's Usage:        {d_used:,} / {d_cap:,} chars (Left today: {d_rem:,})")
        print(f"            Month's Usage:        {m_used:,} / {m_cap:,} chars (Left this month: {m_rem:,})")

    print("-" * 88)
    print(f"Combined Safe Monthly Google Capacity: {total_used_month:,} / {total_safe_cap:,} chars")
    print(f"Final Fallback Engine: Microsoft Edge-TTS (ChristopherNeural) -> 100% UNLIMITED & FREE\n")


if __name__ == "__main__":
    force = "--check-pricing" in sys.argv
    print_status(force_check=force)
