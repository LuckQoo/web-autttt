import sys
import json
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QMessageBox, QLabel, QHeaderView, QTextEdit,
    QFileDialog
)

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import queue


# ---------- Data model ----------
@dataclass
class FieldDesc:
    selector: str
    label: str
    tag: str
    input_type: str
    required: bool
    options: Optional[List[str]] = None  # for select


# ---------- Worker thread (Playwright session) ----------
class PlaywrightWorker(QThread):
    fields_ready = Signal(list)         # list[dict]
    log = Signal(str)
    error = Signal(str)
    done = Signal()

    def __init__(self):
        super().__init__()
        self._tasks = queue.Queue()
        self._stop = False
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def stop(self):
        self._stop = True
        self._tasks.put(("stop", None))

    def request_fetch(self, url: str):
        self._tasks.put(("fetch", {"url": url.strip()}))

    def request_fill(self, fills: List[Dict[str, Any]], url: str = ""):
        self._tasks.put(("fill", {"fills": fills, "url": url.strip()}))

    def _ensure_page(self, url: str):
        if self._page is None:
            self._browser = self._playwright.chromium.launch(headless=False)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        if url:
            self.log.emit(f"Opening: {url}")
            self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeoutError:
                pass

    def _extract_fields(self):
        js = r"""
        () => {
          const isVisible = (el) => {
            const st = window.getComputedStyle(el);
            if (!st) return false;
            if (st.visibility === 'hidden' || st.display === 'none') return false;
            const r = el.getBoundingClientRect();
            return (r.width > 0 && r.height > 0);
          };

          const cssEscape = (s) => {
            // minimal CSS escape
            return String(s).replace(/(["\\#.:\\[\\]\\s>+~])/g, '\\$1');
          };

          const uniqueSelector = (el) => {
            // Prefer stable attributes: name > id > aria-label > placeholder > nth-of-type path
            const tag = el.tagName.toLowerCase();
            const name = el.getAttribute('name');
            if (name) return `${tag}[name="${name.replace(/"/g, '\\"')}"]`;

            const id = el.id;
            if (id) return `${tag}#${cssEscape(id)}`;

            const aria = el.getAttribute('aria-label');
            if (aria) return `${tag}[aria-label="${aria.replace(/"/g, '\\"')}"]`;

            const ph = el.getAttribute('placeholder');
            if (ph) return `${tag}[placeholder="${ph.replace(/"/g, '\\"')}"]`;

            // fallback: build short path
            let path = tag;
            let cur = el;
            for (let i = 0; i < 4 && cur && cur.parentElement; i++) {
              const parent = cur.parentElement;
              const siblings = Array.from(parent.children).filter(x => x.tagName === cur.tagName);
              const idx = siblings.indexOf(cur) + 1;
              path = `${cur.tagName.toLowerCase()}:nth-of-type(${idx})` + (parent ? ' > ' + path : '');
              cur = parent;
              if (cur.tagName.toLowerCase() === 'body') break;
            }
            return 'body > ' + path;
          };

          const getLabelText = (el) => {
            // 1) <label for=id>
            if (el.id) {
              const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
              if (lbl && lbl.textContent) return lbl.textContent.trim();
            }
            // 2) wrapped by label
            const wrap = el.closest('label');
            if (wrap && wrap.textContent) return wrap.textContent.trim();

            // 3) aria-label / placeholder / name
            const aria = el.getAttribute('aria-label');
            if (aria) return aria.trim();
            const ph = el.getAttribute('placeholder');
            if (ph) return ph.trim();
            const name = el.getAttribute('name');
            if (name) return name.trim();

            // 4) nearby text
            const p = el.parentElement;
            if (p && p.textContent) {
              const t = p.textContent.trim();
              if (t && t.length <= 60) return t;
            }
            return "(unlabeled)";
          };

          const fields = [];
          const candidates = Array.from(document.querySelectorAll('input, textarea, select'))
            .filter(el => isVisible(el) && !el.disabled);

          for (const el of candidates) {
            const tag = el.tagName.toLowerCase();

            // skip hidden inputs
            if (tag === 'input') {
              const t = (el.getAttribute('type') || 'text').toLowerCase();
              if (t === 'hidden' || t === 'submit' || t === 'button' || t === 'image' || t === 'reset') continue;
            }

            const selector = uniqueSelector(el);
            const label = getLabelText(el);
            const required = el.required === true || el.getAttribute('aria-required') === 'true';

            let inputType = tag;
            if (tag === 'input') inputType = (el.getAttribute('type') || 'text').toLowerCase();

            let options = null;
            if (tag === 'select') {
              options = Array.from(el.options || []).map(o => (o.textContent || '').trim()).filter(Boolean);
            }

                    fields.push({
                      selector,
                      label,
                      tag,
                      input_type: inputType,
                      required,
                      options,
                      name: el.getAttribute('name') || '',
                      id: el.id || '',
                      aria: el.getAttribute('aria-label') || '',
                      placeholder: el.getAttribute('placeholder') || ''
                    });
                  }
                  return fields;
                }
        """
        return self._page.evaluate(js)

    def _fill_fields(self, fills: List[Dict[str, Any]]):
        for item in fills:
            selector = item["selector"]
            value = item.get("value", "")
            tag = item.get("tag", "")
            input_type = item.get("input_type", "")

            if value is None or value == "":
                continue

            self.log.emit(f"Filling {selector} = {value}")
            self._page.wait_for_selector(selector, state="visible", timeout=15000)

            if tag == "select":
                try:
                    self._page.select_option(selector, label=value)
                except Exception:
                    try:
                        self._page.select_option(selector, value=value)
                    except Exception:
                        self._page.click(selector)
                        self._page.keyboard.type(value)
            elif input_type in ("checkbox",):
                truthy = str(value).strip().lower() in ("1", "true", "yes", "y", "on")
                is_checked = self._page.is_checked(selector)
                if truthy != is_checked:
                    self._page.check(selector) if truthy else self._page.uncheck(selector)
            elif input_type in ("radio",):
                self._page.check(selector)
            else:
                self._page.fill(selector, str(value))

    def run(self):
        try:
            with sync_playwright() as p:
                self._playwright = p
                while not self._stop:
                    try:
                        task, payload = self._tasks.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    if task == "stop":
                        break

                    if task == "fetch":
                        url = payload.get("url", "")
                        try:
                            if not url and self._page is None:
                                self.error.emit("No active page to refresh. Please open URL first.")
                                continue
                            self._ensure_page(url)
                            fields = self._extract_fields()
                            self.log.emit(f"Detected fields: {len(fields)}")
                            self.fields_ready.emit(fields)
                        except Exception as e:
                            self.error.emit(str(e))

                    if task == "fill":
                        fills = payload.get("fills", [])
                        url = payload.get("url", "")
                        try:
                            self._ensure_page(url)
                            self._fill_fields(fills)
                            self.log.emit("Fill completed. (Not submitting)")
                            self.done.emit()
                        except Exception as e:
                            self.error.emit(str(e))
        finally:
            try:
                if self._context:
                    self._context.close()
                if self._browser:
                    self._browser.close()
            except Exception:
                pass


