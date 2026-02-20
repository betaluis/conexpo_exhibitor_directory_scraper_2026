from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://directory.conexpoconagg.com"
START_URL = "https://directory.conexpoconagg.com/8_0/explore/exhibitor-categories-parent.cfm#/"
CATEGORY_LINK_SELECTOR = "tbody a[href*='cat-exhibitorcategoriesparents|']"
SUBCATEGORY_LINK_SELECTOR = "tbody a[href*='cat-exhibitorcategoriesparents|']"
EXHIBITOR_LINK_SELECTOR = "li.js-Card"
EXHIBITOR_LINK_IN_CARD = "a[href*='/exhibitor/exhibitor-details.cfm?exhid=']"
VIEW_ALL_LABEL = "View All Exhibitors"
OUTPUT_CSV = "exhibitors_resume_2.csv"
CHECKPOINT_FILE = "checkpoint.json"
RESUME_AFTER_COMPANY_NAME = "Stedman Machine Company"
CSV_HEADERS = [
    "category",
    "subcategory",
    "company_name",
    "address",
    "website",
    "phone",
    "description",
    "booth",
]


@dataclass(frozen=True)
class Category:
    name: str
    url: str


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _safe_goto(page, url: str, *, retries: int = 2) -> None:
    attempt = 0
    while True:
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            return
        except PlaywrightTimeoutError:
            attempt += 1
            if attempt > retries:
                raise


def _extract_link_text_pairs(page, selector: str) -> list[Category]:
    elements = page.locator(selector)
    count = elements.count()
    categories: list[Category] = []
    for index in range(count):
        element = elements.nth(index)
        href = element.get_attribute("href")
        name = (element.text_content() or "").strip()
        if href:
            categories.append(Category(name=name, url=urljoin(BASE_URL, href)))
    return categories


def _extract_links(page, selector: str) -> list[str]:
    elements = page.locator(selector)
    count = elements.count()
    return _dedupe(
        [urljoin(BASE_URL, elements.nth(i).get_attribute("href") or "") for i in range(count)]
    )


def _count_exhibitor_cards(page) -> int:
    page.wait_for_selector(EXHIBITOR_LINK_SELECTOR, timeout=60000)
    return page.locator(EXHIBITOR_LINK_SELECTOR).count()


def _extract_exhibitor_details(page) -> dict[str, str] | None:
    page.wait_for_selector(".exhibitor-name", timeout=60000)
    company_name = (page.locator(".exhibitor-name").first.text_content() or "").strip()

    contact = page.locator("article#js-vue-contactinfo")
    address_lines = [
        (line.text_content() or "").strip() for line in contact.locator("address p").all()
    ]
    address = ", ".join([line for line in address_lines if line])

    website_link = contact.locator("a[href^='http']")
    if website_link.count() > 0:
        website = (website_link.first.get_attribute("href") or "").strip()
    else:
        website = ""

    contact_text = contact.inner_text()
    phone_match = re.search(r"(\+?\d[\d\-(). ]{6,}\d)", contact_text)
    phone = phone_match.group(1).strip() if phone_match else ""

    description_locator = page.locator("#section-description")
    if description_locator.count() > 0:
        description = (description_locator.first.text_content() or "").strip()
    else:
        description = ""

    booth_links = page.locator("#myssidebar a#newfloorplanlink")
    booth_values = [
        (booth_links.nth(i).text_content() or "").strip() for i in range(booth_links.count())
    ]
    booth = "; ".join([value for value in booth_values if value])

    if not all([company_name, address, website, phone, description, booth]):
        return None

    return {
        "company_name": company_name,
        "address": address,
        "website": website,
        "phone": phone,
        "description": description,
        "booth": booth,
    }


