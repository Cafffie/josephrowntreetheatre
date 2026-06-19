"""Joseph Rowntree (josephrowntreetheatre.co.uk) extractor."""

import json
import re
import time
from datetime import date, datetime

import pandas as pd
from dateutil import parser
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils.base_extractor import BaseExtractor
from utils.logger import setup_logger
from utils.scraping_helpers import (
    accept_cookies,
    convert_to_24hr,
    extract_postcode,
    format_datetime_key,
    get_city_country_uk,
    get_currency_from_price,
    get_scrape_datetime,
    human_delay,
    normalize_country,
    standardize_category,
)

from .josephrowntree_config import (
    COOKIE_BTN_XPATH,
    DELAY_BETWEEN_PERFS,
    DELAY_BETWEEN_SHOWS,
    HEADLESS,
    PAGE_LOAD_TIMEOUT,
    PAGES,
    SEAT_WAIT_TIMEOUT,
    SITE_ID,
)

logger = setup_logger(__name__, log_to_file=False)


class JosephrowntreeExtractor(BaseExtractor):
    def __init__(self, local_test=False, show_count=None, **kwargs):
        super().__init__(site_id=SITE_ID, **kwargs)
        self.local_test = local_test
        self.show_count = show_count

    # ------------------------------------------------------------------
    # BaseExtractor interface
    # ------------------------------------------------------------------

    def extract(self) -> bytes:
        all_data = []
        # venue_details = {"address": None, "city": None, "country": None}
        driver = self.launch_driver(
            headless=HEADLESS, page_load_timeout=PAGE_LOAD_TIMEOUT
        )

        try:
            all_shows = []
            for i, (url, category) in enumerate(PAGES):
                self.custom_logger.info(f"[Listing] {category}: {url}")
                driver.get(url)
                accept_cookies(driver, xpath=COOKIE_BTN_XPATH)
                self._scroll_to_load_all(driver)

                shows = self._extract_event_list(driver, category)
                self.custom_logger.info(f"  → {len(shows)} show(s) found")
                all_shows.extend(shows)

            # Deduplicate by URL — a show listed under both categories should only be scraped once
            seen_urls: set[str] = set()
            deduped: list[dict] = []
            for show in all_shows:
                url = show["event_url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    deduped.append(show)
                else:
                    self.custom_logger.info(
                        f"  Skipping duplicate: {show['title']!r} (already queued)"
                    )
            all_shows = deduped

            if self.show_count:
                all_shows = all_shows[: self.show_count]
                self.custom_logger.info(
                    f"show_count={self.show_count}: limited to {len(all_shows)} show(s)"
                )

            for idx, show in enumerate(all_shows, 1):
                self.custom_logger.info(
                    f"[{idx}/{len(all_shows)}] [{show['category']}] {show['title']!r}"
                )
                try:
                    record = self._scrape_show(driver, show)
                    if record:
                        all_data.append(record)
                        self.log_record(record)
                        self._log_show_summary(record)
                except Exception as exc:
                    self.custom_logger.error(f"  ✗ Error: {exc}", exc_info=True)

                human_delay(*DELAY_BETWEEN_SHOWS)

            self.custom_logger.info(f"Extraction complete — {len(all_data)} record(s)")

        finally:
            try:
                driver.quit()
            except Exception:
                pass

        return json.dumps(all_data, default=str).encode("utf-8")

    # ------------------------------------------------------------------
    # Level 2 — Show detail
    # ------------------------------------------------------------------

    def _scrape_show(self, driver, show: dict) -> dict | None:
        for attempt in range(1, 4):
            try:
                driver.get(show["event_url"])
                break
            except (TimeoutException, WebDriverException) as exc:
                self.custom_logger.warning(
                    f"  Load attempt {attempt}/3 failed for {show['title']!r}: "
                    f"{type(exc).__name__}"
                )
                if attempt == 3:
                    raise
                time.sleep(3)
        accept_cookies(driver, xpath=COOKIE_BTN_XPATH)
        self._scroll_to_load_all(driver)

        performances, venue = self._extract_performances(driver)

        if not performances:
            self.custom_logger.warning(
                f"  No performances found for '{show['title']}', skipping"
            )
            return None

        seat_pricing, currency, capacity, venue_details = self._scrape_seat_pricing(
            driver, performances
        )

        if performances:
            sorted_dates = sorted([p["date"] for p in performances])
            open_date = sorted_dates[0]
            close_date = sorted_dates[-1]
        else:
            open_date = datetime.now().strftime("%Y-%m-%d")
            close_date = datetime.now().strftime("%Y-%m-%d")

        return {
            "title": show["title"],
            "venue_url": show["event_url"],
            "category": standardize_category(show["category"]),
            "venue": venue,
            "address": venue_details["address"],
            "city": venue_details["city"],
            "country": normalize_country(venue_details["country"]),
            "open_date": open_date,
            "close_date": close_date,
            "booking_start_date": open_date,
            "booking_end_date": close_date,
            "upcoming_performances": [
                {"date": p["date"], "time": p["time"]} for p in performances
            ],
            "capacity": capacity,
            "currency": currency,
            "is_limited_run": None,
            "seat_pricing": seat_pricing,
            "scrape_datetime": get_scrape_datetime(),
        }

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        if not df.empty and "is_limited_run" in df.columns:
            df["is_limited_run"] = None
        if not df.empty and "capacity" in df.columns:
            df["capacity"] = pd.to_numeric(df["capacity"], errors="coerce").astype(
                "Int64"
            )
        return df

    def _parse(self, raw: bytes) -> pd.DataFrame:
        data = json.loads(raw.decode("utf-8"))
        df = pd.DataFrame(data)
        if not df.empty and "capacity" in df.columns:
            if df["capacity"].notna().any():
                df["capacity"] = df["capacity"].astype(pd.Int64Dtype())
        self.custom_logger.info(f"Parsed {len(df)} record(s)")
        return df

    # ============================================================
    # 1. VENUE DETAILS
    # ============================================================
    def _get_venue_details(self, driver) -> dict:
        """Extract venue address from the specific footer columns."""

        data = {"address": None, "city": None, "country": None}
        try:
            # Wait until the container paragraph containing address details is located
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "p.AreaAndVenueDetails")
                )
            )

        except Exception as e:
            self.custom_logger.warning(
                f" Address block not found inside iframe {e}", "warning"
            )

        try:
            # Joseph Rowntree Theatre, Haxby Road, York, YO31 8TA
            full_address = driver.find_element(
                By.CSS_SELECTOR, "p.AreaAndVenueDetails"
            ).text.strip()
            data["address"] = full_address

            address_parts = [part.strip() for part in full_address.split(",")]

            if len(address_parts) >= 3:
                postcode = extract_postcode(full_address, region="UK")
                data["city"] = address_parts[2]
                if postcode:
                    _, country = get_city_country_uk(postcode)
                    data["country"] = normalize_country(country) if country else None

        except Exception as e:
            self.custom_logger.warning(f"  Venue details extraction failed: {e}")
        return data

    # ============================================================
    # 2. EVENT LIST SELECTION
    # ============================================================

    def _extract_event_list(self, driver, category: str) -> list[dict]:
        """Parses individual cards inside the main events list holder."""
        try:
            WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "ul#gridview-new li.Exhib")
                )
            )

        except TimeoutException:
            self.custom_logger.warning("  No event found on listing page")
            return []

        shows = []
        shows_cards = driver.find_elements(By.CSS_SELECTOR, "ul#gridview-new li.Exhib")

        for item in shows_cards:
            try:
                title_element = item.find_element(
                    By.CSS_SELECTOR, "div.abautLTitle h3 a"
                )
                title = title_element.get_attribute("textContent").strip()
                link = title_element.get_attribute("href")

                shows.append({"title": title, "event_url": link, "category": category})
            except Exception:
                continue
        return shows

    # ============================================================
    # 3. PERFORMANCE TIMELINE PROCESSING
    # ============================================================

    def _extract_performances(self, driver) -> tuple[list[dict], str | None]:
        """Parses performance instances row-by-row or schedules macro tokens from specific detail blocks."""

        performances = []
        venue = None
        venue_sufix = "Joseph Rowntree Theatre"

        try:
            venue_element = driver.find_element(
                By.CSS_SELECTOR, ".InfoEvent_detail label"
            ).text.strip()
            venue = f" {venue_element}, {venue_sufix}"
        except Exception as e:
            self.custom_logger.warning(f"  Error extracting venue: {e}")

        # Click the first book button to load the performance details
        try:
            first_book_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "div.DetailBanner a#linkToVenues")
                )
            )

            first_book_btn.click()
            time.sleep(2)

        except Exception as e:
            self.custom_logger.warning(f"  Error finding first booking button: {e}")
            return []

        # wait for the performance details block to load and extract the rows
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "ul.list_ticket li"))
            )

            rows = driver.find_elements(By.CSS_SELECTOR, "ul.list_ticket li")

            for row in rows:
                try:
                    date_element = row.find_element(By.CSS_SELECTOR, "span.DateSpan")
                    date_text = date_element.get_attribute("textContent").strip()
                    time_element = row.find_element(
                        By.CSS_SELECTOR, "span.TimeSpan"
                    ).text.strip()

                    try:
                        book_link_el = row.find_element(
                            By.CSS_SELECTOR, ".bookBtn_RT a"
                        )
                        book_link = book_link_el.get_attribute("href")
                    except Exception:
                        book_link = driver.current_url

                    if not date_text or not time_element:
                        continue

                    perf_date = self._parse_date(date_text).strftime("%Y-%m-%d")
                    perf_time = convert_to_24hr(time_element)

                    # self.custom_logger.info(f"perf_time: {perf_date} | perf_time: {perf_time}")

                    performances.append(
                        {"date": perf_date, "time": perf_time, "booking_url": book_link}
                    )
                except Exception:
                    continue

        except Exception as e:
            self.custom_logger.warning(f"  Error extracting performances: {e}")
        return performances, venue

    # ============================================================
    # SEAT PRICING
    # ============================================================

    def _scrape_seat_pricing(
        self, driver, performances: list[dict]
    ) -> tuple[dict, str | None, int | None]:
        """Extracts seats, pricing and venue from internal ticket frame configurations."""

        venue_details = {"address": None, "city": None, "country": None}
        venue_extracted = False

        seat_pricing = {}
        currency = None
        max_capacity = None

        # NEW FLAG: Tracks if we hit a technical "no seat map available" or layout error
        encountered_no_seatmap = False

        for i, perf in enumerate(performances, 1):
            key = format_datetime_key(perf["date"], perf["time"])
            if not key:
                continue

            # Confirm if sold out
            if not perf.get("booking_url"):
                seat_pricing[key] = []
                continue

            self.custom_logger.info(
                f"  [{i}/{len(performances)}] Seats for {perf['date']} {perf['time']}"
            )

            try:
                driver.get(perf["booking_url"])

                iframes = driver.find_elements(By.ID, "SpektrixIFrame")
                if iframes:
                    iframe = iframes[0]
                    driver.switch_to.frame(iframe)

                    # --- SINGLE-PASS ADDRESS EXTRACTION ---
                    if not venue_extracted:
                        venue_details = self._get_venue_details(driver)
                        venue_extracted = True
                    # ------------------------------------------------

                    WebDriverWait(driver, SEAT_WAIT_TIMEOUT).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "div.SeatingArea img, rect.seat")
                        )
                    )

                    # Grab all img items inside the seating container that carry the seat attribute layout class strings
                    seats = driver.find_elements(
                        By.CSS_SELECTOR, "div.SeatingArea img, rect.seat"
                    )
                    self.custom_logger.info(f" Found {len(seats)} unique seats. ")

                    perf_capacity = len(seats) if seats else None
                    if max_capacity is None or perf_capacity > max_capacity:
                        max_capacity = perf_capacity

                    seat_list = []
                    for img in seats:
                        tooltip = (
                            img.get_attribute("tooltip")
                            or img.get_attribute("title")
                            or ""
                        )
                        if not tooltip:
                            continue

                        match = re.search(r"([A-Z]+\d+)\s*-\s*[££]?([\d,.]+)", tooltip)
                        if not match:
                            continue

                        if currency is None:
                            currency = get_currency_from_price(tooltip)

                        style = img.get_attribute("style") or ""
                        top_match = re.search(r"top:\s*([\d.]+)%", style)
                        top = float(top_match.group(1)) if top_match else None

                        section = "BALCONY" if top is not None and top <= 25 else "STALLS"

                        seat_list.append(
                            {
                                "seat": f"{section} {match.group(1)}",
                                "ticket_price": float(match.group(2).replace(",", "")),
                            }
                        )

                    if seat_list:
                        seat_pricing[key] = seat_list
                    self.custom_logger.info(f"    {len(seat_list)} seats extracted")

                else:
                    # MISSING SEATMAP: Page loaded but iframe layout isn't there
                    seat_pricing[key] = []
                    encountered_no_seatmap = True  # <--- Flagged
                    self.custom_logger.info(
                        f" Non seat map available for {perf['date']} {perf['time']}"
                    )

            except Exception as e:
                # LAYOUT ERROR / TIMEOUT
                seat_pricing[key] = []
                encountered_no_seatmap = True  # <--- Flagged
                self.custom_logger.warning(f"  Seat extraction error: {e}")
                seat_pricing[key] = []

            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

            human_delay(*DELAY_BETWEEN_PERFS)

        # =================================================================================
        # CONDITIONAL CHECK:
        # Only clear to {} if we actually hit "no seatmap" issues AND everything is empty.
        # =================================================================================
        if encountered_no_seatmap and all(
            len(seats) == 0 for seats in seat_pricing.values()
        ):
            self.custom_logger.info(
                " All performances lack a seat map layout. Resetting seat_pricing = {}"
            )
            seat_pricing = {}
        # =================================================================================

        return seat_pricing, currency, max_capacity, venue_details

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_date(self, text: str) -> date | None:
        try:
            txt = text.strip()
            # If the text is already in ISO YYYY-MM-DD form, parse without dayfirst
            if re.match(r"^\d{4}-\d{2}-\d{2}$", txt):
                dt = datetime.fromisoformat(txt)
            else:
                dt = parser.parse(txt, dayfirst=True, fuzzy=True)
            original = dt
            incremented = False
            if dt.date() < date.today():
                dt = dt.replace(year=dt.year + 1)
                incremented = True
            try:
                self.custom_logger.info(
                    f"_parse_date: '{text}' -> parsed {original.date().isoformat()}"
                    f"; used {dt.date().isoformat()} (incremented={incremented})"
                )
            except Exception:
                pass
            return dt
        except Exception as e:
            self.custom_logger.warning(f"_parse_date failed for '{text}': {e}")
            return None

    def _log_show_summary(self, record: dict) -> None:
        seat_pricing = record.get("seat_pricing") or {}
        perfs = record.get("upcoming_performances") or []
        divider = "  " + "━" * 54
        lines = [
            divider,
            f"  ✓  {record['title']}  [{record['category']}]",
            f"     Venue    : {record['venue']}, {record['city']}, {record['country']}",
            f"     Run      : {record['open_date']} → {record['close_date']}",
            f"     Capacity : {record['capacity']}  |  Currency: {record['currency']}",
            f"     Performances ({len(perfs)}):",
        ]
        for p in perfs:
            key = f"{p['date']} {p['time']}"
            seats = seat_pricing.get(key, [])
            seat_label = (
                f"{len(seats)} seats" if seats else "No seat map availabe or sold out"
            )
            lines.append(f"       • {key}  →  {seat_label}")
        lines.append(divider)
        self.custom_logger.info("\n".join(lines))

    def _scroll_to_load_all(self, driver) -> None:
        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height


def main():
    extractor = JosephrowntreeExtractor(
        save_csv_locally=False, csv_incremental_mode=False
    )
    result = extractor.run()
    logger.info("Extraction result: %s", result)


if __name__ == "__main__":
    main()
