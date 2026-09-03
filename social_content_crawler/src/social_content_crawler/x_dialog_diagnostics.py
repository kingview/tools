"""Private, bounded DOM summaries and cropped screenshots of blocked dialogs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid

from playwright.sync_api import Locator, Page

from .diagnostics import log_directory, record_exception, redact


PRIVATE_FIELDS = 'input, textarea, select, [contenteditable="true"], [role="textbox"]'


def _safe_text(value: str) -> str:
    value = redact(value)
    value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]", value, flags=re.I)
    return re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[NUMBER]", value)


def _scrub(value):
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def capture_dialog(page: Page, dialog: Locator, category: str) -> Path | None:
    directory = None
    try:
        root = log_directory() / "x-dialogs"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12])
        directory.mkdir(mode=0o700)
        # No outerHTML, input values, URLs, hidden data or storage. Read visible
        # text nodes only; the live DOM is never changed.
        dom = dialog.evaluate("""(root, privateFields) => {
          const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
          const excluded=privateFields+', script, style, [hidden], [aria-hidden="true"]';
          const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
          const text=[]; let size=0,node,visited=0;
          while((node=walker.nextNode()) && ++visited<10000 && size<8000) {
            const e=node.parentElement;
            if(!e.closest(excluded) && visible(e)) {text.push(node.textContent);size+=node.textContent.length;}
          }
          const nodes=[root,...Array.from(root.querySelectorAll('*'))].filter(visible).slice(0,200);
          return {text:text.join(' ').slice(0,8000),nodes:nodes.map(e=>({
            tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),
            testid:e.getAttribute('data-testid'),label:e.matches(privateFields)?null:e.getAttribute('aria-label')
          }))};
        }""", PRIVATE_FIELDS)
        # If visible prose itself contains a credential/contact value, mask the
        # entire crop as well. Redacting JSON alone cannot redact image pixels.
        sensitive_prose = _safe_text(dom["text"]) != dom["text"]
        payload = {"category": category, "dom": dom, "screenshot": "dialog.png",
                   "screenshot_fully_masked": sensitive_prose}
        _write_private(directory / "dom.json", json.dumps(_scrub(payload), ensure_ascii=False, indent=2).encode())
        # Crop to the visible dialog, excluding the user's timeline and draft.
        box = dialog.bounding_box(timeout=1000)
        viewport = page.evaluate("({width:innerWidth,height:innerHeight})")
        if box:
            x, y = max(0, box["x"]), max(0, box["y"])
            width = min(box["x"] + box["width"], viewport["width"], x + 1600) - x
            height = min(box["y"] + box["height"], viewport["height"], y + 1000) - y
            if width > 0 and height > 0:
                png = page.screenshot(type="png", clip={"x": x, "y": y, "width": width, "height": height},
                    mask=[dialog if sensitive_prose else dialog.locator(PRIVATE_FIELDS)], timeout=2000)
                _write_private(directory / "dialog.png", png)
        return directory
    except Exception as exc:
        record_exception("social-content", "x_publish.dialog_diagnostic_failed", exc)
        return directory


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
