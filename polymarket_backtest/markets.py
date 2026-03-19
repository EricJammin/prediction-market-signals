"""
Target markets for the Polymarket whale detection backtest.

Focus: Venezuela invasion (Jan 3, 2026) and US strikes on Iran (Feb 28, 2026).
All condition_ids verified against Gamma API via tag_id=102304 browse.

Event timeline:
  - Jan 3,  2026: US/Venezuela invasion begins
  - Feb 6,  2026: US-Iran meeting occurs
  - Feb 28, 2026: Iran strikes Israel; US begins strikes on Iran
  - Mar 1+, 2026: Continued US strikes on Iran; Khamenei situation develops
"""

MARKETS = [
    # ── VENEZUELA ─────────────────────────────────────────────────────────────
    # Market deadline was Dec 31 2025 — resolved NO because invasion was Jan 3
    # Pre-event whale window: Oct–Dec 2025
    {
        "slug":         "will-the-us-invade-venezuela-in-2025",
        "condition_id": "0x62f31557b0e55475789b57a94ac385ee438ef9f800117fd1b823a0797b1fdd68",
        "description":  "Will the US invade Venezuela in 2025?",
        "category":     "geopolitics",
        "event_date":   "2026-01-03",
        "resolution":   "NO",  # market expired before invasion (Dec 31 deadline)
        "volume_usdc":  2_764_332,
        "validation_market": True,
        "notes": "Invasion happened Jan 3 2026, after market deadline of Dec 31 2025. "
                 "Whale activity in late 2025 may reflect foreknowledge.",
    },

    # ── IRAN–ISRAEL STRIKE (Feb 28, 2026) ─────────────────────────────────────
    {
        "slug":         "iran-strike-on-israel-by-february-28",
        "condition_id": "0xb3ebf217cf2f393a66030c072b04b893268506923e01b23f1bcf3504c3d319c2",
        "description":  "Iran strike on Israel by February 28, 2026",
        "category":     "geopolitics",
        "event_date":   "2026-02-28",
        "resolution":   "YES",
        "volume_usdc":  5_006_329,
        "validation_market": True,
        "notes": "Resolved YES on Feb 28. Pre-event whale activity in Feb 20-27 window.",
    },

    # ── US–IRAN MEETING (Feb 6, 2026) ─────────────────────────────────────────
    {
        "slug":         "us-x-iran-meeting-by-february-6-2026",
        "condition_id": "0x41e47408f8ab39b46a9d9e3c9b15ebd62f1d795eb072ff46df3d376c09eb583e",
        "description":  "Will the US and Iran hold a direct meeting by Feb 6, 2026?",
        "category":     "geopolitics",
        "event_date":   "2026-02-06",
        "resolution":   "YES",
        "volume_usdc":  22_688_334,
        "validation_market": True,
        "notes": "High-volume market ($22.7M). Pre-event whale window: late Jan 2026.",
    },

    # ── US NEXT STRIKE ON IRAN — WEEK OF MAR 1–7 ──────────────────────────────
    {
        "slug":         "will-the-us-next-strike-iran-during-the-week-of-march-1-7",
        "condition_id": "0x5cb20a760bc2bba3b87fae547a25cbac73f702abfa30e4a801e65f8b9f15d8ff",
        "description":  "Will the US next strike Iran during the week of Mar 1–7, 2026?",
        "category":     "geopolitics",
        "event_date":   "2026-03-01",
        "resolution":   "YES",
        "volume_usdc":  2_542_437,
        "validation_market": False,
        "notes": "Complete trade history available (913 trades, Feb 16–28). "
                 "Resolved YES Mar 1. Best market for clean pre-event whale detection.",
    },

    # ── KHAMENEI OUT BY MARCH 31 ───────────────────────────────────────────────
    {
        "slug":         "khamenei-out-as-supreme-leader-of-iran-by-march-31",
        "condition_id": "0x70909f0ba8256a89c301da58812ae47203df54957a07c7f8b10235e877ad63c2",
        "description":  "Will Khamenei be out as Supreme Leader of Iran by March 31, 2026?",
        "category":     "geopolitics",
        "event_date":   "2026-03-31",
        "resolution":   None,
        "volume_usdc":  63_238_698,
        "validation_market": False,
        "notes": "Highest-volume Iran market ($63M). Peaked around Feb 28 US strikes.",
    },

    # ── IRANIAN REGIME FALL BY MARCH 31 ───────────────────────────────────────
    {
        "slug":         "will-the-iranian-regime-fall-by-march-31",
        "condition_id": "0x61ce3773237a948584e422de72265f937034af418a8b703e3a860ea62e59ff36",
        "description":  "Will the Iranian regime fall by March 31, 2026?",
        "category":     "geopolitics",
        "event_date":   "2026-03-31",
        "resolution":   None,
        "volume_usdc":  31_489_758,
        "validation_market": False,
    },

    # ── KHAMENEI OUT IN 2025 (resolved) ───────────────────────────────────────
    {
        "slug":         "khamenei-out-as-supreme-leader-of-iran-in-2025",
        "condition_id": "0x1b6f76e5b8587ee896c35847e12d11e75290a8c3934c5952e8a9d6e4c6f03cfa",
        "description":  "Will Khamenei be out as Supreme Leader of Iran in 2025?",
        "category":     "geopolitics",
        "event_date":   "2025-12-31",
        "resolution":   "NO",
        "volume_usdc":  10_993_956,
        "validation_market": False,
        "notes": "Resolved NO Dec 31 2025. Pre-event data Dec 9–31 2025.",
    },

    # ── IRANIAN REGIME FALL IN 2025 (resolved) ────────────────────────────────
    {
        "slug":         "will-the-iranian-regime-fall-in-2025",
        "condition_id": "0xd5e2c76090cc15dc1e613fd61b9a2cee9b76c9097a6c313f256df55d2df5149c",
        "description":  "Will the Iranian regime fall in 2025?",
        "category":     "geopolitics",
        "event_date":   "2025-12-31",
        "resolution":   "NO",
        "volume_usdc":  4_992_689,
        "validation_market": False,
        "notes": "Resolved NO Dec 31 2025. Data covers Nov 6 – Jan 1.",
    },
]

# For --dry-run: first N markets
DRY_RUN_COUNT = 2
