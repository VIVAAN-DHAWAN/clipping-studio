"""Whop-ish campaign + earnings tracker.

Campaigns say what a clip pays per 1,000 views. Earnings rows are the numbers a
creator copies out of Whop / TikTok / YouTube analytics. Everything is stored in
plain JSON so it is easy to export or edit by hand.
"""
from __future__ import annotations

import csv
import io
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from state import get_state
from utils import now_iso, parse_percent, slugify

CURRENCY = "$"


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or uuid.uuid4().hex[:10]),
        "name": str(row.get("name") or "Untitled campaign").strip()[:90],
        "platform": str(row.get("platform") or "whop").strip().lower()[:24],
        "whop_url": str(row.get("whop_url") or "").strip()[:300],
        "rate": round(float(row.get("rate") or 0), 4),
        "per": int(row.get("per") or 1000),
        "status": str(row.get("status") or "active").strip().lower()[:20],
        "notes": str(row.get("notes") or "")[:400],
        "created_at": str(row.get("created_at") or now_iso()),
    }


def list_campaigns() -> list[dict[str, Any]]:
    return [dict(c) for c in get_state().campaigns]


def upsert_campaign(row: dict[str, Any]) -> dict[str, Any]:
    state = get_state()
    cleaned = _clean(row)
    for idx, existing in enumerate(state.campaigns):
        if existing.get("id") == cleaned["id"]:
            cleaned["created_at"] = existing.get("created_at", cleaned["created_at"])
            state.campaigns[idx] = cleaned
            state.save_campaigns()
            return dict(cleaned)
    state.campaigns.append(cleaned)
    state.save_campaigns()
    state.add_event("info", f"Campaign added: {cleaned['name']}")
    return dict(cleaned)


def delete_campaign(campaign_id: str) -> bool:
    state = get_state()
    before = len(state.campaigns)
    state.campaigns = [c for c in state.campaigns if c.get("id") != campaign_id]
    if len(state.campaigns) == before:
        return False
    state.save_campaigns()
    return True


def find_campaign(campaign_id: str) -> dict[str, Any] | None:
    for campaign in get_state().campaigns:
        if campaign.get("id") == campaign_id:
            return dict(campaign)
    return None


# ------------------------------------------------------------------ earnings
def _earning(row: dict[str, Any]) -> dict[str, Any]:
    campaign = find_campaign(str(row.get("campaign_id") or "")) or {}
    views = max(0, int(float(row.get("views") or 0)))
    rate = float(row.get("rate") or campaign.get("rate") or 0)
    per = int(campaign.get("per") or row.get("per") or 1000) or 1000
    computed = (views / per) * rate
    override = row.get("revenue_override")
    revenue = float(override) if override not in (None, "") else computed
    day = str(row.get("date") or date.today().isoformat())[:10]
    return {
        "id": str(row.get("id") or uuid.uuid4().hex[:10]),
        "campaign_id": campaign.get("id", ""),
        "campaign": campaign.get("name", str(row.get("campaign") or "")),
        "platform": campaign.get("platform", str(row.get("platform") or "")),
        "date": day,
        "views": views,
        "likes": max(0, int(float(row.get("likes") or 0))),
        "comments": max(0, int(float(row.get("comments") or 0))),
        "rate": round(rate, 4),
        "per": per,
        "revenue": round(max(0.0, revenue), 2),
        "note": str(row.get("note") or "")[:300],
        "clip_id": str(row.get("clip_id") or ""),
        "created_at": str(row.get("created_at") or now_iso()),
    }


def add_earning(row: dict[str, Any]) -> dict[str, Any]:
    state = get_state()
    entry = _earning(row)
    state.earnings.append(entry)
    state.earnings = state.earnings[-5000:]
    state.save_earnings()
    if entry.get("clip_id"):
        state.update_clip(entry["clip_id"], {"earnings_id": entry["id"]})
    return entry


