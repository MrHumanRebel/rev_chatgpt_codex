#!/usr/bin/env python3
import sys
import re
import uuid
import time
from pathlib import Path
from flask import Flask, request, jsonify
import atexit

# Playwright importok
try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:
    print("A Playwright nincs telepítve. (pip install playwright && playwright install)")
    sys.exit(1)


# ==========================================
# GLOBÁLIS PLAYWRIGHT ÁLLAPOT
# ==========================================
PLAYWRIGHT_INSTANCE = None
BROWSER_CONTEXT = None
CHAT_PAGE = None

GEMINI_URL = "https://gemini.google.com/app"

# Gemini DOM szelektorok
GEMINI_EDITOR_SELECTOR = "div.ql-editor.textarea.new-input-ui[contenteditable='true']"
GEMINI_SEND_BUTTON_SELECTOR = 'button[aria-label="Üzenet küldése"]'
GEMINI_RESPONSE_MARKDOWN_SELECTOR = "div.markdown.markdown-main-panel"
GEMINI_COMPLETION_FOOTER_SELECTOR = "div.response-footer.gap.complete"


# ==========================================
# SEGÉDFÜGGVÉNYEK – COOKIES/LOCALSTORAGE PARSOLÁS
# ==========================================

def load_raw_data():
    raw_cookies = ""
    raw_ls = ""

    try:
        with open("cookies.txt", "r", encoding="utf-8") as f:
            raw_cookies = f.read()
    except FileNotFoundError:
        print("HIBA: Nem találom a 'cookies.txt' fájlt!")

    try:
        with open("localstorage.txt", "r", encoding="utf-8") as f:
            raw_ls = f.read()
    except FileNotFoundError:
        print("FIGYELEM: Nem találom a 'localstorage.txt' fájlt (localStorage injektálás kihagyva).")

    return raw_cookies, raw_ls


def build_google_cookies(raw_cookies: str):
    """
    A nyers cookie dumpból (Chrome export, TAB/whitespace táblázat) Playwright cookie-kat épít.
    Várt formátum soronként:
        NAME    VALUE   DOMAIN  PATH    EXPIRES ...
    """
    cookies = []

    if not raw_cookies:
        print("FIGYELEM: raw_cookies üres, nincs mit injektálni.")
        return cookies

    for line in raw_cookies.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = re.split(r"\s+", line)
        if len(parts) < 3:
            continue

        name = parts[0]
        value = parts[1]
        domain = parts[2]
        path = parts[3] if len(parts) > 3 else "/"

        if "google.com" not in domain:
            continue

        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": True,
            "sameSite": "Lax",
        }
        cookies.append(cookie)

    print(f"{len(cookies)} db Google cookie kerül injektálásra (build_google_cookies).")
    for c in cookies[:5]:
        print(f"  - {c['name']} @ {c['domain']}")
    return cookies