def _load_checkpoint() -> dict[str, str] | None:
    checkpoint_path = Path(CHECKPOINT_FILE)
    if not checkpoint_path.exists():
        return None
    with checkpoint_path.open("r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def _save_checkpoint(category: str, subcategory: str, exhibitor_index: int) -> None:
    checkpoint_path = Path(CHECKPOINT_FILE)
    payload = {
        "category": category,
        "subcategory": subcategory,
        "exhibitor_index": exhibitor_index,
    }
    with checkpoint_path.open("w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle)


def _init_csv() -> None:
    csv_path = Path(OUTPUT_CSV)
    if csv_path.exists():
        return
    with csv_path.open("w", encoding="utf-8", newline="") as file_handle:
        writer = csv.DictWriter(file_handle, fieldnames=CSV_HEADERS)
        writer.writeheader()


def _append_row(row: dict[str, str]) -> None:
    with Path(OUTPUT_CSV).open("a", encoding="utf-8", newline="") as file_handle:
        writer = csv.DictWriter(file_handle, fieldnames=CSV_HEADERS)
        writer.writerow(row)


def run(
    *,
    list_categories: bool = False,
    list_subcategories: bool = False,
    fresh: bool = False,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        _init_csv()
        checkpoint = {} if fresh else (_load_checkpoint() or {})
        resume_category = checkpoint.get("category")
        resume_subcategory = checkpoint.get("subcategory")
        resume_exhibitor_index = int(checkpoint.get("exhibitor_index", 0) or 0)
        resume_mode = bool(resume_category and resume_subcategory)
        resume_after_name = "" if fresh else RESUME_AFTER_COMPANY_NAME.strip().lower()
        resume_name_found = not resume_after_name

        _safe_goto(page, START_URL)
        page.wait_for_timeout(1500)
        page.wait_for_selector(CATEGORY_LINK_SELECTOR, timeout=60000)

        categories = _extract_link_text_pairs(page, CATEGORY_LINK_SELECTOR)
        categories = [category for category in categories if category.name != VIEW_ALL_LABEL]
        print(f"Found {len(categories)} categories")
        if list_categories:
            for category in categories:
                print(category.name)
            context.close()
            browser.close()
            return
        if list_subcategories:
            for category in categories:
                _safe_goto(page, category.url)
                page.wait_for_timeout(1000)
                try:
                    page.wait_for_selector(SUBCATEGORY_LINK_SELECTOR, timeout=60000)
                except PlaywrightTimeoutError:
                    print(f"{category.name}: 0 subcategories")
                    continue
                subcategories = _extract_link_text_pairs(page, SUBCATEGORY_LINK_SELECTOR)
                subcategories = [
                    subcategory for subcategory in subcategories if subcategory.name != VIEW_ALL_LABEL
                ]
                print(f"{category.name}: {len(subcategories)} subcategories")
                for subcategory in subcategories:
                    print(f"- {subcategory.name}")
            context.close()
            browser.close()
            return

        category_started = not resume_mode
        for category in categories:
            if not category_started:
                if category.name == resume_category:
                    category_started = True
                else:
                    continue

            _safe_goto(page, category.url)
            page.wait_for_timeout(1000)
            try:
                page.wait_for_selector(SUBCATEGORY_LINK_SELECTOR, timeout=60000)
            except PlaywrightTimeoutError:
                print(f"Category {category.name} has 0 subcategories (no table rows)")
                continue

            subcategories = _extract_link_text_pairs(page, SUBCATEGORY_LINK_SELECTOR)
            subcategories = [
                subcategory for subcategory in subcategories if subcategory.name != VIEW_ALL_LABEL
            ]
            print(f"Category {category.name} has {len(subcategories)} subcategories")

            subcategory_started = not resume_mode or category.name != resume_category
            for subcategory in subcategories:
                if resume_mode and category.name == resume_category and not subcategory_started:
                    if subcategory.name == resume_subcategory:
                        subcategory_started = True
                    continue

                _safe_goto(page, subcategory.url)
                page.wait_for_timeout(1000)
                try:
                    exhibitor_count = _count_exhibitor_cards(page)
                except PlaywrightTimeoutError:
                    print(
                        f"Subcategory {subcategory.name} has 0 exhibitors (no cards)"
                    )
                    _save_checkpoint(category.name, subcategory.name, 0)
                    continue

                print(
                    f"Subcategory {subcategory.name} has {exhibitor_count} exhibitors"
                )

                exhibitor_index = 0
                if resume_mode and category.name == resume_category and subcategory.name == resume_subcategory:
                    exhibitor_index = resume_exhibitor_index
                seen_exhibitors: set[str] = set()
                while exhibitor_index < exhibitor_count:
                    cards = page.locator(EXHIBITOR_LINK_SELECTOR)
                    if exhibitor_index >= cards.count():
                        break

                    page.evaluate(
                        """
                        () => {
                            const overlay = document.querySelector('.introjs-overlay');
                            if (overlay) {
                                overlay.remove();
                            }
                            const skip = document.querySelector('.introjs-skipbutton');
                            if (skip) {
                                skip.click();
                            }
                        }
                        """
                    )

                    card = cards.nth(exhibitor_index)
                    link = card.locator(EXHIBITOR_LINK_IN_CARD)
                    if link.count() == 0:
                        exhibitor_index += 1
                        continue

                    exhibitor_href = link.first.get_attribute("href") or ""
                    if exhibitor_href in seen_exhibitors:
                        exhibitor_index += 1
                        continue
                    seen_exhibitors.add(exhibitor_href)

                    link.first.scroll_into_view_if_needed()
                    link.first.click()
                    page.wait_for_timeout(500)

                    timed_out = False
                    try:
                        details = _extract_exhibitor_details(page)
                    except PlaywrightTimeoutError:
                        print(f"Skipping exhibitor {page.url} due to timeout")
                        details = None
                        timed_out = True

                    if details is None:
                        if not timed_out:
                            print(
                                f"Skipping exhibitor {page.url} due to missing fields"
                            )
                    else:
                        company_name = details["company_name"].strip()
                        if not resume_name_found:
                            if company_name.lower() == resume_after_name:
                                resume_name_found = True
                                print(
                                    f"Found resume company {company_name}, continuing"
                                )
                            page.go_back(wait_until="networkidle")
                            page.wait_for_timeout(1000)
                            exhibitor_index += 1
                            _save_checkpoint(category.name, subcategory.name, exhibitor_index)
                            continue

                        _append_row(
                            {
                                "category": category.name,
                                "subcategory": subcategory.name,
                                **details,
                            }
                        )
                        print(
                            "Exhibitor",
                            details["company_name"],
                            "|",
                            details["booth"],
                            "|",
                            details["phone"],
                        )

                    page.go_back(wait_until="networkidle")
                    page.wait_for_timeout(1000)
                    exhibitor_index += 1
                    _save_checkpoint(category.name, subcategory.name, exhibitor_index)

                _save_checkpoint(category.name, subcategory.name, exhibitor_index)

        context.close()
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Conexpo exhibitor scraper")
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="Print categories and exit",
    )
    parser.add_argument(
        "--list-subcategories",
        action="store_true",
        help="Print categories with subcategories and exit",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore checkpoints and resume hints",
    )
    args = parser.parse_args()
    run(
        list_categories=args.list_categories,
        list_subcategories=args.list_subcategories,
        fresh=args.fresh,
    )