# ---------- GUI ----------
class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto Form Fetch & Fill (MVP)")
        self.resize(1000, 650)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Enter URL (e.g., https://example.com/form)")

        self.btn_fetch = QPushButton("Fetch Fields")
        self.btn_refresh = QPushButton("Refresh Fields")
        self.btn_import = QPushButton("Import TXT")
        self.btn_fill = QPushButton("Fill Current Page")
        self.btn_import.setEnabled(False)
        self.btn_fill.setEnabled(False)
        self.btn_refresh.setEnabled(False)

        top = QHBoxLayout()
        top.addWidget(QLabel("URL:"))
        top.addWidget(self.url_edit, 1)
        top.addWidget(self.btn_fetch)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_fill)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Label", "Tag/Type", "Required", "Selector", "Options", "Value to Fill"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setWordWrap(True)
        self.table.itemChanged.connect(self._on_table_item_changed)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(QLabel("Detected Fields"))
        layout.addWidget(self.table, 1)
        layout.addWidget(QLabel("Log"))
        layout.addWidget(self.log_view, 0)

        self.btn_fetch.clicked.connect(self.fetch_fields)
        self.btn_refresh.clicked.connect(self.refresh_fields)
        self.btn_import.clicked.connect(self.import_txt)
        self.btn_fill.clicked.connect(self.fill_current_page)

        self._worker: Optional[PlaywrightWorker] = None
        self._txt_data: Dict[str, Any] = {}

        self._worker = PlaywrightWorker()
        self._worker.log.connect(self.append_log)
        self._worker.error.connect(self._on_worker_error)
        self._worker.fields_ready.connect(self._on_fields_ready)
        self._worker.done.connect(self._on_fill_done)
        self._worker.start()
        self._has_page = False
        self._txt_source_path = ""
        self._txt_queue: List[str] = []
        self._txt_mode = "single"

    def append_log(self, msg: str):
        self.log_view.append(msg)

    def show_error(self, msg: str):
        QMessageBox.critical(self, "Error", msg)

    def fetch_fields(self):
        url = self.url_edit.text().strip()
        if not url:
            self.show_error("Please enter a URL.")
            return

        self.btn_fetch.setEnabled(False)
        self.btn_fill.setEnabled(False)
        self.table.setRowCount(0)
        self.append_log("---- Fetch start ----")

        self._worker.request_fetch(url)
        self.btn_fetch.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self._has_page = True

    def _on_worker_error(self, msg: str):
        self.append_log(f"[ERROR] {msg}")
        self.show_error(msg)
        self.btn_fetch.setEnabled(True)

    def _on_fields_ready(self, fields: List[Dict[str, Any]]):
        self.table.setRowCount(0)

        for f in fields:
            row = self.table.rowCount()
            self.table.insertRow(row)

            label = f.get("label", "")
            tag = f.get("tag", "")
            itype = f.get("input_type", "")
            required = f.get("required", False)
            selector = f.get("selector", "")
            options = f.get("options", None)

            label_item = QTableWidgetItem(label)
            meta = {
                "label": label,
                "name": f.get("name", ""),
                "id": f.get("id", ""),
                "aria": f.get("aria", ""),
                "placeholder": f.get("placeholder", "")
            }
            label_item.setData(Qt.ItemDataRole.UserRole, meta)
            self.table.setItem(row, 0, label_item)
            self.table.setItem(row, 1, QTableWidgetItem(f"{tag}/{itype}"))
            self.table.setItem(row, 2, QTableWidgetItem("Yes" if required else "No"))

            sel_item = QTableWidgetItem(selector)
            sel_item.setFlags(sel_item.flags() & ~Qt.ItemFlag.ItemIsEditable)  # keep selector readonly
            self.table.setItem(row, 3, sel_item)

            opt_text = ""
            if isinstance(options, list) and options:
                opt_text = ", ".join(options[:30]) + (" ..." if len(options) > 30 else "")
            opt_item = QTableWidgetItem(opt_text)
            opt_item.setFlags(opt_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 4, opt_item)

            self.table.setItem(row, 5, QTableWidgetItem(""))

        self.append_log("---- Fetch done ----")
        self.btn_import.setEnabled(self.table.rowCount() > 0)
        self.btn_fill.setEnabled(self.table.rowCount() > 0)

    def refresh_fields(self):
        # Re-scan current page (after login / navigation)
        if not self._worker:
            self.show_error("Browser is not ready.")
            return
        self.append_log("---- Refresh start ----")
        self._worker.request_fetch("")
        self._has_page = True

    def import_txt(self):
        if self.table.rowCount() == 0:
            self.show_error("Please fetch fields first.")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select TXT", "", "Text Files (*.txt);;All Files (*)"
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            self.show_error(f"Failed to read TXT: {e}")
            return

        self._init_txt_queue(raw, file_path)
        total_lines = len(self._txt_queue) if self._txt_mode == "queue" else len(self._txt_data.get("lines", []))
        self.append_log(f"Imported TXT lines: {total_lines}")

        # Auto map and fill values in table
        mapped = self._auto_map_to_fields(clear_values=True)
        self.append_log(f"Auto-mapped fields: {mapped}")
        self.btn_fill.setEnabled(self.table.rowCount() > 0)

    def _init_txt_queue(self, raw: str, file_path: str):
        # Decide whether to treat TXT as a queue of single-line records
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        self._txt_source_path = file_path

        has_pipe = any("|" in ln for ln in lines)
        has_kv = all(("=" in ln) or (":" in ln) for ln in lines) if lines else False

        if len(lines) > 1 and (has_pipe or not has_kv):
            # Record-per-line queue mode
            self._txt_mode = "queue"
            self._txt_queue = lines
            self._set_current_txt_line(lines[0])
        else:
            # Single record (supports multi-line key/value)
            self._txt_mode = "single"
            self._txt_queue = []
            self._txt_data = self._parse_txt(raw)

    def _set_current_txt_line(self, line: str):
        # Parse a single line as the current record
        self._txt_data = self._parse_txt(line)

    def _parse_txt(self, raw: str) -> Dict[str, Any]:
        # Parse lines, keep key-value pairs if present, else keep as free lines
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        kv = {}
        for ln in lines:
            if "=" in ln:
                k, v = ln.split("=", 1)
            elif ":" in ln:
                k, v = ln.split(":", 1)
            else:
                continue
            k = k.strip()
            v = v.strip()
            if k and v:
                kv[k] = v

        # If the content is a single pipe-separated line, treat as values list
        pipe_values = []
        if len(lines) == 1 and "|" in lines[0]:
            pipe_values = [v.strip() for v in lines[0].split("|") if v.strip()]

        return {
            "lines": lines,
            "kv": kv,
            "pipe_values": pipe_values
        }

    def _normalize(self, s: str) -> str:
        return "".join(ch.lower() for ch in s if ch.isalnum())

    def _auto_map_to_fields(self, clear_values: bool = False) -> int:
        # Best-effort mapping by label keywords + value patterns
        lines = self._txt_data.get("lines", [])
        kv = self._txt_data.get("kv", {})
        pipe_values = self._txt_data.get("pipe_values", [])

        # Build candidate values by pattern
        def is_email(v): return "@" in v and "." in v
        def is_phone(v):
            digits = "".join(ch for ch in v if ch.isdigit())
            return len(digits) >= 8
        def is_date(v):
            return any(x in v for x in ("/", "-", "年"))
        def is_card_number(v):
            digits = "".join(ch for ch in v if ch.isdigit())
            return 13 <= len(digits) <= 19
        def is_cvv(v):
            digits = "".join(ch for ch in v if ch.isdigit())
            return len(digits) in (3, 4)
        def is_expiry(v):
            # accept MM/YY, MM/YYYY, YYYY-MM
            return any(x in v for x in ("/", "-")) and any(ch.isdigit() for ch in v)
        def is_country_code(v):
            return len(v) == 2 and v.isalpha()
        def is_postal(v):
            digits = "".join(ch for ch in v if ch.isdigit())
            return len(digits) >= 4

        if pipe_values:
            values = pipe_values
        else:
            values = list(kv.values()) if kv else lines

        # keywords map (zh + en)
        kw_map = {
            "name": ["姓名", "名字", "聯絡人", "收件人", "name", "full name", "contact"],
            "phone": ["手機", "電話", "聯絡電話", "phone", "mobile", "tel"],
            "email": ["信箱", "email", "e-mail", "電子郵件"],
            "address": ["地址", "住址", "address"],
            "company": ["公司", "單位", "company", "organization", "org"],
            "id": ["身分證", "證號", "id", "identity", "passport"],
            "date": ["生日", "日期", "date", "birthday", "birth"],
            "card_number": ["卡號", "信用卡號", "credit card", "card number", "cc number", "pan"],
            "card_expiry": ["有效期限", "到期日", "expiry", "exp", "expiration"],
            "card_cvv": ["安全碼", "驗證碼", "cvv", "cvc", "security code"],
            "card_holder": ["持卡人", "cardholder", "name on card"],
            "bank": ["發卡銀行", "銀行", "issuer", "bank"],
            "billing_address": ["帳單地址", "billing address", "billing"],
            "city": ["城市", "city", "town"],
            "state": ["州", "省", "state", "province", "region"],
            "postal": ["郵遞區號", "zip", "postal", "postcode"],
            "country": ["國家", "國別", "country", "nation"]
        }

        used = set()
        mapped = 0

        # Special handling for pipe format: assume common order if not enough labels
        pipe_hint = {}
        if pipe_values and len(pipe_values) >= 8:
            # common order: card | mm | yy | cvv | name | addr1 | city | state | postal | country | phone | email
            p = pipe_values
            pipe_hint = {
                "card_number": p[0] if len(p) > 0 else "",
                "exp_month": p[1] if len(p) > 1 else "",
                "exp_year": p[2] if len(p) > 2 else "",
                "card_cvv": p[3] if len(p) > 3 else "",
                "card_holder": p[4] if len(p) > 4 else "",
                "address": p[5] if len(p) > 5 else "",
                "city": p[6] if len(p) > 6 else "",
                "state": p[7] if len(p) > 7 else "",
                "postal": p[8] if len(p) > 8 else "",
                "country": p[9] if len(p) > 9 else "",
                "phone": p[10] if len(p) > 10 else "",
                "email": p[11] if len(p) > 11 else ""
            }

        for r in range(self.table.rowCount()):
            if clear_values:
                self.table.setItem(r, 5, QTableWidgetItem(""))
            label_item = self.table.item(r, 0)
            label = label_item.text()
            tag_type = self.table.item(r, 1).text()
            meta = label_item.data(Qt.ItemDataRole.UserRole) or {}
            combined = " ".join([
                label,
                meta.get("name", ""),
                meta.get("id", ""),
                meta.get("aria", ""),
                meta.get("placeholder", "")
            ])
            label_norm = self._normalize(combined)

            best_value = ""

            # Prefer exact key match if kv exists
            if kv:
                for k, v in kv.items():
                    if self._normalize(k) in label_norm or label_norm in self._normalize(k):
                        best_value = v
                        break

            # Pipe hints by field name patterns
            if not best_value and pipe_hint:
                if any(x in label_norm for x in ["cardnumber", "cardnumber", "ccnumber", "cardnumber", "pan"]):
                    best_value = pipe_hint.get("card_number", "")
                elif any(x in label_norm for x in ["cardexpiry", "expiry", "exp", "expiration"]):
                    m = pipe_hint.get("exp_month", "")
                    y = pipe_hint.get("exp_year", "")
                    if m and y:
                        best_value = f"{m}/{y}"
                elif any(x in label_norm for x in ["cardcvc", "cardcvv", "cvc", "cvv", "securitycode"]):
                    best_value = pipe_hint.get("card_cvv", "")
                elif any(x in label_norm for x in ["billingname", "cardholder", "nameoncard"]):
                    best_value = pipe_hint.get("card_holder", "")
                elif "billingaddressline1" in label_norm or "addressline1" in label_norm:
                    best_value = pipe_hint.get("address", "")
                elif "billinglocality" in label_norm or "city" in label_norm:
                    best_value = pipe_hint.get("city", "")
                elif "billingdependentlocality" in label_norm or "state" in label_norm or "province" in label_norm:
                    best_value = pipe_hint.get("state", "")
                elif "billingpostalcode" in label_norm or "postal" in label_norm or "zip" in label_norm:
                    best_value = pipe_hint.get("postal", "")
                elif "billingcountry" in label_norm or "country" in label_norm:
                    best_value = pipe_hint.get("country", "")
                elif "phone" in label_norm or "mobile" in label_norm:
                    best_value = pipe_hint.get("phone", "")
                elif "email" in label_norm:
                    best_value = pipe_hint.get("email", "")

            # If no kv match, heuristic by keywords + value pattern
            if not best_value:
                # pick by keyword
                for key, kws in kw_map.items():
                    if any(self._normalize(k) in label_norm for k in kws):
                        for v in values:
                            if v in used:
                                continue
                            if key == "email" and is_email(v):
                                best_value = v
                                break
                            if key == "phone" and is_phone(v):
                                best_value = v
                                break
                            if key == "date" and is_date(v):
                                best_value = v
                                break
                            if key == "card_number" and is_card_number(v):
                                best_value = v
                                break
                            if key == "card_expiry" and is_expiry(v):
                                best_value = v
                                break
                            if key == "card_cvv" and is_cvv(v):
                                best_value = v
                                break
                            if key == "country" and is_country_code(v):
                                best_value = v
                                break
                            if key == "postal" and is_postal(v):
                                best_value = v
                                break
                            if key in ("name", "company", "address", "id"):
                                best_value = v
                                break
                            if key in ("card_holder", "bank", "billing_address"):
                                best_value = v
                                break
                            if key in ("city", "state"):
                                best_value = v
                                break
                    if best_value:
                        break

            # Fallback: fill next unused line
            if not best_value:
                for v in values:
                    if v not in used:
                        best_value = v
                        break

            if best_value:
                self.table.setItem(r, 5, QTableWidgetItem(best_value))
                used.add(best_value)
                mapped += 1

        return mapped

    def _on_table_item_changed(self, item: QTableWidgetItem):
        # Enable fill if there is at least one non-empty value in column 5
        if item.column() != 5:
            return
        for r in range(self.table.rowCount()):
            v_item = self.table.item(r, 5)
            if v_item and v_item.text().strip():
                self.btn_fill.setEnabled(True)
                return

    def fill_current_page(self):
        url = self.url_edit.text().strip()
        if not url:
            self.show_error("Please enter a URL.")
            return

        fills: List[Dict[str, Any]] = []
        for r in range(self.table.rowCount()):
            selector = self.table.item(r, 3).text()
            label = self.table.item(r, 0).text()
            tag_type = self.table.item(r, 1).text()
            value_item = self.table.item(r, 5)
            value = value_item.text() if value_item else ""

            # split tag/type
            tag, itype = ("", "")
            if "/" in tag_type:
                tag, itype = tag_type.split("/", 1)

            fills.append({
                "selector": selector,
                "label": label,
                "tag": tag,
                "input_type": itype,
                "value": value
            })

        self.append_log("---- Fill start ----")
        self.btn_fill.setEnabled(False)

        # If we already have an open page, do not navigate again
        self._worker.request_fill(fills, url="" if self._has_page else url)

    def _on_fill_done(self):
        self.append_log("---- Fill done ----")
        self.btn_fill.setEnabled(True)
        if self._txt_mode == "queue" and self._txt_queue:
            # Consume first line and advance to next
            self._txt_queue.pop(0)
            if self._txt_queue:
                self._set_current_txt_line(self._txt_queue[0])
                mapped = self._auto_map_to_fields(clear_values=True)
                self.append_log(f"Advanced to next TXT line. Auto-mapped fields: {mapped}")
            else:
                self.append_log("TXT queue empty.")
                self._txt_data = {"lines": [], "kv": {}, "pipe_values": []}

            # Persist remaining lines back to the TXT file
            if self._txt_source_path:
                try:
                    with open(self._txt_source_path, "w", encoding="utf-8") as f:
                        if self._txt_queue:
                            f.write("\n".join(self._txt_queue) + "\n")
                        else:
                            f.write("")
                except Exception as e:
                    self.append_log(f"[ERROR] Failed to update TXT: {e}")

    def closeEvent(self, event):
        if self._worker:
            self._worker.stop()
            self._worker.wait(2000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    w = App()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
