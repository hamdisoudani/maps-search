#!/usr/bin/env python3
"""
maps_search — Full-data Google Maps search via Daytona sandbox + headless Chromium.

Extracts: name, rating, review_count, address, phone, website, hours,
category, price_level, coordinates, plus_code, business_status, attributes.

Speed: persistent sandbox mode (reuses sandbox across runs).
First run  ~15-20s (spawn + install deps)
Subsequent ~8-10s   (sandbox alive, just push script + execute)

Usage:
    maps-search "Plumber in Paris"
    maps-search "Sushi Tokyo" --limit 5 --output json
    maps-search "Cafe near me" -o csv
    maps-search --status
    maps-search --destroy
    maps-search --oneshot "Quick look"      # no caching
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────
ENV_FILE = Path.home() / ".hermes" / ".env"
SCRIPT_DIR = Path(__file__).resolve().parent
PLAYWRIGHT_TEMPLATE = SCRIPT_DIR / "maps_search_payload.py.tpl"
CACHE_DIR = Path.home() / ".hermes" / "cache"
CACHE_FILE = CACHE_DIR / "maps_sandbox.json"
LOCK_FILE = CACHE_DIR / "maps_sandbox.lock"

PLAYWRIGHT_IMAGE = "ubuntu:22.04"
CPU = 2
MEMORY_GB = 4
DISK_GB = 10

# ── Helpers ─────────────────────────────────────────────────────────────

def load_api_key():
    """Read DAYTONA_API_KEY from .env file, ignoring comments."""
    if not ENV_FILE.exists():
        print("FATAL: ~/.hermes/.env not found", file=sys.stderr)
        sys.exit(1)
    for line in ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        if key.strip() == "DAYTONA_API_KEY":
            return val.strip()
    print("FATAL: DAYTONA_API_KEY not found in .env", file=sys.stderr)
    sys.exit(1)


def get_daytona():
    """Factory: return a Daytona client (avoids duplicating key loading)."""
    from daytona_sdk import Daytona
    api_key = load_api_key()
    os.environ["DAYTONA_API_KEY"] = api_key
    return Daytona()


def sanitize_query(q: str) -> str:
    """Sanitize query for safe embedding in a single-quoted Python string."""
    if len(q) > 200:
        raise ValueError(f"Query too long ({len(q)} chars, max 200)")
    if not q.isprintable():
        raise ValueError("Query contains non-printable characters")
    return q.replace("\\", "\\\\").replace("'", "\\'")


def get_cached_sandbox(daytona) -> tuple:
    """Return (sb, is_fresh) — an active sandbox from cache, or None."""
    if not CACHE_FILE.exists():
        return None, True
    try:
        meta = json.loads(CACHE_FILE.read_text())
        sb_id = meta.get("sandbox_id")
        if not sb_id:
            return None, True
        sb = daytona.get(sb_id)
        if sb.state.name == "STARTED":
            return sb, False
        if sb.state.name == "STOPPED":
            print("   🔄 Restarting paused sandbox...", file=sys.stderr)
            daytona.start(sb)
            time.sleep(3)
            sb = daytona.get(sb_id)
            if sb.state.name == "STARTED":
                return sb, False
    except Exception as e:
        print(f"   ⚠ Cache read error: {e}", file=sys.stderr)
    return None, True


def save_sandbox_cache(sb_id: str):
    """Atomically write sandbox ID to cache file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "sandbox_id": sb_id,
        "created_at": time.time(),
        "image": PLAYWRIGHT_IMAGE,
    }))
    tmp.rename(CACHE_FILE)  # atomic on same filesystem


def clear_sandbox_cache():
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()


# ═══════════════════════════════════════════════════════════════════════
#  Core engine
# ═══════════════════════════════════════════════════════════════════════

