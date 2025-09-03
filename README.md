## VoiceDict (Windows 11)

### Кратко (RU)
- Приложение для набора текста голосом в любом окне Windows.
- Горячая клавиша: Win+Shift+I — старт/стоп.
- Транскрибирует EN/RU, вставляет результат в активное поле ввода.
- Скриншот интерфейса: ![UI](./docs/ui.png)

### Быстрый запуск (RU)
- Вариант 1: запустите `run.bat` (создаст venv и поставит зависимости).
- Вариант 2: вручную (см. раздел Install ниже) и затем `python voice_dict.py`.

—

Hands‑free speech → text into any focused app (e.g., Cursor chat).

- Global hotkey: Win+Shift+I (toggle start/stop)
- Short sound effects on start/stop
- Transcribes English, Russian, and code reliably (OpenAI API)
- Types into the focused input using clipboard paste for robustness
- Compact always‑on‑top UI shows status and last transcript

### Requirements
- Windows 11
- Python 3.10+
- An OpenAI API key
- Recommended: run the Python process as Administrator (for reliable global hotkeys)

### Install
```bash
# In repo root
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# Configure API key
copy .env.example .env
# Edit .env and set OPENAI_API_KEY
```

### Run
```bash
python voice_dict.py
```
- Leave the small window visible or minimize it. The app listens globally.
- Press Win+Shift+I to start; press again to stop.
- Transcripts are pasted into the focused text field.

### Notes
- Audio capture: 16 kHz mono, WebRTC VAD segments, up to ~15s per chunk
- Models: tries `gpt-4o-transcribe` first, falls back to `whisper-1`
- Typing: uses clipboard + Ctrl+V to preserve formatting and speed
- Beeps: Windows `winsound` (no external files needed)

### Troubleshooting
- Hotkey not firing: run terminal as Administrator.
- No text appears: ensure the focused app accepts Ctrl+V. Try clicking the input box.
- API errors: verify `OPENAI_API_KEY` in `.env` and your account limits.
- Microphone busy: close other apps using the mic.

### Security
- Your audio is sent to OpenAI for transcription. Review your data policies.

### Uninstall
- Deactivate and remove the virtual environment:
```bash
.\.venv\Scripts\deactivate
rmdir /s /q .venv
```
