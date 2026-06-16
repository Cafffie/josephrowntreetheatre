import re
import os
import time
import logging
import pandas as pd
from datetime import datetime, date
from dateutil import parser

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import undetected_chromedriver as uc

# ============================================================
# CONFIG & LOGGING
# ============================================================
RUN_HEADLESS = False
OUTPUT_FILE = "output1.csv"
PAGES = [
    ("https://www.josephrowntreetheatre.co.uk/whats-on/musical/", "Musical"),
    ("https://www.josephrowntreetheatre.co.uk/whats-on/play/", "Play")
]

if not os.path.exists("log"):
    os.makedirs("log")

logging.basicConfig(
    filename="log/scrape.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

def log(msg, level="info"):
    print(f"[LOG] {msg}")
    if level == "error": logging.error(msg)
    elif level == "warning": logging.warning(msg)
    else: logging.info(msg)


# ============================================================
# BROWSER SETUP
# ============================================================
def setup_browser():
    log("🚀 Starting browser...")
    options = uc.ChromeOptions()
    if RUN_HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = uc.Chrome(options=options, version_main=147)
    driver.implicitly_wait(10)
    return driver


def safe_get(driver, url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            log(f"🌍 Loading page ({attempt}/{retries}): {url}")
            driver.get(url)
            return True
        except Exception as e:
            log(f"❌ Load failed: {e}", "error")
            time.sleep(2)
    return False


def handle_cookies(driver):
    try:
        # Tailored for the Joseph Rowntree Theatre cookie banner layout
        cookie_btn_selector = "a#removecookie"
        WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, cookie_btn_selector))
        )
        driver.find_element(By.CSS_SELECTOR, cookie_btn_selector).click()
        log("Cookies accepted.")
        time.sleep(1)
    except TimeoutException:
        pass


def scroll_to_load_all(driver):
    log("⬇️ Scrolling page...")
    last_height = driver.execute_script("return document.body.scrollHeight")

    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)

        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    log("✅ Finished scrolling")


def _parse_date(text: str) -> date | None:
    try:
        dt = parser.parse(text, dayfirst=True, fuzzy=True)
        if dt.date() < date.today():
            dt = dt.replace(year=dt.year + 1)
        return dt.strftime("%Y-%m-%d")
    except Exception as e:
        log(f"_parse_date failed for '{text}': {e}")
        return None


# ============================================================
# CLEAN CURRENCY TEXT
# ============================================================
def detect_currency(text):
    if not text: return None
    if "£" in text: return "GBP"
    elif "$" in text: return "USD"
    elif "€" in text: return "EUR"
    return None


# ============================================================
# 1. VENUE DETAILS FUNCTION
# ============================================================
def _get_venue_details(driver) -> dict:
    """Extract venue address from the specific footer columns."""
    data = {"venue": None, "address": None, "city": None, "country": "UK"}

    try:
        # Wait until the container paragraph containing address details is located
        WebDriverWait(driver, 5).until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "p.AreaAndVenueDetails")))
        log(f"📦 Address block found inside iframe")
    except Exception as e:
        log(f"⚠️ Address block not found inside iframe {e}", "warning")

    try:
        # Joseph Rowntree Theatre, Haxby Road, York, YO31 8TA
        full_address = driver.find_element(By.CSS_SELECTOR, "p.AreaAndVenueDetails").text.strip()
        data["address"] = full_address

        address_parts = [part.strip() for part in full_address.split(",")]
        data["venue"] = driver.find_element(By.CSS_SELECTOR, ".AreaAndVenueDetails .AreaName").text.strip().rstrip(',')
        data["city"] = address_parts[2]
        data["country"] = "UK"

        log(f"📍 Extracted Address: {data['address']}")

    except Exception as e:
        log(f"⚠️ Dynamic venue extraction failed: {e}", "warning")

    return data