def ensure_sandbox(daytona, persistent: bool):
    """Get or create a ready-to-use sandbox. Returns (sb, is_new).
    
    Safety: wraps creation in try/except so sandbox is never orphaned.
    """
    if persistent:
        try:
            cached, is_new = get_cached_sandbox(daytona)
            if cached:
                return cached, is_new
        except Exception as e:
            print(f"   ⚠ Cache error, creating fresh: {e}", file=sys.stderr)

    from daytona_sdk import CreateSandboxFromImageParams, Resources
    sb = None
    try:
        print("   🚀 Spawning new sandbox...", file=sys.stderr)
        params = CreateSandboxFromImageParams(
            image=PLAYWRIGHT_IMAGE,
            resources=Resources(cpu=CPU, memory=MEMORY_GB, disk=DISK_GB),
        )
        sb = daytona.create(params=params, timeout=120)

        # Install deps with timeouts
        r = sb.process.exec(
            "apt-get update -qq 2>/dev/null && apt-get install -y -qq python3-pip 2>&1 | tail -1",
            timeout=120
        )
        if r.exit_code != 0 and r.exit_code is not None:
            print(f"   ⚠ apt: {r.result.strip()[-80:]}", file=sys.stderr)

        r = sb.process.exec(
            "pip3 install playwright -q 2>&1 | tail -1 && "
            "python3 -m playwright install-deps chromium 2>&1 | tail -1 && "
            "python3 -m playwright install chromium 2>&1 | tail -3",
            timeout=300
        )
        if r.exit_code != 0:
            print(f"   ⚠ playwright: {r.result.strip()[-150:]}", file=sys.stderr)

        if persistent:
            save_sandbox_cache(sb.id)
        return sb, True

    except Exception:
        # Orphan prevention: delete any partially-created sandbox
        if sb:
            try:
                daytona.delete(sb)
                print("   🧹 Cleaned up failed sandbox", file=sys.stderr)
            except Exception:
                pass
        raise


def run_search(query: str, limit: int = 10, persistent: bool = True) -> list[dict]:
    """Core search function. Returns structured results."""
    daytona = get_daytona()
    sb = None
    cleanup = not persistent

    try:
        sb, is_new = ensure_sandbox(daytona, persistent)

        if is_new:
            print(f"   🔧 Sandbox {sb.id[:8]} ready", file=sys.stderr)
        else:
            print(f"   ♻️  Reusing sandbox {sb.id[:8]}", file=sys.stderr)

        # Read and render template
        if not PLAYWRIGHT_TEMPLATE.exists():
            raise FileNotFoundError(
                f"Template not found: {PLAYWRIGHT_TEMPLATE}"
            )
        payload = PLAYWRIGHT_TEMPLATE.read_text(encoding="utf-8")
        safe_query = sanitize_query(query)
        payload = payload.replace("__QUERY__", safe_query)

        # Write payload to sandbox via base64
        encoded = base64.b64encode(payload.encode()).decode()
        r = sb.process.exec(f"echo '{encoded}' | base64 -d > /tmp/run_maps.py")
        if r.exit_code != 0:
            raise RuntimeError(f"Failed to write script: {r.result}")

        print(f"   🔍 Searching...", file=sys.stderr)
        t0 = time.time()
        r = sb.process.exec("cd /tmp && python3 run_maps.py 2>&1", timeout=90)
        elapsed = time.time() - t0
        output = r.result

        # Check for sandbox-side errors
        exit_code = r.exit_code

        # CAPTCHA / rate-limit detection
        if "CAPTCHA" in output or "unusual traffic" in output.lower():
            print("   ⚠ Google Maps rate-limited or CAPTCHA detected", file=sys.stderr)
            return []

        # Parse structured results
        all_results = []
        if "---RESULTS_START---" in output:
            raw = output.split("---RESULTS_START---")[1].split("---RESULTS_END---")[0].strip()
            if raw:
                try:
                    all_results = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(f"   ⚠ JSON parse error: {e}", file=sys.stderr)
        elif exit_code and exit_code != 0:
            print(f"   ⚠ Script failed (exit {exit_code}): {output[-300:]}", file=sys.stderr)

        # Extract screenshot
        try:
            cat_r = sb.process.exec("base64 /tmp/maps_result.png 2>/dev/null || echo 'NO_SS'")
            if cat_r.exit_code == 0 and "NO_SS" not in cat_r.result:
                img_data = base64.b64decode(cat_r.result.strip())
                ss_path = f"/tmp/maps_search_{int(time.time())}.png"
                with open(ss_path, "wb") as f:
                    f.write(img_data)
                print(f"   📸 {ss_path}", file=sys.stderr)
        except Exception:
            pass  # screenshot is optional

        print(f"   ⏱  {elapsed:.1f}s | {len(all_results)} results\n", file=sys.stderr)

        return all_results[:limit] if limit else all_results

    except Exception as e:
        print(f"   ❌ {e}", file=sys.stderr)
        return []

    finally:
        if cleanup and sb:
            try:
                daytona.delete(sb)
                clear_sandbox_cache()
                print(f"   🧹 Cleaned up", file=sys.stderr)
            except Exception as e:
                print(f"   ⚠ Cleanup FAILED: {e} (sandbox still alive)", file=sys.stderr)
                # Don't clear cache — sandbox still exists
                pass


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

