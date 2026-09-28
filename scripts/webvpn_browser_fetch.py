#!/usr/bin/env python
"""MFA-assisted WebVPN PDF fetcher for the literature-research skill.

This helper is intentionally interactive: it opens a visible browser so the
user can complete institutional MFA, then reuses that authenticated WebVPN
session to fetch pending PDFs from publisher DOI/PDF URLs.
"""

from __future__ import annotations

import argparse
import asyncio
import binascii
import html
import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import urlencode, urljoin, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
LIT_PATH = SCRIPT_DIR / "lit.py"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
spec = importlib.util.spec_from_file_location("lit", LIT_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot import {LIT_PATH}")
lit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lit
spec.loader.exec_module(lit)


def default_browser_path() -> str:
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", ""),
        str(Path(local_appdata) / "Google" / "Chrome" / "Application" / "chrome.exe") if local_appdata else "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for executable in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(executable)
        if found:
            candidates.append(found)
    for item in candidates:
        if item and Path(item).exists():
            return item
    return ""


def profile_path(value: Any, pdir: Path) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.replace("{project_dir}", str(pdir)).replace("{data_root}", str(pdir.parent))
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        path = pdir / path
    return str(path)


def webvpn_encoded_url(vpn_url: str, target: str) -> str:
    from cryptography.hazmat.backends import default_backend  # type: ignore
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore

    key = b"wrdvpnisthebest!"
    iv = b"wrdvpnisthebest!"
    marker_hex = binascii.hexlify(key).decode("ascii")
    parsed = urlparse(target)
    host = parsed.hostname or ""
    if parsed.port and not (parsed.scheme == "https" and parsed.port == 443) and not (parsed.scheme == "http" and parsed.port == 80):
        host = f"{host}:{parsed.port}"
    cipher = Cipher(algorithms.AES(key), modes.CFB(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    enc_host = marker_hex + binascii.hexlify(encryptor.update(host.encode("utf-8")) + encryptor.finalize()).decode("ascii")
    path = parsed.path or ""
    if parsed.query:
        path += f"?{parsed.query}"
    return f"{vpn_url.rstrip('/')}/{parsed.scheme}/{enc_host}{path}"


def candidate_urls(row: Any) -> List[Tuple[str, str]]:
    doi = row["doi"] or ""
    journal = (row["journal"] or "").lower()
    urls: List[Tuple[str, str]] = []
    if doi:
        if doi.startswith("10.1126/"):
            urls.append(("publisher_pdf", f"https://www.science.org/doi/pdf/{doi}?download=true"))
            urls.append(("publisher_pdf", f"https://www.science.org/doi/pdf/{doi}"))
        if doi.startswith("10.1021/"):
            if row["pdf_url"]:
                urls.append(("direct", row["pdf_url"]))
            urls.extend(("publisher", u) for u in lit.publisher_pdf_candidates(doi))
            urls.append(("article", f"https://pubs.acs.org/doi/{doi}"))
            if row["landing_url"]:
                urls.append(("landing", row["landing_url"]))
            seen = set()
            out = []
            for method, url in urls:
                if url and url not in seen:
                    seen.add(url)
                    out.append((method, url))
            return out
        urls.append(("doi", f"https://doi.org/{doi}"))
        if doi.startswith("10.1021/"):
            urls.append(("article", f"https://pubs.acs.org/doi/{doi}"))
        elif doi.startswith("10.1002/"):
            urls.append(("article", f"https://onlinelibrary.wiley.com/doi/{doi}"))
        elif doi.startswith("10.1038/"):
            urls.append(("article", f"https://www.nature.com/articles/{doi.rsplit('/', 1)[-1]}"))
        elif doi.startswith("10.1039/"):
            urls.append(("article", f"https://pubs.rsc.org/en/content/articlelanding/{doi.rsplit('/', 1)[-1]}"))
        elif doi.startswith("10.1016/"):
            for candidate in lit.publisher_pdf_candidates(doi):
                match = re.search(r"/pii/([A-Z0-9]+)", candidate, flags=re.I)
                if match:
                    urls.append(("article", f"https://www.sciencedirect.com/science/article/pii/{match.group(1)}"))
                    break
    if row["landing_url"]:
        urls.append(("landing", row["landing_url"]))
    if row["pdf_url"]:
        urls.append(("direct", row["pdf_url"]))
    if doi:
        publisher_urls = lit.publisher_pdf_candidates(doi)
        if doi.startswith("10.1016/") and not any(name in journal for name in ("cell", "heliyon")):
            publisher_urls = [u for u in publisher_urls if "www.cell.com/action/showPdf" not in u]
        urls.extend(("publisher", u) for u in publisher_urls)
    seen = set()
    out = []
    for method, url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append((method, url))
    return out


def select_pending_rows(
    conn: Any, profile: Dict[str, Any], limit: int, doi_prefix: str = "", skip_dois: Sequence[str] = ()
) -> List[Any]:
    """Select pending PDFs in the same spirit as priority_pdf_queue.csv."""
    doi_prefix = (doi_prefix or "").strip().lower()
    skip_dois_norm = {lit.normalize_doi(item) for item in skip_dois if lit.normalize_doi(item)}
    rows = conn.execute(
        """
        SELECT * FROM papers
        WHERE status='pending_pdf'
        """
    ).fetchall()
    if doi_prefix:
        rows = [row for row in rows if (row["doi"] or "").lower().startswith(doi_prefix)]
    if skip_dois_norm:
        rows = [row for row in rows if lit.normalize_doi(row["doi"] or "") not in skip_dois_norm]
    ranked = sorted(
        rows,
        key=lambda row: (
            1 if lit.priority_journal_match(row["journal"], profile) else 0,
            float(row["relevance_score"] or 0),
            int(row["citation_count"] or 0),
            int(row["year"] or 0),
        ),
        reverse=True,
    )
    return ranked[:limit]


async def maybe_login(page: Any, vpn_url: str, username: str, password: str, timeout_s: int) -> None:
    await page.goto(vpn_url, wait_until="networkidle", timeout=60_000)
    username_selector = "#nameInput, input[name='username']:not([type='hidden']), input[placeholder*='学工号'], input[placeholder*='GID']"
    password_selector = "input[type='password'], input[placeholder*='密码']"
    try:
        await page.wait_for_selector(username_selector, state="visible", timeout=20_000)
        await page.locator(username_selector).first.fill(username)
        await page.locator(password_selector).first.fill(password)
        submit = page.locator("#submitBtn, button:has-text('立即登录'), button[type='submit']").first
        await submit.click()
    except Exception:
        # Some institutions use custom login/MFA flows. In that case keep the
        # visible browser open and let the user complete authentication.
        pass
    print("[webvpn-browser] Complete MFA in the opened browser if prompted.")
    deadline = asyncio.get_event_loop().time() + timeout_s
    vpn_host = urlparse(vpn_url).netloc
    while asyncio.get_event_loop().time() < deadline:
        await try_submit_login(page, username, password)
        try:
            if vpn_host in urlparse(page.url).netloc and not await is_login_page(page):
                if await probe_webvpn_session(page, vpn_url):
                    print("[webvpn-browser] WebVPN session is active.")
                    return
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    raise TimeoutError("WebVPN login timed out before an authenticated session was detected")


async def wait_for_manual_login(page: Any, vpn_url: str, timeout_s: int) -> bool:
    vpn_host = urlparse(vpn_url).netloc
    deadline = asyncio.get_event_loop().time() + timeout_s
    next_notice = 0.0
    while asyncio.get_event_loop().time() < deadline:
        now = asyncio.get_event_loop().time()
        if now >= next_notice:
            print("[webvpn-browser] Waiting for manual WebVPN/MFA completion in the opened browser...")
            next_notice = now + 30
        try:
            if vpn_host in urlparse(page.url).netloc and not await is_login_page(page):
                if await probe_webvpn_session(page, vpn_url):
                    print("[webvpn-browser] WebVPN session is active.")
                    return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return False


async def try_submit_login(page: Any, username: str, password: str) -> None:
    try:
        if not await page.locator('input[type="password"]').count():
            return
        username_box = page.locator("#nameInput, input[name='username']:not([type='hidden'])").first
        if await username_box.count():
            await username_box.fill(username)
        await page.locator('input[type="password"]').first.fill(password)
        submit = page.locator("#submitBtn, button[type='submit'], input[type='submit']").first
        if await submit.count():
            await submit.click()
    except Exception:
        pass


async def is_login_page(page: Any) -> bool:
    current = (page.url or "").lower()
    if "cas/login" in current:
        return True
    try:
        if await page.locator('input[type="password"]').count():
            return True
    except Exception:
        pass
    return False


async def probe_webvpn_session(page: Any, vpn_url: str) -> bool:
    probe_url = webvpn_encoded_url(vpn_url, "https://pubs.acs.org/doi/10.1021/acs.est.1c07739")
    vpn_host = urlparse(vpn_url).netloc
    try:
        await page.goto(probe_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1000)
    except Exception:
        pass
    if await is_login_page(page):
        return False
    if vpn_host not in urlparse(page.url or "").netloc:
        return False
    try:
        user_agent = await page.evaluate("navigator.userAgent")
    except Exception:
        user_agent = lit.USER_AGENT
    try:
        status, _, _, final_url = await browser_context_http_get(
            page.context,
            probe_url,
            timeout_s=20,
            user_agent=user_agent,
            accept="text/html,application/xhtml+xml,application/pdf,*/*",
        )
    except Exception:
        return False
    final_lower = (final_url or "").lower()
    if "cas/login" in final_lower:
        return False
    return status < 500 and vpn_host in urlparse(final_url or "").netloc


async def page_pdf_links(page: Any) -> List[str]:
    raw_links = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a, iframe, embed'))
            .map(a => ({
                text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(),
                href: a.href || a.src || a.getAttribute('href') || a.getAttribute('src') || ''
            }))
            .filter(x => x.href && /pdf|epdf|pdfft|download/i.test((x.text || '') + ' ' + (x.href || '')))
            .slice(0, 80)
        """
    )
    ranked: List[Tuple[int, str]] = []
    for item in raw_links:
        href = str(item.get("href") or "")
        text = str(item.get("text") or "")
        combined = f"{text} {href}".lower()
        if not href:
            continue
        if any(bad in combined for bad in ("high-res image", "full-size image", "content/image", ".jpg", ".jpeg", ".png", ".gif", "download full issue")):
            continue
        if any(bad in combined for bad in ("supporting information", "supplementary information", "supplementary material", "supplementary materials", "supporting-info", "/doi/suppl/", "/suppl_file/", "suppl_file", "_si_", "-si-", "_supp", "suppinfo", "supportinfo")):
            continue
        score = 0
        if "pdfft" in href.lower():
            score = 100
        elif re.search(r"view\s*pdf|download\s*pdf|pdf", text, flags=re.I):
            score = 90
        elif re.search(r"/pdf(?:[/?#]|$)", href, flags=re.I):
            score = 80
        elif "epdf" in href.lower():
            score = 70
        elif ".pdf" in href.lower():
            score = 60
        else:
            continue
        ranked.append((score, href))
    ranked.sort(key=lambda item: item[0], reverse=True)
    out: List[str] = []
    for _, link in ranked:
        if link not in out:
            out.append(link)
    return out


async def response_is_pdf(response: Any, out_path: Path) -> Tuple[bool, int]:
    if not response:
        return False, 0
    if lit.is_supplementary_pdf_url(getattr(response, "url", "") or ""):
        return False, 0
    try:
        body = await response.body()
    except Exception:
        return False, 0
    headers = {k.lower(): v for k, v in response.headers.items()}
    if lit.write_pdf_if_valid(out_path, body, headers):
        ok, pages, _ = lit.verify_pdf(out_path)
        if ok:
            return True, pages
        out_path.unlink(missing_ok=True)
    else:
        out_path.unlink(missing_ok=True)
    return False, 0


def likely_pdf_response(response: Any) -> bool:
    url = (getattr(response, "url", "") or "").lower()
    if lit.is_supplementary_pdf_url(url):
        return False
    try:
        headers = {k.lower(): v for k, v in response.headers.items()}
    except Exception:
        headers = {}
    ctype = headers.get("content-type", "").lower()
    return (
        "pdf" in ctype
        or "main.pdf" in url
        or "/pdfft" in url
        or re.search(r"/pdf(?:[/?#]|$)", url, flags=re.I) is not None
    )


def is_sciencedirect_main_pdf_url(url: str) -> bool:
    try:
        return "/main.pdf" in urlparse(url).path.lower()
    except Exception:
        return False


async def save_download_if_pdf(download: Any, out_path: Path) -> Tuple[bool, int]:
    tmp_path = out_path.with_suffix(".download")
    try:
        await download.save_as(str(tmp_path))
        body = tmp_path.read_bytes()
    except Exception:
        return False, 0
    finally:
        tmp_path.unlink(missing_ok=True)
    if lit.write_pdf_if_valid(out_path, body, {}):
        ok, pages, _ = lit.verify_pdf(out_path)
        if ok:
            return True, pages
        out_path.unlink(missing_ok=True)
    return False, 0


async def save_pdf_viewer_download(page: Any, out_path: Path) -> Tuple[bool, int]:
    try:
        is_pdf_viewer = await page.evaluate(
            """() => !!document.querySelector('embed[type*="pdf" i], iframe[type*="pdf" i]')"""
        )
    except Exception:
        is_pdf_viewer = False
    if not is_pdf_viewer and "/doi/pdf/" not in (page.url or "").lower():
        return False, 0
    try:
        async with page.expect_download(timeout=12_000) as download_info:
            await page.keyboard.press("Control+S")
        download = await download_info.value
    except Exception:
        return False, 0
    return await save_download_if_pdf(download, out_path)


async def capture_pdf_during(page: Any, out_path: Path, action: Any, timeout_ms: int = 30_000) -> Tuple[bool, int]:
    done = asyncio.Event()
    result = {"pages": 0}
    context = page.context
    watched_pages: List[Any] = []
    context_response_attached = False

    async def inspect_response(response: Any) -> None:
        if done.is_set() or not likely_pdf_response(response):
            return
        ok, pages = await response_is_pdf(response, out_path)
        if ok:
            result["pages"] = pages
            done.set()

    def response_handler(response: Any) -> None:
        asyncio.create_task(inspect_response(response))

    async def inspect_download(download: Any) -> None:
        if done.is_set():
            return
        ok, pages = await save_download_if_pdf(download, out_path)
        if ok:
            result["pages"] = pages
            done.set()

    def download_handler(download: Any) -> None:
        asyncio.create_task(inspect_download(download))

    def attach_page_handlers(target_page: Any) -> None:
        watched_pages.append(target_page)
        try:
            target_page.on("download", download_handler)
        except Exception:
            pass
        if not context_response_attached:
            try:
                target_page.on("response", response_handler)
            except Exception:
                pass

    def page_handler(new_page: Any) -> None:
        attach_page_handlers(new_page)

    try:
        context.on("response", response_handler)
        context_response_attached = True
    except Exception:
        context_response_attached = False
    try:
        context.on("page", page_handler)
    except Exception:
        pass
    attach_page_handlers(page)
    try:
        try:
            await action()
        except Exception:
            pass
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            context.remove_listener("response", response_handler)
        except Exception:
            pass
        try:
            context.remove_listener("page", page_handler)
        except Exception:
            pass
        for target_page in watched_pages:
            try:
                target_page.remove_listener("download", download_handler)
            except Exception:
                pass
            if not context_response_attached:
                try:
                    target_page.remove_listener("response", response_handler)
                except Exception:
                    pass
    return done.is_set(), int(result["pages"] or 0)


async def click_pdf_control_capture(page: Any, out_path: Path) -> Tuple[bool, int]:
    handle = await page.evaluate_handle(
        """() => {
            const norm = (value) => (value || '')
                .replace(/[\u00a0\u2000-\u200b\u202f\u205f\u3000\u807d]/g, ' ')
                .replace(/\s+/g, ' ')
                .trim()
                .toLowerCase();
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.visibility !== 'hidden' && style.display !== 'none'
                    && rect.width > 0 && rect.height > 0;
            };
            const score = (item) => {
                const text = item.text;
                const href = item.href;
                if (/view.{0,16}pdf/.test(text) || (text.includes('view') && text.includes('pdf'))) return 100;
                if (/download.{0,16}pdf/.test(text) || (text.includes('download') && text.includes('pdf'))) return 95;
                if (/\/pdfft/.test(href)) return 90;
                if (/\/pdf(?:[/?#]|$)/.test(href)) return 85;
                if (/pdf/.test(text)) return 70;
                if (/\.pdf(?:[?#]|$)/.test(href)) return 65;
                return 0;
            };
            const elements = Array.from(document.querySelectorAll('a, button, [role="button"]'));
            const candidates = elements
                .map((el) => ({
                    el,
                    text: norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.title),
                    href: norm(el.getAttribute('href') || el.href || '')
                }))
                .filter((item) => visible(item.el))
                .filter((item) => !/high-res image|full-size image|download full issue|graphical abstract/.test(item.text + ' ' + item.href))
                .filter((item) => !/supporting information|supplementary information|supplementary material|supplementary materials|supporting-info|\\/doi\\/suppl\\/|suppl_file|_si_|-si-/.test(item.text + ' ' + item.href))
                .map((item) => ({...item, rank: score(item)}))
                .filter((item) => item.rank > 0)
                .sort((a, b) => b.rank - a.rank);
            return candidates.length ? candidates[0].el : null;
        }"""
    )
    element = handle.as_element()
    if not element:
        await handle.dispose()
        return False, 0

    async def action() -> None:
        await element.scroll_into_view_if_needed(timeout=5000)
        try:
            await element.click(timeout=10_000)
        except Exception:
            await page.evaluate("(el) => el.click()", element)

    try:
        return await capture_pdf_during(page, out_path, action, timeout_ms=35_000)
    finally:
        await handle.dispose()


async def capture_pdf_response(page: Any, out_path: Path, timeout_ms: int = 20_000) -> Tuple[bool, int]:
    done = asyncio.Event()
    result = {"pages": 0}

    async def inspect_response(response: Any) -> None:
        if done.is_set():
            return
        url = (response.url or "").lower()
        headers = {k.lower(): v for k, v in response.headers.items()}
        ctype = headers.get("content-type", "").lower()
        if "pdf" not in ctype and "main.pdf" not in url and "/pdfft" not in url:
            return
        try:
            body = await response.body()
        except Exception:
            return
        if lit.write_pdf_if_valid(out_path, body, headers):
            ok, pages, _ = lit.verify_pdf(out_path)
            if ok:
                result["pages"] = pages
                done.set()
                return
            out_path.unlink(missing_ok=True)

    def handler(response: Any) -> None:
        asyncio.create_task(inspect_response(response))

    page.on("response", handler)
    try:
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            page.remove_listener("response", handler)
        except Exception:
            pass
    return done.is_set(), int(result["pages"] or 0)


async def goto_capture_pdf(page: Any, url: str, out_path: Path, wait_after_ms: int = 5000) -> Tuple[Any, bool, int]:
    done = asyncio.Event()
    result = {"pages": 0}

    async def inspect_response(response: Any) -> None:
        if done.is_set():
            return
        resp_url = (response.url or "").lower()
        headers = {k.lower(): v for k, v in response.headers.items()}
        ctype = headers.get("content-type", "").lower()
        if "pdf" not in ctype and "main.pdf" not in resp_url and "/pdfft" not in resp_url:
            return
        try:
            body = await response.body()
        except Exception:
            return
        if lit.write_pdf_if_valid(out_path, body, headers):
            ok, pages, _ = lit.verify_pdf(out_path)
            if ok:
                result["pages"] = pages
                done.set()
                return
            out_path.unlink(missing_ok=True)

    def handler(response: Any) -> None:
        asyncio.create_task(inspect_response(response))

    async def inspect_download(download: Any) -> None:
        if done.is_set():
            return
        ok, pages = await save_download_if_pdf(download, out_path)
        if ok:
            result["pages"] = pages
            done.set()

    def download_handler(download: Any) -> None:
        asyncio.create_task(inspect_download(download))

    page.on("response", handler)
    try:
        page.on("download", download_handler)
    except Exception:
        pass
    response = None
    try:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            if "Download is starting" not in str(exc):
                raise
        ok, pages = await response_is_pdf(response, out_path)
        if ok:
            return response, True, pages
        try:
            await asyncio.wait_for(done.wait(), timeout=wait_after_ms / 1000)
        except asyncio.TimeoutError:
            pass
        if done.is_set():
            return response, True, int(result["pages"] or 0)
        return response, False, 0
    finally:
        try:
            page.remove_listener("response", handler)
        except Exception:
            pass
        try:
            page.remove_listener("download", download_handler)
        except Exception:
            pass


def html_pdf_links(html: str, base_url: str) -> List[str]:
    links: List[str] = []
    for link in sciencedirect_pdf_links_from_html(html):
        if link not in links:
            links.append(link)
    patterns = [
        r'(?:href|src)=["\']([^"\']*pdf[^"\']*)["\']',
        r'href=["\']([^"\']*pdf[^"\']*)["\']',
        r'data-pdf-url=["\']([^"\']+)["\']',
        r'"pdfUrl":\s*"([^"]+)"',
        r'citation_pdf_url["\s]*content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, html or "", flags=re.I):
            url = urljoin(base_url, globals()["html"].unescape(match))
            combined = url.lower()
            if lit.is_supplementary_pdf_url(url) or any(
                bad in combined
                for bad in (
                    "supporting information",
                    "supplementary information",
                    "supplementary material",
                    "supplementary materials",
                    "supporting-info",
                    "/doi/suppl/",
                    "/suppl_file/",
                    "suppl_file",
                    "_si_",
                    "-si-",
                    "_supp",
                    "suppinfo",
                    "supportinfo",
                )
            ):
                continue
            if url not in links:
                links.append(url)
    return links[:12]


def sciencedirect_pdf_links_from_html(page_html: str) -> List[str]:
    text = globals()["html"].unescape(page_html or "")
    text = text.replace("\\u002F", "/").replace("\\/", "/")
    links: List[str] = []

    def first(pattern: str, segment: str) -> str:
        match = re.search(pattern, segment, flags=re.I | re.S)
        return match.group(1) if match else ""

    for match in re.finditer(r'"pdfDownload"\s*:', text, flags=re.I):
        segment = text[match.start() : match.start() + 6000]
        md5 = first(r'"md5"\s*:\s*"([^"]+)"', segment)
        pid = first(r'"pid"\s*:\s*"([^"]+)"', segment)
        pii = first(r'"pii"\s*:\s*"([^"]+)"', segment)
        pdf_extension = first(r'"pdfExtension"\s*:\s*"([^"]+)"', segment) or "/pdfft"
        path = first(r'"path"\s*:\s*"([^"]+)"', segment) or "science/article/pii"
        if not pii:
            pii_match = re.search(r"1-s2\.0-([A-Z0-9]+)-main\.pdf", pid, flags=re.I)
            pii = pii_match.group(1) if pii_match else ""
        if not (md5 and pid and pii):
            continue
        pdf_extension = pdf_extension if pdf_extension.startswith("/") else f"/{pdf_extension}"
        path = path.strip("/")
        query = urlencode({"md5": md5, "pid": pid})
        url = f"https://www.sciencedirect.com/{path}/{pii}{pdf_extension}?{query}"
        for candidate in (url, f"{url}&download=true"):
            if candidate not in links:
                links.append(candidate)
    return links


async def browser_context_http_get(
    context: Any,
    url: str,
    timeout_s: int = 120,
    user_agent: str | None = None,
    accept: str | None = None,
    referer: str | None = None,
) -> Tuple[int, Dict[str, str], bytes, str]:
    """GET a URL with the cookies from the authenticated browser context."""
    try:
        import httpx  # type: ignore
    except Exception as exc:
        raise RuntimeError("httpx is required for WebVPN cookie-bridge downloads") from exc

    cookies = httpx.Cookies()
    for cookie in await context.cookies():
        domain = cookie.get("domain") or urlparse(url).hostname or ""
        path = cookie.get("path") or "/"
        cookies.set(cookie["name"], cookie["value"], domain=domain, path=path)
    headers = {
        "User-Agent": user_agent or lit.USER_AGENT,
        "Accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    async with httpx.AsyncClient(
        cookies=cookies,
        follow_redirects=True,
        verify=lit.webvpn_verify_tls(),
        timeout=timeout_s,
        headers=headers,
    ) as client:
        resp = await client.get(url)
        return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, resp.content, str(resp.url)


async def fetch_sciencedirect_pdf_from_page(
    page: Any, vpn_url: str, out_path: Path, user_agent: str | None = None
) -> Tuple[bool, int]:
    try:
        links = sciencedirect_pdf_links_from_html(await page.content())
    except Exception:
        links = []
    if links:
        print(f"[webvpn-browser] ScienceDirect metadata candidates={len(links)}")
    for target in links:
        encoded = target if urlparse(target).netloc == urlparse(vpn_url).netloc else webvpn_encoded_url(vpn_url, target)
        try:
            try:
                await page.goto(encoded, wait_until="domcontentloaded", timeout=90_000)
            except Exception:
                pass
            deadline = asyncio.get_event_loop().time() + 35
            while asyncio.get_event_loop().time() < deadline:
                if is_sciencedirect_main_pdf_url(page.url or ""):
                    break
                await page.wait_for_timeout(1000)
            final_url = page.url
            if not is_sciencedirect_main_pdf_url(final_url or ""):
                print("[webvpn-browser] ScienceDirect metadata did not resolve to main.pdf")
                continue
            await page.wait_for_timeout(1500)
            status, headers, body, _ = await browser_context_http_get(
                page.context,
                final_url,
                user_agent=user_agent,
                accept="application/pdf,*/*",
                referer=encoded,
            )
            print(
                "[webvpn-browser] ScienceDirect final GET "
                f"status={status} ctype={headers.get('content-type', '')[:60]} size={len(body)}"
            )
            if status == 200 and lit.write_pdf_if_valid(out_path, body, headers):
                ok, pages, _ = lit.verify_pdf(out_path)
                if ok:
                    return True, pages
            out_path.unlink(missing_ok=True)
        except Exception:
            out_path.unlink(missing_ok=True)
            continue
    return False, 0


async def fetch_one_via_cookie_bridge(context: Any, vpn_url: str, row: Any, pdf_dir: Path, user_agent: str | None = None) -> Tuple[str | None, str | None, str, int]:
    out_path = pdf_dir / (lit.sanitize_filename(row["id"]) + ".pdf")
    if out_path.exists():
        ok, pages, _ = lit.verify_pdf(out_path)
        if ok:
            return str(out_path), "existing", "ok", pages
    seen = set()
    queue: List[Tuple[str, str]] = []
    for method, target in candidate_urls(row):
        proxied = target if urlparse(target).netloc == urlparse(vpn_url).netloc else webvpn_encoded_url(vpn_url, target)
        queue.append((method, proxied))
    while queue:
        method, url = queue.pop(0)
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            status, headers, body, final_url = await browser_context_http_get(context, url, user_agent=user_agent)
        except Exception:
            continue
        if status == 200 and lit.write_pdf_if_valid(out_path, body, headers):
            ok, pages, _ = lit.verify_pdf(out_path)
            if ok:
                return str(out_path), f"webvpn_cookie:{method}", "ok", pages
            out_path.unlink(missing_ok=True)
        ctype = headers.get("content-type", "")
        if "html" in ctype.lower() or body[:20].lstrip().lower().startswith(b"<!doctype") or body[:20].lstrip().lower().startswith(b"<html"):
            text = body.decode("utf-8", errors="replace")
            for link in html_pdf_links(text, final_url):
                proxied = link if urlparse(link).netloc == urlparse(vpn_url).netloc else webvpn_encoded_url(vpn_url, link)
                if proxied not in seen:
                    queue.append((f"{method}:link", proxied))
    return None, None, "missing", 0


async def fetch_one(page: Any, vpn_url: str, row: Any, pdf_dir: Path, debug_dir: Path | None = None) -> Tuple[str | None, str | None, str, int]:
    out_path = pdf_dir / (lit.sanitize_filename(row["id"]) + ".pdf")
    if out_path.exists():
        ok, pages, _ = lit.verify_pdf(out_path)
        if ok:
            return str(out_path), "existing", "ok", pages
    attempts: List[Tuple[str, str]] = candidate_urls(row)
    seen_attempts = set()
    for method, target in attempts:
        if target in seen_attempts:
            continue
        seen_attempts.add(target)
        try:
            encoded = target if urlparse(target).netloc == urlparse(vpn_url).netloc else webvpn_encoded_url(vpn_url, target)
            response, ok, pages = await goto_capture_pdf(page, encoded, out_path, wait_after_ms=8000)
            if ok:
                return str(out_path), f"webvpn_browser:{method}", "ok", pages
            ok, pages = await save_pdf_viewer_download(page, out_path)
            if ok:
                return str(out_path), f"webvpn_browser:{method}:viewer-download", "ok", pages
            try:
                user_agent = await page.evaluate("navigator.userAgent")
            except Exception:
                user_agent = lit.USER_AGENT
            ok, pages = await fetch_sciencedirect_pdf_from_page(page, vpn_url, out_path, user_agent)
            if ok:
                return str(out_path), f"webvpn_browser:{method}:sciencedirect-metadata", "ok", pages
            ok, pages = await click_pdf_control_capture(page, out_path)
            if ok:
                return str(out_path), f"webvpn_browser:{method}:click", "ok", pages
            for link in await page_pdf_links(page):
                try:
                    absolute = urljoin(page.url, link)
                    next_url = absolute if urlparse(absolute).netloc == urlparse(vpn_url).netloc else webvpn_encoded_url(vpn_url, absolute)
                    response, ok, pages = await goto_capture_pdf(page, next_url, out_path, wait_after_ms=8000)
                    if ok:
                        return str(out_path), f"webvpn_browser:{method}:link", "ok", pages
                except Exception:
                    continue
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = lit.sanitize_filename(f"{row['id']}_{method}")[:120]
                try:
                    await page.screenshot(path=str(debug_dir / f"{safe}.png"), full_page=True)
                    text = await page.locator("body").inner_text(timeout=10_000)
                    (debug_dir / f"{safe}.txt").write_text(
                        f"url: {page.url}\ntitle: {await page.title()}\n\n{text[:4000]}",
                        encoding="utf-8",
                    )
                except Exception:
                    pass
        except Exception as exc:
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = lit.sanitize_filename(f"{row['id']}_{method}_error")[:120]
                (debug_dir / f"{safe}.txt").write_text(str(exc), encoding="utf-8")
            continue
    return None, None, "missing", 0


async def run(args: argparse.Namespace) -> None:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:
        raise RuntimeError("Install Playwright first: python -m pip install --user playwright") from exc

    lit.load_env_file(args.env_file)
    profile = lit.load_profile(args.profile)
    pdir = lit.ensure_project(profile)
    pdf_dir = pdir / "pdfs"
    vpn_url = os.environ.get("WEBVPN_URL")
    username = os.environ.get("WEBVPN_USERNAME")
    password = os.environ.get("WEBVPN_PASSWORD")
    if not (vpn_url and username and password):
        raise RuntimeError("WEBVPN_URL, WEBVPN_USERNAME, and WEBVPN_PASSWORD are required")

    conn = lit.connect(pdir / "library.sqlite")
    try:
        rows = select_pending_rows(conn, profile, args.limit, args.doi_prefix, args.skip_doi)
        configured_browser = profile_path(profile.get("webvpn_browser_path"), pdir)
        browser_path = args.browser or configured_browser or default_browser_path()
        configured_profile = profile_path(profile.get("webvpn_user_data_dir"), pdir)
        shared_profile = os.environ.get("LIT_WEBVPN_CHROME_PROFILE") or str(pdir.parent / ".webvpn_chrome_profile")
        user_data_dir = Path(args.user_data_dir or configured_profile or shared_profile)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        login_timeout = args.login_timeout or int(profile.get("webvpn_login_timeout", 180) or 180)
        manual_wait_timeout = args.manual_wait_timeout or int(profile.get("webvpn_manual_wait_timeout", 600) or 600)
        max_consecutive_failures = args.max_consecutive_failures
        if max_consecutive_failures is None:
            max_consecutive_failures = int(profile.get("webvpn_max_consecutive_failures", 3) or 0)
        print(f"[webvpn-browser] using browser: {browser_path or 'Playwright Chromium'}")
        print(f"[webvpn-browser] using profile: {user_data_dir}")
        async with async_playwright() as p:
            launch_options: Dict[str, Any] = {
                "headless": False,
                "accept_downloads": True,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if browser_path:
                launch_options["executable_path"] = browser_path
            context = await p.chromium.launch_persistent_context(
                str(user_data_dir),
                **launch_options,
            )
            page = await context.new_page()
            try:
                await maybe_login(page, vpn_url, username, password, login_timeout)
            except TimeoutError:
                if args.keep_open_on_timeout:
                    print("[webvpn-browser] Login timed out. Browser will stay open for manual MFA/login completion.")
                    if not await wait_for_manual_login(page, vpn_url, manual_wait_timeout):
                        print("[webvpn-browser] Manual login wait timed out; stopping without changes.")
                        await context.close()
                        return
                else:
                    raise
            ok_count = 0
            missing = 0
            consecutive_missing = 0
            debug_dir = Path(args.debug_dir) if args.debug_dir else None
            try:
                browser_user_agent = await page.evaluate("navigator.userAgent")
            except Exception:
                browser_user_agent = lit.USER_AGENT
            for idx, row in enumerate(rows, 1):
                print(f"[webvpn-browser] fetch {idx}/{len(rows)}: {(row['title'] or '')[:90]}")
                doi = (row["doi"] or "").lower()
                if doi.startswith("10.1016/"):
                    path, method, status, pages = await fetch_one(page, vpn_url, row, pdf_dir, debug_dir)
                    if not path:
                        path, method, status, pages = await fetch_one_via_cookie_bridge(context, vpn_url, row, pdf_dir, browser_user_agent)
                elif doi.startswith("10.1021/"):
                    path, method, status, pages = await fetch_one(page, vpn_url, row, pdf_dir, debug_dir)
                    if not path:
                        path, method, status, pages = await fetch_one_via_cookie_bridge(context, vpn_url, row, pdf_dir, browser_user_agent)
                else:
                    path, method, status, pages = await fetch_one_via_cookie_bridge(context, vpn_url, row, pdf_dir, browser_user_agent)
                    if not path:
                        path, method, status, pages = await fetch_one(page, vpn_url, row, pdf_dir, debug_dir)
                if path:
                    ok_count += 1
                    consecutive_missing = 0
                    conn.execute(
                        "UPDATE papers SET pdf_path=?, pdf_method=?, pdf_status=?, pdf_pages=?, status='pending_fulltext', updated_at=? WHERE id=?",
                        (path, method, status, pages, lit.utc_now(), row["id"]),
                    )
                else:
                    missing += 1
                    consecutive_missing += 1
                    conn.execute("UPDATE papers SET pdf_status='missing', updated_at=? WHERE id=?", (lit.utc_now(), row["id"]))
                conn.commit()
                if max_consecutive_failures and consecutive_missing >= max_consecutive_failures:
                    print(
                        f"[webvpn-browser] stopped after {consecutive_missing} consecutive failures; adjust strategy before continuing."
                    )
                    break
            await context.close()
        lit.write_pdf_manifest(conn, pdir, profile)
        lit.write_missing_pdf(conn, pdir, profile)
        lit.record_run(conn, "webvpn_browser_fetch", {"targets": len(rows), "ok": ok_count, "missing": missing})
        print(f"[webvpn-browser] complete: ok={ok_count}, missing={missing}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MFA-assisted WebVPN PDF fetcher")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--login-timeout", type=int, help="Seconds to wait for automatic WebVPN login before manual MFA fallback; defaults to profile webvpn_login_timeout or 180")
    parser.add_argument("--manual-wait-timeout", type=int, help="Seconds to wait for manual MFA/login after login-timeout when --keep-open-on-timeout is used; defaults to profile webvpn_manual_wait_timeout or 600")
    parser.add_argument("--browser", help="Path to Edge/Chrome executable")
    parser.add_argument("--user-data-dir", help="Persistent browser profile directory for WebVPN cookies")
    parser.add_argument("--keep-open-on-timeout", action="store_true", help="Keep browser open after login timeout and wait for Enter")
    parser.add_argument("--debug-dir", help="Optional directory for failed page screenshots/text")
    parser.add_argument("--max-consecutive-failures", type=int, help="Stop early after this many consecutive missing PDFs; defaults to profile webvpn_max_consecutive_failures or 3; use 0 to disable")
    parser.add_argument("--doi-prefix", default="", help="Optional DOI prefix filter, e.g. 10.1016/ for ScienceDirect batches")
    parser.add_argument("--skip-doi", action="append", default=[], help="DOI to skip for this run; can be supplied multiple times")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
