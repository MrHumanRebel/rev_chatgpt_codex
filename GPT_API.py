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
DEVICE_ID = str(uuid.uuid4())  # Alapértelmezett Device ID, ha nem találunk a localstorage.txt-ben


# ==========================================
# SEGÉDFÜGGVÉNYEK (A "MOCSKOS" PARSOLÁSHOZ)
# ==========================================

def parse_value_from_dump(text, key_name):
    """
    Keres egy kulcsot és a hozzá tartozó értéket egy formázatlan dump szövegben.
    """
    if not text:
        return None

    # 1. oai-did (UUID) a LocalStorage-ból
    if "oai-did" in key_name:
        match = re.search(
            r"oai-did.*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)

    # 2. session-token (JWT szerű) a Cookie-kból
    if "session-token" in key_name:
        match = re.search(
            r"session-token\s*(eyJ[a-zA-Z0-9\-\._~]+\.[a-zA-Z0-9\-\._~]+\.[a-zA-Z0-9\-\._~]+\.[a-zA-Z0-9\-\._~]+)",
            text,
        )
        if not match:
            match = re.search(
                r"session-token\s*(eyJ[a-zA-Z0-9\-\._~]+\.[a-zA-Z0-9\-\._~]+\.[a-zA-Z0-9\-\._~]+)",
                text,
            )
        if not match:
            match = re.search(
                r"session-token\s*(eyJ[a-zA-Z0-9\-\._~]+\.\.[a-zA-Z0-9\-\._~]+)",
                text,
            )

        if match:
            return match.group(1)

    # 3. cf_clearance (Cloudflare)
    if "cf_clearance" in key_name:
        match = re.search(r"cf_clearance\s*([a-zA-Z0-9\.\-_]+)", text)
        if match:
            return match.group(1)

    # 4. _puid (User ID)
    if "_puid" in key_name:
        match = re.search(r"_puid\s*(user-[a-zA-Z0-9\-\._~:%=]+)", text)
        if match:
            return match.group(1)

    return None


def load_raw_data():
    """Beolvassa a localstorage.txt és cookies.txt fájlokat."""
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
        print("HIBA: Nem találom a 'localstorage.txt' fájlt!")

    return raw_cookies, raw_ls


# ==========================================
# PLAYWRIGHT LOGIKA (VISSZATÉRÍTI A VÁLASZT)
# ==========================================
def run_with_playwright(prompt: str) -> str:
    """
    Kiküldi a promptot a ChatGPT-nek Playwright segítségével,
    és egy meglévő, globális munkamenetet használ.
    """
    global PLAYWRIGHT_INSTANCE, BROWSER_CONTEXT, CHAT_PAGE, DEVICE_ID

    response_text = "HIBA: A kérés nem futott le."  # Alapértelmezett hibaüzenet

    # ------------------------------------------
    # 1. Munkamenet inicializálása (csak az első híváskor)
    # ------------------------------------------
    if CHAT_PAGE is None:
        print("Böngésző inicializálása (első kérés)...")

        PLAYWRIGHT_INSTANCE = sync_playwright().start()

        raw_cookies_text, raw_ls_text = load_raw_data()
        session_token = parse_value_from_dump(raw_cookies_text, "session-token")
        cf_clearance = parse_value_from_dump(raw_cookies_text, "cf_clearance")
        puid = parse_value_from_dump(raw_cookies_text, "_puid")

        device_id_ls = parse_value_from_dump(raw_ls_text, "oai-did")
        if device_id_ls:
            DEVICE_ID = device_id_ls

        print(f"Session Token: {'IGEN' if session_token else 'NEM'}")
        print(f"Cloudflare Clearance: {'IGEN' if cf_clearance else 'NEM'}")
        print(f"Device ID: {DEVICE_ID}")

        profile_path = Path.cwd() / "chrome_profile"
        try:
            BROWSER_CONTEXT = PLAYWRIGHT_INSTANCE.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            return f"HIBA: Böngésző indítási hiba: {e}"

        cookies_to_add = []
        if session_token:
            cookies_to_add.append(
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        if cf_clearance:
            cookies_to_add.append(
                {
                    "name": "cf_clearance",
                    "value": cf_clearance,
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "None",
                }
            )
        if puid:
            cookies_to_add.append(
                {
                    "name": "_puid",
                    "value": puid,
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "sameSite": "Lax",
                }
            )

        if cookies_to_add:
            try:
                BROWSER_CONTEXT.add_cookies(cookies_to_add)
                print(f"{len(cookies_to_add)} db kritikus cookie hozzáadva.")
            except Exception as e:
                print(f"HIBA cookie hozzáadáskor: {e}")
        else:
            print("FIGYELEM: Nem sikerült cookie-kat kinyerni a dumpból!")

        CHAT_PAGE = BROWSER_CONTEXT.new_page()

        print("Navigálás a chatgpt.com-ra...")
        CHAT_PAGE.goto("https://chatgpt.com")

        print(f"LocalStorage 'oai-did' beállítása: {DEVICE_ID}")
        CHAT_PAGE.evaluate(
            f"""() => {{
            localStorage.setItem('oai-did', '{DEVICE_ID}');
        }}"""
        )

        print("Oldal frissítése a beállítások érvényesítéséhez...")
        CHAT_PAGE.reload()

        try:
            print("Várakozás a prompt mezőre (max 600s)...")
            CHAT_PAGE.wait_for_selector("#prompt-textarea", timeout=600000)
        except Exception as e:
            print(
                f"KRITIKUS HIBA az inicializáláskor: {e}. Valószínűleg lejártak a cookie-k."
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
                "HIBA: A böngésző inicializálása sikertelen. "
                f"Hiba: {e}. Kérem, frissítse a 'cookies.txt' és 'localstorage.txt' fájlokat."
            )

    if CHAT_PAGE is None:
        return (
            "HIBA: A böngésző munkamenet az előző kérés során leállt. "
            "Kérem indítsa újra a szervert."
        )

    page = CHAT_PAGE

    try:
        print(f"Prompt küldése: {prompt[:50]}...")
        page.fill("#prompt-textarea", prompt)

        send_button_selector = 'button[data-testid="send-button"]'
        response_container_selector = 'div[data-message-author-role="assistant"]'
        regenerate_button_selector = 'button[aria-label="Regenerate response"]'
        voice_mode_button_svg_path = 'path[d^="M7.167 15.416V4.583"]'
        voice_mode_button_selector = f"button:has({voice_mode_button_svg_path})"

        try:
            page.click(send_button_selector)
        except PlaywrightTimeoutError:
            page.keyboard.press("Enter")

        page.wait_for_selector(response_container_selector, timeout=10000)

        print("Generálás elindult. Várjuk a befejezést (max. ~100 perc)...")
        combined_completion_selector = f"{regenerate_button_selector}, {voice_mode_button_selector}"
        page.wait_for_selector(combined_completion_selector, timeout=60000000)
        print("Válasz sikeresen befejeződött.")

        # --- VÁLASZ KINYERÉSE (RAW) ---
        # Nem vágunk diff-et, nem pucolunk semmit, ami a ChatGPT UI-ban
        # az utolsó asszisztens üzenetben van, az megy vissza stringként.
        text = ""

        try:
            # Elsődlegesen a markdown tartalmat olvassuk ki az utolsó asszisztens üzenetből.
            response_locator = page.locator(f"{response_container_selector} .markdown").last
            if response_locator:
                raw = response_locator.inner_text() or ""
                text = raw
                print("RAW markdown szöveg kinyerve az utolsó asszisztens üzenetből.")
        except Exception as e:
            print(f"HIBA a markdown szöveg kiolvasásakor: {e}")

        if not text:
            # Ha valamiért nincs .markdown, essünk vissza az egész konténer szövegére.
            try:
                response_container = page.locator(response_container_selector).last
                if response_container:
                    raw = response_container.inner_text() or ""
                    text = raw
                    print("RAW szöveg kinyerve az asszisztens konténerből (fallback).")
            except Exception as e:
                print(f"További hiba a fallback során: {e}")

        if text:
            response_text = text
        else:
            print("HIBA: A kinyert szöveg üres maradt.")
            response_text = "HIBA: A kinyert szöveg üres maradt."

    except Exception as e:
        print(f"HIBA a folyamat közben: {e}. Munkamenet lezárva.")

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
            "HIBA: A Playwright nem tudta elküldeni a kérést. "
            f"Hiba: {e}"
        )

    return response_text


# ==========================================
# FLASK API
# ==========================================

app = Flask(__name__)


def _extract_text_from_content(content):
    """
    LiteLLM / OpenAI üzenet `content` mezőből kiszedi a szöveget.
    """
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
    """
    Az egész messages[] tömböt "kilapítja" egy darab nagy prompttá,
    hogy Aider összes korábbi user/assistant üzenete, fájltartalma stb.
    ténylegesen eljusson a ChatGPT web felülethez.
    """
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

    print(f"\n--- ÚJ KÉRÉS (összefűzött): {prompt[:60]}... ---")
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

    # OpenAI /v1/chat/completions-szerű válasz – formátum pontosan a doksi szerint
    response_data = {
        "id": "chatcmpl-" + str(uuid.uuid4()).replace("-", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "gpt-4o-playwright",
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
                    "id": "gpt-4o-playwright",
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
    """
    Lefut, amikor a Flask szerver leáll (pl. CTRL+C).
    """
    global BROWSER_CONTEXT, PLAYWRIGHT_INSTANCE
    if BROWSER_CONTEXT:
        print("\n🤖 Lezárás: Playwright böngésző bezárása (folyamatos munkamenet vége)...")
        try:
            BROWSER_CONTEXT.close()
        except Exception as e:
            print(f"Lezárási hiba: {e}")

    if PLAYWRIGHT_INSTANCE:
        try:
            PLAYWRIGHT_INSTANCE.stop()
        except Exception as e:
            print(f"Playwright stop hiba: {e}")


# ==========================================
# INDÍTÁS
# ==========================================
if __name__ == "__main__":
    atexit.register(shutdown_playwright)

    print("🤖 Playwright-alapú Aider API szerver indítása a http://127.0.0.1:5000 címen...")
    print("--- NE FELEJTSD EL KÉSZÍTENI AZ aider számára a 'cookies.txt' és 'localstorage.txt' fájlokat! ---")
    app.run(debug=False, port=5000, threaded=False)