# ============================================================
# 2. EVENT LIST SELECTION
# ============================================================
def _extract_event_list(driver, category: str) -> list[dict]:
    """
    Parses individual cards inside the main events list holder from page.html layout structural classes.
    """
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "ul#gridview-new li.Exhib")
            )
        )
    except Exception as e:
        log("  No event card found on listing page")
        return []

    shows = []
    shows_cards = driver.find_elements(By.CSS_SELECTOR, "ul#gridview-new li.Exhib")
    log(f"📦 Found {len(shows_cards)} show cards")

    for item in shows_cards:
        try:
            title_element = item.find_element(By.CSS_SELECTOR, "div.abautLTitle h3 a")
            title = title_element.get_attribute("textContent").strip()
            link = title_element.get_attribute("href")

            shows.append({
                "title": title,
                "event_url": link,
                "category": category
            })
        except Exception:
            continue
    return shows


# ============================================================
# 3. PERFORMANCE TIMELINE PROCESSING
# ============================================================
def _extract_performances(driver) -> list[dict]:
    """Parses performance instances row-by-row or schedules macro tokens from specific detail blocks."""
    performances = []

    # Click the first book button to load the performance details 
    try:
        first_book_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "div.DetailBanner a#linkToVenues")))
        
        first_book_btn.click()
        time.sleep(2)
        log("✅ 'First Book' button clicked successfully.")

    except Exception as e:
        log(f"  Error finding first booking button: {e}")
        return []

    # wait for the performance details block to load and extract the rows 
    try:
        WebDriverWait(driver, 5).until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "ul.list_ticket li")))
   
        rows = driver.find_elements(By.CSS_SELECTOR, "ul.list_ticket li")
        log(f"📦 Found {len(rows)} performance dates")

        for row in rows:
            try:
                date_element = row.find_element(By.CSS_SELECTOR, "span.DateSpan")
                date_element = date_element.get_attribute("textContent").strip()
                time_element = row.find_element(By.CSS_SELECTOR, "span.TimeSpan").text.strip()
                
                try:
                    book_link_el = row.find_element(By.CSS_SELECTOR, ".bookBtn_RT a")
                    book_link = book_link_el.get_attribute("href")
                except:
                    book_link = driver.current_url

                parsed_date = _parse_date(date_element)
                log(f"Date text: {date_element} | Parsed date: {parsed_date}")
                
                if not parsed_date or not time_element:
                    continue

                perf_date = parsed_date
                perf_time = parser.parse(time_element, fuzzy=True).strftime("%H:%M")
                

                performances.append({
                    "date": perf_date,
                    "time": perf_time,
                    "booking_url": book_link
                })
            except Exception:
                continue

    except Exception as e:
        log(f"  Error extracting performances: {e}")           
    return performances

# ============================================================
# SEAT PRICING
# ============================================================
def extract_all_seats(driver, performances):
    """Extracts seats and pricing from internal ticket frame configurations."""
    venue_details = {"venue": None, "address": None, "city": None, "country": "UK"}
    venue_extracted = False
    seat_pricing = {}
    currency = None
    
    for i, perf in enumerate(performances, start=1):
        try:
            start = time.time()
            log(f"   🔄 [{i}/{len(performances)}] {perf['date']} {perf['time']}")

            driver.get(perf["booking_url"])
            handle_cookies(driver)

            iframe = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "SpektrixIFrame"))
            )
            driver.switch_to.frame(iframe)

            # --- SINGLE-PASS ADDRESS EXTRACTION ---
            if not venue_extracted:
                venue_details = _get_venue_details(driver)
                venue_extracted = True
            # ------------------------------------------------

            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.SeatingArea img, rect.seat"))
            )

            seats = driver.find_elements(By.CSS_SELECTOR, "div.SeatingArea img[class*='Seat'], rect.seat")
            log(f"📦 Found {len(seats)} unique seats. ")

            seat_list = []
            for seat in seats:
                tooltip = seat.get_attribute("tooltip") or seat.get_attribute("title") or ""
                
                detected_currency = detect_currency(tooltip)
                if detected_currency and currency is None:
                    currency = detected_currency

                if not tooltip:
                    continue

                match = re.search(r"([A-Z]+\d+)\s*-\s*£?([\d,.]+)", tooltip)
                if not match:
                    continue
                seat_id = match.group(1)
                ticket_price = float(match.group(2).replace(",", ""))

                seat_list.append({
                    "seat": seat_id,
                    "ticket_price": ticket_price
                })

            perf["capacity"] = len(seats) if seats else None
            key = f"{perf['date']} {perf['time']}"
            seat_pricing[key] = seat_list

            log(f" ✅ Seat lists: {len(seat_list)} | Time: {round(time.time()-start,2)}s")

        except Exception as e:
            log(f"❌ Seat extraction skipped or unavailable for current iframe context: {e}", "warning")
            perf["capacity"] = None
        finally:
            try:
                driver.switch_to.default_content()
            except:
                pass

    log("✅ Seat extraction flow processed")
    return seat_pricing, currency, venue_details