def apply_localstorage_from_text(page, raw_ls: str):
    """
    localstorage.txt formátum:
        KULCS=ÉRTÉK
      vagy
        KULCS<TAB/SPACE>ÉRTÉK
    """
    if not raw_ls:
        return

    count = 0

    for line in raw_ls.splitlines():
        line = line.rstrip("\n\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        key = value = None

        if "=" in line:
            key, value = line.split("=", 1)
        else:
            parts = re.split(r"\s+", line, maxsplit=1)
            if len(parts) == 2:
                key, value = parts
            else:
                continue

        key = key.strip()
        value = value.strip()
        if not key:
            continue

        page.evaluate(
            """([k, v]) => { localStorage.setItem(k, v); }""",
            [key, value],
        )
        count += 1

    print(f"localStorage injektálás befejezve a localstorage.txt alapján ({count} kulcs).")


# ==========================================
# CANVAS MÓD BEKAPCSOLÁSA
# ==========================================

def ensure_canvas_enabled(page):
    """
    Bekapcsolja a Canvas módot, ha még nincs.
    - Ha van 'span.toolbox-drawer-item-deselect-button-label' 'Canvas' szöveggel -> már aktív.
    - Különben rákattint a 'Eszközök'/'Tools' gombra, majd a 'Canvas' elemre.
    """
    try:
        result = page.evaluate(
            """
            () => {
              // Ha már látszik a "Canvas" kikapcsoló gomb (deselect), akkor aktív
              const hasDeselectCanvas = Array.from(
                document.querySelectorAll('span.toolbox-drawer-item-deselect-button-label')
              ).some(el => (el.textContent || '').trim() === 'Canvas');
              if (hasDeselectCanvas) return 'already-on';

              const findButtonByText = (texts) => {
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const b of buttons) {
                  const t = (b.textContent || '').trim();
                  if (!t) continue;
                  for (const wanted of texts) {
                    if (t.includes(wanted)) return b;
                  }
                }
                return null;
              };

              // Eszközök / Tools gomb megnyitása
              const toolsBtn = findButtonByText(['Eszközök', 'Tools']);
              if (toolsBtn) toolsBtn.click();

              // Keressük a "Canvas" feliratot
              const all = Array.from(document.querySelectorAll('button, span, div'));
              const canvasLabel = all.find(el => (el.textContent || '').trim() === 'Canvas');
              if (!canvasLabel) return 'canvas-not-found';

              const clickable = canvasLabel.closest('button') || canvasLabel;
              if (clickable instanceof HTMLElement) {
                  clickable.click();
                  return 'toggled-on';
              }

              return 'no-clickable';
            }
            """
        )
        print(f"Canvas mód állapota: {result}")
    except Exception as e:
        print(f"Canvas mód beállítási hiba: {e}")


# ==========================================
# PLAYWRIGHT LOGIKA (VISSZATÉRÍTI A VÁLASZT)
# ==========================================

def run_with_playwright(prompt: str) -> str:
    """
    Kiküldi a promptot a Google Gemini-nek Playwright segítségével,
    és egy meglévő, globális munkamenetet használ.
    """
    global PLAYWRIGHT_INSTANCE, BROWSER_CONTEXT, CHAT_PAGE

    response_text = "HIBA: A kérés nem futott le."

    # -------- 1. Inicializálás csak első kérésnél --------
    if CHAT_PAGE is None:
        print("Böngésző inicializálása (első kérés, Gemini)...")

        PLAYWRIGHT_INSTANCE = sync_playwright().start()
        raw_cookies_text, raw_ls_text = load_raw_data()
        cookies_to_add = build_google_cookies(raw_cookies_text)

        profile_path = Path.cwd() / "gemini_profile"
        try:
            BROWSER_CONTEXT = PLAYWRIGHT_INSTANCE.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            return f"HIBA: Böngésző indítási hiba (Gemini): {e}"

        if cookies_to_add:
            try:
                BROWSER_CONTEXT.add_cookies(cookies_to_add)
            except Exception as e:
                print(f"HIBA cookie hozzáadáskor: {e}")
        else:
            print("FIGYELEM: Nem sikerült Google cookie-kat kinyerni a cookies.txt-ből!")

        CHAT_PAGE = BROWSER_CONTEXT.new_page()

        print(f"Navigálás a Gemini-re: {GEMINI_URL} ...")
        CHAT_PAGE.goto(GEMINI_URL)
        print("Aktuális URL a navigation után:", CHAT_PAGE.url)

        if raw_ls_text:
            apply_localstorage_from_text(CHAT_PAGE, raw_ls_text)
            CHAT_PAGE.reload()
            print("Oldal újratöltve a localStorage injektálás után.")
            print("Aktuális URL reload után:", CHAT_PAGE.url)

        try:
            print("Várakozás a Gemini chat inputra (max 600s)...")
            CHAT_PAGE.wait_for_selector(GEMINI_EDITOR_SELECTOR, timeout=600_000)
        except Exception as e:
            print(
                f"KRITIKUS HIBA az inicializáláskor: {e}. "
                "Valószínűleg nem valid a cookie/localStorage dump, vagy login képernyőre dob."
            )
            try:
                if BROWSER_CONTEXT:
                    BROWSER_CONTEXT.close()
                if PLAYWRIGHT_INSTANCE:
                    PLAYWRIGHT_INSTANCE.stop()
            except Exception:
                pass

            CHAT_PAGE = None
            BROWSER_CONTEXT = None
            PLAYWRIGHT_INSTANCE = None

            return (
                "HIBA: A böngésző inicializálása sikertelen a Gemini-hez. "
                "Frissítsd a 'cookies.txt' és 'localstorage.txt' tartalmát."
            )

        # Canvas bekapcsolása az első betöltés után
        print("Canvas mód ellenőrzése/bekapcsolása (init)...")
        ensure_canvas_enabled(CHAT_PAGE)

    if CHAT_PAGE is None:
        return (
            "HIBA: A böngésző munkamenet az előző kérés során leállt. "
            "Indítsd újra a szervert."
        )

    page = CHAT_PAGE

    # -------- 2. Baseline válasz-blokkok száma --------
    try:
        initial_blocks = page.query_selector_all(GEMINI_RESPONSE_MARKDOWN_SELECTOR)
        initial_block_count = len(initial_blocks)
    except Exception:
        initial_block_count = 0

    try:
        initial_footers = page.query_selector_all(GEMINI_COMPLETION_FOOTER_SELECTOR)
        initial_footer_count = len(initial_footers)
    except Exception:
        initial_footer_count = 0

    print(f"Baseline: {initial_block_count} markdown blokk, {initial_footer_count} footer.")

    # Canvas-t kérésenként is biztosítjuk, ha esetleg kikapcsoltad UI-ból
    print("Canvas mód ellenőrzése/bekapcsolása (request)...")
    ensure_canvas_enabled(page)

    # -------- 3. Prompt elküldése a Gemini UI-nak --------
    try:
        print(f"Prompt küldése Gemini-nek: {prompt[:80]}...")

        try:
            editor = page.wait_for_selector(GEMINI_EDITOR_SELECTOR, timeout=30_000)
        except PlaywrightTimeoutError:
            return (
                "HIBA: Nem találom a Gemini szövegmezőt. "
                "Ellenőrizd a GEMINI_EDITOR_SELECTOR értékét a GEMINI_API.py-ben."
            )

        editor.click()
        editor.fill("")
        editor.fill(prompt)

        try:
            send_button = page.wait_for_selector(GEMINI_SEND_BUTTON_SELECTOR, timeout=10_000)

            page.wait_for_function(
                "(btn) => !btn.hasAttribute('aria-disabled') || "
                "btn.getAttribute('aria-disabled') === 'false'",
                arg=send_button,
                timeout=10_000,
            )

            send_button.click()
        except Exception as e:
            print(f"Send gomb hiba, fallback Enter: {e}")
            page.keyboard.press("Enter")

        # -------- 4. Várakozás az ÚJ válaszra (nem a régire!) --------
        print("Várakozás a Gemini válaszára (ÚJ markdown + ÚJ footer)...")

        try:
            page.wait_for_selector(GEMINI_RESPONSE_MARKDOWN_SELECTOR, timeout=60_000)
        except PlaywrightTimeoutError:
            print("HIBA: Nem jelent meg válasz-markdown blokk.")
            return "HIBA: Nem sikerült a Gemini válaszát kiolvasni (nincs markdown blokk)."

        try:
            page.wait_for_function(
                """
                (arg) => {
                    const {
                        markdownSelector,
                        footerSelector,
                        initialBlockCount,
                        initialFooterCount
                    } = arg;

                    const blocks = Array.from(document.querySelectorAll(markdownSelector));
                    const footers = footerSelector
                        ? Array.from(document.querySelectorAll(footerSelector))
                        : [];

                    // Csak akkor kész, ha ÚJ blokk és/vagy ÚJ footer is van
                    if (blocks.length <= initialBlockCount) {
                        return false;
                    }

                    const last = blocks[blocks.length - 1];
                    const busy = last.getAttribute('aria-busy');

                    if (busy === 'true') return false;

                    if (footerSelector) {
                        if (footers.length <= initialFooterCount) {
                            return false;
                        }
                    }

                    return true;
                }
                """,
                arg={
                    "markdownSelector": GEMINI_RESPONSE_MARKDOWN_SELECTOR,
                    "footerSelector": GEMINI_COMPLETION_FOOTER_SELECTOR,
                    "initialBlockCount": initial_block_count,
                    "initialFooterCount": initial_footer_count,
                },
                timeout=120_000,
            )
        except PlaywrightTimeoutError:
            print("FIGYELEM: Timeout a generálás befejezésének detektálásánál – a legutolsó szöveget olvassuk ki.")

        # -------- 5. Az ÚJ utolsó markdown blokk szövegének kiolvasása --------
        try:
            blocks_after = page.query_selector_all(GEMINI_RESPONSE_MARKDOWN_SELECTOR)
            if blocks_after:
                last_block = blocks_after[-1]
                text = last_block.inner_text() or ""
            else:
                text = ""

            if not text.strip():
                print("HIBA: Az utolsó markdown blokk üres szöveget adott.")
                response_text = "HIBA: A kinyert Gemini szöveg üres maradt."
            else:
                response_text = text
        except Exception as e:
            print(f"HIBA a válasz kiolvasásakor: {e}")
            response_text = f"HIBA: A Gemini válasz kiolvasása közben hiba történt: {e}"

    except Exception as e:
        print(f"HIBA a folyamat közben (Gemini): {e}. Munkamenet lezárva.")

        try:
            if BROWSER_CONTEXT:
                BROWSER_CONTEXT.close()
            if PLAYWRIGHT_INSTANCE:
                PLAYWRIGHT_INSTANCE.stop()
        except Exception:
            pass

        CHAT_PAGE = None
        BROWSER_CONTEXT = None
        PLAYWRIGHT_INSTANCE = None

        response_text = (
            "HIBA: A Playwright nem tudta elküldeni a kérést a Gemini-nek. "
            f"Hiba: {e}"
        )

    return response_text


# ==========================================
# FLASK API – OpenAI-kompatibilis wrapper
# ==========================================

app = Flask(__name__)


def _extract_text_from_content(content):
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = (
                    item.get("text")
                    or item.get("input_text")
                    or item.get("content")
                    or ""
                )
                if txt:
                    parts.append(str(txt))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    return str(content)


def _build_prompt_from_messages(messages):
    blocks = []

    for msg in messages:
        role = msg.get("role", "user")
        text = _extract_text_from_content(msg.get("content", ""))

        if not text:
            continue
        if role == "tool":
            continue

        if role == "system":
            blocks.append(text)
        elif role == "user":
            blocks.append(text)
        elif role == "assistant":
            blocks.append(f"Assistant: {text}")
        else:
            blocks.append(text)

    return "\n\n".join(blocks).strip()


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/chat/completions", methods=["POST"])
def chat_completions():
    data = request.json or {}
    try:
        print(f"REQUEST PATH: {request.path}")
    except Exception:
        pass

    messages = data.get("messages", [])
    prompt = _build_prompt_from_messages(messages)

    if not prompt:
        return jsonify({"error": "Nincs értelmezhető szöveg a 'messages' mezőben."}), 400

    print(f"\n--- ÚJ KÉRÉS (összefűzött, Gemini): {prompt[:60]}... ---")
    generated_content = run_with_playwright(prompt)
    print(f"--- KÉSZ, válasz hossza: {len(generated_content)} karakter ---")

    if generated_content.startswith("HIBA:"):
        error_message = generated_content.replace("HIBA: ", "")
        return (
            jsonify(
                {
                    "error": {
                        "message": error_message,
                        "type": "browser_error",
                        "code": "500",
                    }
                }
            ),
            500,
        )

    response_data = {
        "id": "chatcmpl-" + str(uuid.uuid4()).replace("-", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "gemini-playwright",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": generated_content,
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": len(generated_content.split()),
            "total_tokens": len(prompt.split()) + len(generated_content.split()),
        },
    }
    print(response_data)
    return jsonify(response_data)


@app.route("/v1/models", methods=["GET"])
@app.route("/models", methods=["GET"])
def list_models():
    return jsonify(
        {
            "object": "list",
            "data": [
                {
                    "id": "gemini-playwright",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "user-host",
                }
            ],
        }
    )


# ==========================================
# LEZÁRÁSI LOGIKA
# ==========================================

def shutdown_playwright():
    global BROWSER_CONTEXT, PLAYWRIGHT_INSTANCE
    if BROWSER_CONTEXT:
        print("\n🤖 Lezárás: Playwright böngésző bezárása (Gemini munkamenet vége)...")
        try:
            BROWSER_CONTEXT.close()
        except Exception as e:
            print(f"Lezárási hiba: {e}")

    if PLAYWRIGHT_INSTANCE:
        try:
            PLAYWRIGHT_INSTANCE.stop()
        except Exception as e:
            print(f"Playwright stop hiba: {e}")


if __name__ == "__main__":
    atexit.register(shutdown_playwright)

    print("🤖 Playwright-alapú Gemini API szerver indítása a http://127.0.0.1:5000 címen...")
    print("Használd a cookies.txt + localstorage.txt injektálást a meglévő Google/Gemini sessionödhöz.")
    app.run(debug=False, port=5000, threaded=False)
