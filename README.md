# rev_chatgpt_codex

Playwright-alapú **OpenAI-kompatibilis proxy** ChatGPT és Google Gemini webes felülethez.  
Cél: bármilyen OpenAI-klienst (pl. **aider**, `openai` Python kliens, VSCode pluginek stb.) úgy használni,
mintha egy OpenAI modellt hívnál – valójában viszont a böngészőben futó ChatGPT / Gemini kapja a promptot.

⚠️ **Figyelmeztetés / disclaimer**

Ez a projekt kizárólag **saját fiókkal, saját böngészőből kinyert sessionre** épít, kísérleti/oktatási célra.
Előfordulhat, hogy a böngésző automatizálása, illetve a cookie-k ilyen célú felhasználása **ellentétes az adott
szolgáltató ÁSZF-ével**.  
**A kód használata teljes egészében a saját felelősséged**, mindig tartsd be a vonatkozó szabályokat.

---

## Fő funkciók

- **OpenAI /v1/chat/completions kompatibilis API** Flask-en keresztül
- **Playwright Chromium** böngésző persistent profillal
- Cookie + localStorage injektálás meglévő bejelentkezett sessionből
- Két külön „modell”:
  - `gpt-4o-playwright` – ChatGPT web (chatgpt.com)
  - `gemini-playwright` – Google Gemini web (gemini.google.com/app)
- Aiderrel használható pl.:

  ```bash
  export OPENAI_API_BASE=http://127.0.0.1:5000
  export OPENAI_API_KEY=dummy

  # ChatGPT
  aider --model openai/gpt-4o-playwright --edit-format diff --no-stream

  # Gemini
  aider --model openai/gemini-playwright --edit-format diff --no-stream