def update_earning(earning_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    state = get_state()
    for idx, existing in enumerate(state.earnings):
        if existing.get("id") == earning_id:
            merged = {**existing, **row, "id": earning_id}
            state.earnings[idx] = _earning(merged)
            state.save_earnings()
            return dict(state.earnings[idx])
    return None


def delete_earning(earning_id: str) -> bool:
    state = get_state()
    before = len(state.earnings)
    state.earnings = [e for e in state.earnings if e.get("id") != earning_id]
    if len(state.earnings) == before:
        return False
    state.save_earnings()
    return True


# ------------------------------------------------------------------- summary
def summary() -> dict[str, Any]:
    state = get_state()
    rows = [dict(e) for e in state.earnings]
    campaigns = {c["id"]: c for c in state.campaigns}
    total_views = sum(int(e.get("views") or 0) for e in rows)
    total_revenue = round(sum(float(e.get("revenue") or 0) for e in rows), 2)

    by_campaign: dict[str, dict[str, Any]] = {}
    by_platform: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, float]] = {}
    for e in rows:
        key = e.get("campaign_id") or e.get("campaign") or "unassigned"
        bucket = by_campaign.setdefault(key, {
            "campaign": e.get("campaign") or "Unassigned",
            "platform": e.get("platform", ""),
            "views": 0, "revenue": 0.0, "clips": 0,
        })
        bucket["views"] += int(e.get("views") or 0)
        bucket["revenue"] = round(bucket["revenue"] + float(e.get("revenue") or 0), 2)
        bucket["clips"] += 1
        plat = (e.get("platform") or "other").lower()
        pbucket = by_platform.setdefault(plat, {"views": 0, "revenue": 0.0})
        pbucket["views"] += int(e.get("views") or 0)
        pbucket["revenue"] = round(pbucket["revenue"] + float(e.get("revenue") or 0), 2)
        day = str(e.get("date") or "")[:10]
        if day:
            dbucket = by_day.setdefault(day, {"views": 0.0, "revenue": 0.0})
            dbucket["views"] += int(e.get("views") or 0)
            dbucket["revenue"] = round(dbucket["revenue"] + float(e.get("revenue") or 0), 2)

    today = date.today()
    series = []
    for back in range(13, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        row = by_day.get(day, {"views": 0.0, "revenue": 0.0})
        series.append({"date": day, "views": int(row["views"]), "revenue": round(row["revenue"], 2)})

    best_day = max(by_day.items(), key=lambda kv: kv[1]["revenue"], default=("", {"revenue": 0.0}))
    rpm = round((total_revenue / total_views) * 1000, 2) if total_views else 0.0
    milestones = [100_000, 1_000_000, 10_000_000]
    projections = []
    for target in milestones:
        projections.append({
            "views": target,
            "label": f"{target // 1_000_000}M" if target >= 1_000_000 else f"{target // 1000}K",
            "payout": round((target / 1000.0) * rpm, 2) if rpm else 0.0,
        })

    return {
        "total_views": total_views,
        "total_revenue": total_revenue,
        "rpm": rpm,
        "clips_tracked": len(rows),
        "campaigns": len([c for c in campaigns.values() if c.get("status") == "active"]),
        "by_campaign": sorted(by_campaign.values(), key=lambda r: -r["revenue"]),
        "by_platform": by_platform,
        "series": series,
        "best_day": {"date": best_day[0], "revenue": round(best_day[1]["revenue"], 2)} if best_day[0] else None,
        "projections": projections,
        "earnings": sorted(rows, key=lambda e: (e.get("date", ""), e.get("created_at", "")),
                           reverse=True)[:200],
    }


# --------------------------------------------------------------- CSV import
CSV_FIELDS = {
    "campaign": ("campaign", "campaign_name", "name"),
    "date": ("date", "day"),
    "views": ("views", "impressions", "plays"),
    "likes": ("likes",),
    "comments": ("comments",),
    "revenue": ("revenue", "payout", "earnings", "amount"),
    "rate": ("rate", "rpm", "cpm"),
    "platform": ("platform",),
}


def import_csv(text: str, *, campaign_id: str = "") -> dict[str, Any]:
    """Import a Whop/analytics CSV. Header names are matched loosely."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    lookup = {(name or "").strip().lower(): name for name in reader.fieldnames}
    added = 0
    skipped = 0
    for raw in reader:
        row: dict[str, Any] = {}
        for field, aliases in CSV_FIELDS.items():
            for alias in aliases:
                key = lookup.get(alias)
                if key and raw.get(key) not in (None, ""):
                    row[field] = raw[key]
                    break
        if not row.get("views") and not row.get("revenue"):
            skipped += 1
            continue
        if campaign_id:
            row["campaign_id"] = campaign_id
        elif row.get("campaign"):
            match = next(
                (c for c in get_state().campaigns
                 if c["name"].lower() == str(row["campaign"]).strip().lower()),
                None,
            )
            if match:
                row["campaign_id"] = match["id"]
        if row.get("rate"):
            row["rate"] = parse_percent(str(row["rate"])) or row["rate"]
        try:
            add_earning(row)
            added += 1
        except (TypeError, ValueError):
            skipped += 1
    return {"added": added, "skipped": skipped}
