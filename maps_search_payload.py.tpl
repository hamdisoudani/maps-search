import asyncio, json, sys, re, time
from playwright.async_api import async_playwright

QUERY = '__QUERY__'


async def extract_cards(page) -> list[dict]:
    """Extract ALL business data using DOM + text parsing."""

    # Adaptive scroll
    feed_el = await page.query_selector('[role="feed"]')
    if feed_el:
        last_height = 0
        for _ in range(8):
            curr = await page.evaluate("""() => {
                const f = document.querySelector('[role="feed"]');
                return f ? f.scrollHeight : 0;
            }""")
            if curr == last_height and _ > 0:
                break
            last_height = curr
            await page.evaluate("""() => {
                const f = document.querySelector('[role="feed"]');
                if (f) f.scrollTop = f.scrollHeight;
            }""")
            await page.wait_for_timeout(400)

    # Extract from DOM
    results = await page.evaluate("""() => {
        const cards = document.querySelectorAll('.Nv2PK');
        const items = [];
        for (const card of cards) {
            try {
                const item = {};
                const nameEl = card.querySelector('.qBF1Pd');
                item.name = nameEl ? nameEl.textContent.trim() : '';
                const ratingEl = card.querySelector('.MW4etd');
                item.rating = ratingEl ? ratingEl.textContent.trim() : '0';
                const webEl = card.querySelector('a.lcr4fd[href^="http"]');
                item.website = webEl ? webEl.href : '';
                const linkEl = card.querySelector('a.hfpxzc');
                if (linkEl) {
                    const href = linkEl.href || '';
                    const lat = href.match(/!3d([\d.\-]+)/);
                    const lng = href.match(/!4d([\d.\-]+)/);
                    if (lat) item.lat = lat[1];
                    if (lng) item.lng = lng[1];
                }
                item.wheelchair_accessible =
                    !!card.querySelector('[aria-label="Wheelchair accessible entrance"]');
                const attrIcons = card.querySelectorAll('[aria-label][role="img"]');
                const attrs = [];
                for (const el of attrIcons) {
                    const label = el.getAttribute('aria-label');
                    if (label && label !== 'Wheelchair accessible entrance' && !label.includes('stars')) {
                        attrs.push(label);
                    }
                }
                if (attrs.length > 0) item.attributes = attrs;
                item._text = card.innerText;
                items.push(item);
            } catch(e) { items.push({error: String(e)}); }
        }
        return items;
    }""")

    # Post-process text into fields
    PHONE_RE = re.compile(r'[\d\s\-()+]{8,20}')
    clean = []
    seen_names = set()

    for item in results:
        name = item.get('name', '').strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        text = item.get('_text', '')
        lines = [l.strip() for l in text.split('\n') if l.strip()]

        rating_str = item.get('rating', '0')
        try:
            item['rating'] = float(rating_str)
        except:
            item['rating'] = 0.0

        detail_line = ''
        hours_line = ''

        for line in lines:
            if line == name:
                continue
            try:
                if line == rating_str or line == rating_str.replace('.0', ''):
                    continue
                float(line.replace(',', '.'))
                continue
            except:
                pass
            if '·' in line:
                if not detail_line:
                    detail_line = line
                elif 'Open' in line or 'Closed' in line or '24' in line or 'AM' in line or 'PM' in line:
                    hours_line = line
                elif not hours_line:
                    hours_line = line

        if detail_line:
            parts = [p.strip() for p in re.split(r'\s*[·\u2022]\s*', detail_line) if p.strip()]
            if parts:
                item['category'] = parts[0]
                for p in reversed(parts):
                    cleaned = re.sub(r'[\ue000-\uf8ff]', '', p).strip()
                    if cleaned and not cleaned.startswith('$'):
                        item['address'] = cleaned
                        break

        if hours_line:
            hp_parts = re.split(r'\s*[·\u2022]\s*', hours_line)
            hp_parts = [p.strip() for p in hp_parts if p.strip()]
            for hp in hp_parts:
                if 'Open' in hp or 'Closed' in hp or '24' in hp or 'AM' in hp or 'PM' in hp:
                    item['hours'] = hp
                elif re.search(r'[\d\s\-()+]{8,}', hp):
                    phone = hp.strip()
                    if len(phone) >= 8:
                        item['phone'] = phone

        if not item.get('phone'):
            for line in lines:
                m = PHONE_RE.search(line)
                if m:
                    phone = m.group(0).strip()
                    if len(phone) >= 8:
                        item['phone'] = phone
                        break

        full_text = ' '.join(lines).lower()
        if 'temporarily closed' in full_text:
            item['business_status'] = 'temporarily_closed'
        elif 'permanently closed' in full_text:
            item['business_status'] = 'permanently_closed'
        elif any(w in full_text for w in [' open ', 'open ', ' open']):
            item['business_status'] = 'open'

        pm = re.search(r'(\$+)\s', full_text)
        if pm:
            item['price_level'] = pm.group(1)

        rm = re.search(r'\(([\d,]+)\s*review', full_text)
        if rm:
            try:
                item['review_count'] = int(rm.group(1).replace(',', ''))
            except:
                pass

        pcm = re.search(r'([A-Z0-9]{4,}\+[A-Z0-9]{2,})', full_text)
        if pcm:
            item['plus_code'] = pcm.group(1)

        item.pop('_text', None)
        if item.get('address') or item.get('phone') or item.get('website'):
            clean.append(item)

    return clean


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US"
        )
        page = await context.new_page()

        try:
            print("Navigating...", flush=True)
            await page.goto(
                "https://www.google.com/maps",
                timeout=25000,
                wait_until="domcontentloaded"
            )
            await page.wait_for_timeout(2000)

            body_text = await page.inner_text("body")
            bl = body_text.lower()
            if "unusual traffic" in bl or "captcha" in bl or "please verify" in bl:
                print("ERROR: CAPTCHA or rate limit detected", flush=True)
                await browser.close()
                sys.exit(2)

            search = await page.query_selector('input[name="q"]')
            if not search:
                print("ERROR: no search input", flush=True)
                await browser.close()
                sys.exit(1)

            await search.click()
            await page.wait_for_timeout(200)
            await search.fill("")
            await page.keyboard.type(QUERY, delay=15)
            await page.keyboard.press("Enter")

            for attempt in range(15):
                count = await page.evaluate("""() => document.querySelectorAll('.Nv2PK').length""")
                if count > 0:
                    break
                await page.wait_for_timeout(1000)

            print(f"TITLE: {await page.title()}", flush=True)
            print(f"URL: {page.url}", flush=True)

            results = await extract_cards(page)
            print(f"COUNT: {len(results)}", flush=True)

            await page.screenshot(path="/tmp/maps_result.png", full_page=True)

            print("---RESULTS_START---", flush=True)
            print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
            print("---RESULTS_END---", flush=True)

        except SystemExit:
            raise
        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            import traceback
            traceback.print_exc()
            sys.exit(1)

        finally:
            await browser.close()


asyncio.run(main())