def cmd_status():
    """Check if persistent sandbox is alive."""
    daytona = get_daytona()
    try:
        sb, _ = get_cached_sandbox(daytona)
        if sb:
            print(f"✅ Persistent sandbox {sb.id[:8]} is ALIVE (state: {sb.state.name})")
        else:
            print("❌ No persistent sandbox running")
    except Exception as e:
        print(f"⚠️ Error checking sandbox: {e}")


def cmd_destroy():
    """Kill the persistent sandbox."""
    daytona = get_daytona()
    try:
        sb, _ = get_cached_sandbox(daytona)
        if sb:
            daytona.delete(sb)
            clear_sandbox_cache()
            print(f"✅ Sandbox {sb.id[:8]} destroyed")
        else:
            print("No sandbox to destroy")
    except Exception as e:
        print(f"⚠️ {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Google Maps search via Daytona sandbox — full business data extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  maps-search "Plumber in Paris"
  maps-search "Sushi Tokyo" --limit 5 --output json
  maps-search "Cafe near me" -o csv
  maps-search --status
  maps-search --destroy
  maps-search --oneshot "Quick look"
        """,
    )
    parser.add_argument("query", type=str, nargs="?", help="Search query")
    parser.add_argument("--limit", "-l", type=int, default=20, help="Max results (default: 20)")
    parser.add_argument("--output", "-o", choices=["pretty", "json", "csv"], default="pretty")
    parser.add_argument("--oneshot", action="store_true", help="Destroy sandbox after query (no caching)")
    parser.add_argument("--status", action="store_true", help="Check persistent sandbox status")
    parser.add_argument("--destroy", action="store_true", help="Kill persistent sandbox")

    args = parser.parse_args()

    if args.status:
        cmd_status()
        return
    if args.destroy:
        cmd_destroy()
        return
    if not args.query:
        parser.print_help()
        return

    results = run_search(args.query, limit=args.limit, persistent=not args.oneshot)

    if args.output == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.output == "csv":
        import csv
        fieldnames = [
            "name", "rating", "review_count", "category", "address",
            "phone", "website", "hours", "price_level",
            "business_status", "lat", "lng",
            "plus_code", "wheelchair_accessible", "attributes"
        ]
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = {k: r.get(k, "") for k in fieldnames}
            if isinstance(row.get("attributes"), list):
                row["attributes"] = "; ".join(row["attributes"])
            writer.writerow(row)
    else:
        # pretty
        print(f"\n{'='*70}")
        print(f"  Google Maps: \"{args.query}\"  —  {len(results)} results")
        print(f"{'='*70}")
        for i, r in enumerate(results, 1):
            name = r.get("name", "?")
            rating = r.get("rating", "?")
            rc = r.get("review_count", "")
            stars = f"⭐{rating}" if rating else ""
            reviews = f"({rc} reviews)" if rc else ""
            print(f"\n  {i}. {name}  {stars} {reviews}".strip())

            for label, field, icon in [
                ("Category", "category", "📂"),
                ("Address", "address", "📍"),
                ("Phone", "phone", "📞"),
                ("Hours", "hours", "🕐"),
                ("Website", "website", "🌐"),
                ("Status", "business_status", "🚦"),
                ("Price", "price_level", "💰"),
            ]:
                val = r.get(field, "")
                if val:
                    print(f"     {icon} {val}")

            # Extra fields
            attrs = r.get("attributes", [])
            if attrs:
                print(f"     ♿ {' | '.join(attrs) if isinstance(attrs, list) else attrs}")

            coords = ""
            if r.get("lat") and r.get("lng"):
                coords = f"{r['lat']}, {r['lng']}"
            pc = r.get("plus_code", "")
            extras = " | ".join(filter(None, [coords, pc]))
            if extras:
                print(f"     🗺️ {extras}")

        print(f"\n{'='*70}")
        print(f"  {len(results)} result(s)  |  use -o json for full data")
        print(f"{'='*70}")


if __name__ == "__main__":
    main()