# ============================================================
# MAIN APPLICATION FLOW
# ============================================================
def scrape_shows():
    log("🚀 SCRAPER STARTED")

    driver = setup_browser()
    all_rows = []

    try:
        for page_idx, (url, category) in enumerate(PAGES, start=1):
            log(f"\n🌍 CATEGORY CORRELATION {page_idx}/{len(PAGES)} → {category}")

            if not safe_get(driver, url):
                continue

            handle_cookies(driver)
            scroll_to_load_all(driver)

            shows = _extract_event_list(driver, category)

            for i, show in enumerate(shows, start=1):
                log(f"\n🎭 EVENT SPECIFIC EXTRACTION {i}/{len(shows)} → {show['title']}")

                if not safe_get(driver, show["event_url"]):
                    continue

                handle_cookies(driver)
                scroll_to_load_all(driver)
                scrape_dt = datetime.now().strftime("%Y-%m-%d %H:%M")

                raw_performances = _extract_performances(driver)
                
                if not raw_performances:
                    log(f"⚠️ No active performances extracted for '{show['title']}', row skipped.")
                    continue

                dates = [p["date"] for p in raw_performances if p.get("date")]
                open_date = min(dates) if dates else ""
                close_date = max(dates) if dates else ""

                formatted_performances = str([
                    {"date": p["date"], "time": p["time"]} for p in raw_performances
                ])

                seat_pricing, currency, venue_details = extract_all_seats(driver, raw_performances)
                formatted_seat_pricing = repr(seat_pricing) if seat_pricing else "{}"

                capacity = max([p.get("capacity", 0) for p in raw_performances], default=0)

                row = {
                    "title": show["title"],
                    "venue_url": show["event_url"],
                    "category": show["category"],
                    "venue": venue_details["venue"],
                    "address": venue_details["address"],
                    "city": venue_details["city"],
                    "country": venue_details["country"],
                    "open_date": open_date,
                    "close_date": close_date,
                    "booking_start_date": open_date,
                    "booking_end_date": close_date,
                    "upcoming_performances": formatted_performances,
                    "capacity": capacity if capacity > 0 else None,
                    "currency": currency if seat_pricing else None,
                    "is_limited_run": None,
                    "seat_pricing": formatted_seat_pricing,
                    "scrape_datetime": scrape_dt
                }
                all_rows.append(row)
                log(f"✅ Extracted Row Record Saved: {show['title']}")

    except Exception as e:
        log(f"⚠️ Error occurred while scraping shows: {e}", "warning")

    finally:
        driver.quit()
        log("🛑 Browser processes completely shut down.")

    # Build CSV in strict canonical order
    canonical_columns = [
        "title", "venue_url", "category", "venue", "address", "city", "country",
        "open_date", "close_date", "booking_start_date", "booking_end_date",
        "upcoming_performances", "capacity", "currency", "is_limited_run",
        "seat_pricing", "scrape_datetime"
    ]

    if all_rows:
        df = pd.DataFrame(all_rows)
        df = df.reindex(columns=canonical_columns)
    else:
        df = pd.DataFrame(columns=canonical_columns)

    df.to_csv(OUTPUT_FILE, index=False)
    log(f"✅ Scraped data saved to: {OUTPUT_FILE} ({len(df)} lines generated).")


if __name__ == "__main__":
    scrape_shows()
