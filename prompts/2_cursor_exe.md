# Упаковка проекта в exe файл
_Exported on 9/3/2025 at 03:51:40 PDT from Cursor (1.5.9)_

---

**User**

Упакуй этот проект в `exe` файл для windows со всеми зависимостями внутри. Я хочу чтобы его мог запустить кто угодно. Конфиг пускай сохраняется в той же папке где и exe.

---

**Cursor**

I'll scan the project to find how `config.json` is handled and prepare build steps. Then I'll create a small TODO list and start by adjusting the config path to save beside the executable.

I'm going to scan the key files to see how the config path is currently handled and what the app's entrypoint and packaging needs are, so I can safely change the config location and set up PyInstaller correctly.

I updated the plan: I'll first change the config path in `voice_dict.py` to use the executable's directory when frozen, then set up and run a PyInstaller build in a virtual environment to create a one-file Windows exe. Next, I'll verify the exe exists in `dist` and confirm config behavior.

```python
import os
import sys
import threading
import queue
import time
import io
import wave
import json

import sounddevice as sd
import webrtcvad
import keyboard
import pyperclip

# Windows-only simple beeps
try:
	import winsound
	def play_start_beep():
		winsound.Beep(1200, 120)
	def play_stop_beep():
		winsound.Beep(600, 120)
except Exception:
	def play_start_beep():
		pass
	def play_stop_beep():
		pass

# Load OpenAI client
from dotenv import load_dotenv

try:
	from openai import OpenAI
	_openai_client = OpenAI()
except Exception as _e:
	_openai_client = None

APP_TITLE = "VoiceDict"
DEFAULT_HOTKEY = "windows+shift+i"  # Default global hotkey
SAMPLE_RATE = 16000
FRAME_DURATION_MS = 20  # 10/20/30ms supported by WebRTC VAD
CHANNELS = 1
VAD_AGGRESSIVENESS = 2  # 0-3, higher is more aggressive
MAX_SEGMENT_SECONDS = 15.0
TRAILING_SILENCE_MS = 600  # how much silence to consider end of utterance
# Determine base directory for resources/config
# - In a PyInstaller one-file build, use the directory of the executable
# - In dev (non-frozen), use the directory of this source file
if getattr(sys, 'frozen', False):
	BASE_DIR = os.path.dirname(sys.executable)
else:
	BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env from the base directory (supports both dev and packaged exe)
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Config stored next to the executable (or next to this file in dev)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

class AudioSegmenter:
	def __init__(self, sample_rate: int, frame_duration_ms: int, vad_level: int):
		self.sample_rate = sample_rate
		self.frame_duration_ms = frame_duration_ms
		self.frame_bytes = int(sample_rate * frame_duration_ms / 1000) * 2  # int16 mono
		self.vad = webrtcvad.Vad(vad_level)
		self.trailing_silence_frames_needed = int(TRAILING_SILENCE_MS / frame_duration_ms)
		self.max_frames_per_segment = int(MAX_SEGMENT_SECONDS * 1000 / frame_duration_ms)

	def stream_segments(self, stop_event: threading.Event):
		"""
		Generator that yields PCM16 mono bytes for each detected speech segment.
		Blocks reading from microphone using sounddevice RawInputStream.
		"""
		current_segment = bytearray()
		silence_run = 0
		frames_in_segment = 0
		
		with sd.RawInputStream(
			samplerate=self.sample_rate,
			channels=CHANNELS,
			dtype='int16',
			blocksize=int(self.sample_rate * self.frame_duration_ms / 1000),
		) as stream:
			while not stop_event.is_set():
				data, overflowed = stream.read(int(self.sample_rate * self.frame_duration_ms / 1000))
				if overflowed:
					# Drop frame on overflow
					continue
				frame_bytes = bytes(data)
				if len(frame_bytes) != self.frame_bytes:
					continue
				is_speech = self.vad.is_speech(frame_bytes, self.sample_rate)
				if is_speech:
					current_segment.extend(frame_bytes)
					frames_in_segment += 1
					silence_run = 0
					# If segment grows too long, flush early
					if frames_in_segment >= self.max_frames_per_segment:
						if current_segment:
							yield bytes(current_segment)
						current_segment = bytearray()
						frames_in_segment = 0
						silence_run = 0
				else:
					if frames_in_segment > 0:
						silence_run += 1
						# If enough trailing silence, flush segment
						if silence_run >= self.trailing_silence_frames_needed:
							if current_segment:
								yield bytes(current_segment)
							current_segment = bytearray()
							frames_in_segment = 0
							silence_run = 0
				# Allow UI to breathe
				time.sleep(0)
		# Flush any remaining audio at stop
		if current_segment:
			yield bytes(current_segment)


def pcm16_mono_to_wav_bytes(pcm_bytes: bytes, sample_rate: int) -> bytes:
	buf = io.BytesIO()
	with wave.open(buf, 'wb') as wf:
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(sample_rate)
		wf.writeframes(pcm_bytes)
	return buf.getvalue()


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
	if _openai_client is None:
		raise RuntimeError("OpenAI client not initialized. Check installation and API key.")
	last_exc = None
	for model_name in ("gpt-4o-transcribe", "whisper-1"):
		try:
			bio = io.BytesIO(wav_bytes)
			bio.name = "segment.wav"
			resp = _openai_client.audio.transcriptions.create(
				model=model_name,
				file=bio,
			)
			text = getattr(resp, "text", None)
			if not text and isinstance(resp, dict):
				text = resp.get("text")
			return text or ""
		except Exception as e:
			last_exc = e
			continue
	raise last_exc if last_exc else RuntimeError("Transcription failed")


def type_text_via_clipboard(text: str):
	if not text:
		return
	pyperclip.copy(text)
	keyboard.press_and_release('ctrl+v')


def load_config_hotkey() -> str:
	try:
		with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
			cfg = json.load(f)
			hk = cfg.get('hotkey')
			if isinstance(hk, str) and hk.strip():
				return hk
	except Exception:
		pass
	return DEFAULT_HOTKEY


def save_config_hotkey(hotkey: str):
	try:
		cfg = { 'hotkey': hotkey }
		with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
			json.dump(cfg, f, ensure_ascii=False, indent=2)
	except Exception:
		pass


class VoiceDictApp:
	def __init__(self):
		self.listening = False
		self.stop_event = threading.Event()
		self.segments_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
		self.transcriber_thread: threading.Thread | None = None
		self.capturer_thread: threading.Thread | None = None
		self.hotkey_handle = None
		self.hotkey_string = load_config_hotkey()
		# tray removed
		# tray removed

		# UI
		import tkinter as tk
		self.tk = tk
		self.root = tk.Tk()
		self.root.title(APP_TITLE)
		self.root.attributes('-topmost', True)
		self.root.resizable(False, False)
		self.status_var = tk.StringVar(value='Idle')
		self.last_text_var = tk.StringVar(value='')

		self.header = tk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 12, "bold"))
		self.header.pack(padx=10, pady=(8, 2))
		self.status = tk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 10))
		self.status.pack(padx=10)
		self.last = tk.Label(self.root, textvariable=self.last_text_var, font=("Consolas", 9), wraplength=260, justify='left')
		self.last.pack(padx=10, pady=(6, 6))
		self.hint_var = tk.StringVar(value=f"Hotkey: {self.hotkey_string}")
		self.hint = tk.Label(self.root, textvariable=self.hint_var, font=("Segoe UI", 8))
		self.hint.pack(padx=10, pady=(0, 6))
		self.change_btn = tk.Button(self.root, text="Change Hotkey", command=self.begin_hotkey_capture)
		self.change_btn.pack(padx=10, pady=(0, 8))
		self.root.geometry("320x175")

		# Register hotkey
		self._register_hotkey(self.hotkey_string)

		# tray removed
		# tray removed

		# Graceful close
		self.root.protocol("WM_DELETE_WINDOW", self.on_close)

	def _set_status_ui(self, text: str):
		self.status_var.set(text)

	def set_status(self, text: str):
		try:
			self.root.after(0, self._set_status_ui, text)
		except Exception:
			pass

	def _set_last_text_ui(self, text: str):
		self.last_text_var.set(text)

	def set_last_text(self, text: str):
		preview = text.strip().replace('\n', ' ')
		if len(preview) > 120:
			preview = preview[:117] + '...'
		try:
			self.root.after(0, self._set_last_text_ui, preview)
		except Exception:
			pass

	def _set_hint_ui(self, text: str):
		self.hint_var.set(text)

	def update_hint(self):
		self.root.after(0, self._set_hint_ui, f"Hotkey: {self.hotkey_string}")

	def _register_hotkey(self, hotkey: str) -> bool:
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		try:
			self.hotkey_handle = keyboard.add_hotkey(hotkey, self.toggle_listening, suppress=False)
			return True
		except Exception as e:
			self.set_status(f"Hotkey failed: {e}")
			self.hotkey_handle = None
			return False

	def begin_hotkey_capture(self):
		# Temporarily remove current hotkey to avoid accidental toggles
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
				self.hotkey_handle = None
		except Exception:
			pass
		# Disable button and prompt
		try:
			self.change_btn.config(state='disabled')
		except Exception:
			pass
		self.set_status('Press the new hotkey (Esc to cancel)...')
		threading.Thread(target=self._hotkey_capture_thread, name='HotkeyCapture', daemon=True).start()

	def _hotkey_capture_thread(self):
		new_hotkey = None
		try:
			# Block until a combo is pressed; do not suppress
			new_hotkey = keyboard.read_hotkey(suppress=False)
		except Exception as e:
			self.set_status(f"Capture failed: {e}")
		finally:
			self.root.after(0, self._finish_hotkey_capture, new_hotkey)

	def _finish_hotkey_capture(self, new_hotkey: str | None):
		# Re-enable button
		try:
			self.change_btn.config(state='normal')
		except Exception:
			pass
		# Handle cancel
		if not new_hotkey or new_hotkey.lower() == 'esc':
			self.set_status('Hotkey change canceled')
			# Re-register previous hotkey
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			return
		# Try register new hotkey
		if self._register_hotkey(new_hotkey):
			self.hotkey_string = new_hotkey
			save_config_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status(f"Hotkey set: {self.hotkey_string}")
		else:
			# Failed: restore previous
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status('Failed to set hotkey')

	def toggle_listening(self):
		if self.listening:
			self.stop_listening()
		else:
			self.start_listening()

	def start_listening(self):
		if self.listening:
			return
		self.listening = True
		self.stop_event.clear()
		self.set_status('Listening...')
		play_start_beep()

		# Start transcriber worker
		self.transcriber_thread = threading.Thread(target=self._transcriber_loop, name='Transcriber', daemon=True)
		self.transcriber_thread.start()

		# Start audio capturer worker
		self.capturer_thread = threading.Thread(target=self._capture_loop, name='AudioCapture', daemon=True)
		self.capturer_thread.start()

	def stop_listening(self):
		if not self.listening:
			return
		self.listening = False
		self.stop_event.set()
		play_stop_beep()
		self.set_status('Stopping...')

		# Allow threads to drain
		time.sleep(0.05)
		self.set_status('Idle')

	def _capture_loop(self):
		segmenter = AudioSegmenter(SAMPLE_RATE, FRAME_DURATION_MS, VAD_AGGRESSIVENESS)
		try:
			for pcm_segment in segmenter.stream_segments(self.stop_event):
				if not self.listening:
					break
				wav_bytes = pcm16_mono_to_wav_bytes(pcm_segment, SAMPLE_RATE)
				try:
					self.segments_queue.put(wav_bytes, timeout=0.1)
				except queue.Full:
					# Drop if backlog
					pass
		except Exception as e:
			self.set_status(f"Audio error: {e}")

	def _transcriber_loop(self):
		while not self.stop_event.is_set() or not self.segments_queue.empty():
			try:
				wav_bytes = self.segments_queue.get(timeout=0.1)
			except queue.Empty:
				continue
			try:
				text = transcribe_wav_bytes(wav_bytes)
				if text:
					self.set_last_text(text)
					# Insert a trailing space to keep flowing
					type_text_via_clipboard(text + ' ')
			except Exception as e:
				self.set_status(f"Transcribe error: {e}")

	def _on_unmap(self, _event):
		# If minimized, hide to tray
		try:
			if self.root.state() == 'iconic':
				# tray removed
				pass
		except Exception:
			pass

	def _tray_show(self):
		try:
			self.root.deiconify()
			self.root.after(0, self.root.lift)
			# tray removed
		except Exception:
			pass

	def _tray_hide(self):
		try:
			self.root.withdraw()
			# tray removed
		except Exception:
			pass

	def _tray_exit(self):
		self.on_close()

	def on_close(self):
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		self.stop_event.set()
		self.listening = False
		# tray removed
		# tray removed
		self.root.after(50, self.root.destroy)


def main():
	api_key = os.environ.get('OPENAI_API_KEY', '')
	if not api_key:
		print("ERROR: OPENAI_API_KEY is not set. Create a .env or set env var.")
		# Proceed but likely to fail during transcription
	app = VoiceDictApp()
	print(f"{APP_TITLE} running. Global hotkey: {app.hotkey_string}")
	app.root.mainloop()


if __name__ == "__main__":
	main()

```

I'll install PyInstaller, ensure dependencies are present, and then build a one-file, windowed exe.

I'll create a dedicated build venv, install dependencies and PyInstaller, and build the one-file exe. Then I'll verify the exe exists in `dist`.

I'll set up the build using Python, install dependencies and PyInstaller, then run the PyInstaller build. If the 'py' launcher isn't available, I'll fall back to 'python'.

I'll check the PyInstaller output directory to confirm the exe was built and identify its exact path.

I'll build the exe with PyInstaller again (no piping), then check that `dist\\VoiceDict.exe` exists.

I’m adding a custom PyInstaller hook to bypass the failing contrib hook for `webrtcvad`, then I’ll rebuild the one-file exe and verify the output in `dist`.

```python
# Minimal override hook for webrtcvad to avoid copy_metadata on nonstandard wheels
# The compiled extension should be auto-detected; no extra datas/binaries needed.
datas = []
binaries = []
hiddenimports = []

```

- Built a one-file Windows exe with PyInstaller and made config save next to the executable.
- The exe is at `dist\VoiceDict.exe`. `config.json` is also in `dist\` and will be read/written there on every run.
- `.env` (for `OPENAI_API_KEY`) will be looked for next to the exe.

How to run and share:
1) Put your `OPENAI_API_KEY` into a `.env` file next to the exe (same folder) in the format:
```
OPENAI_API_KEY=sk-...
```
2) Double-click `VoiceDict.exe`. On first run, `config.json` will be created/updated in the same folder.
3) Share the `dist\VoiceDict.exe` file (and optionally `.env` instructions). No Python needed on target machines.

Notes:
- If SmartScreen warns, click “More info” → “Run anyway” (unsigned binary).
- Global hotkeys may require admin privileges on some systems.

I also added a local PyInstaller hook to avoid a packaging issue with `webrtcvad`.

```plaintext
OPENAI_API_KEY=sk-...
```

---

**User**

Добавь в билд также файл .env, создавай если его нет с пустым ключем в директории запуска, так же как и config.json, и читай ключ оттуда.

---

**Cursor**

I'll add runtime creation of `.env` (with empty `OPENAI_API_KEY`) and `config.json` in the executable directory, adjust loading order to read from that `.env`, rebuild the exe, and verify it.

I'll implement runtime checks in `voice_dict.py` to create `.env` with an empty `OPENAI_API_KEY=` and `config.json` if missing, then load the `.env` from the exe directory before reading the API key. Then I’ll rebuild the exe.

I'll update `voice_dict.py` to auto-create `.env` and `config.json` in the executable directory on startup, ensure `.env` is loaded before initializing the OpenAI client, then rebuild the exe and verify `.env` gets created next to it at runtime.

```python
import os
import sys
import threading
import queue
import time
import io
import wave
import json

import sounddevice as sd
import webrtcvad
import keyboard
import pyperclip

# Windows-only simple beeps
try:
	import winsound
	def play_start_beep():
		winsound.Beep(1200, 120)
	def play_stop_beep():
		winsound.Beep(600, 120)
except Exception:
	def play_start_beep():
		pass
	def play_stop_beep():
		pass

# Load OpenAI client
from dotenv import load_dotenv

APP_TITLE = "VoiceDict"
DEFAULT_HOTKEY = "windows+shift+i"  # Default global hotkey
SAMPLE_RATE = 16000
FRAME_DURATION_MS = 20  # 10/20/30ms supported by WebRTC VAD
CHANNELS = 1
VAD_AGGRESSIVENESS = 2  # 0-3, higher is more aggressive
MAX_SEGMENT_SECONDS = 15.0
TRAILING_SILENCE_MS = 600  # how much silence to consider end of utterance
# Determine base directory for resources/config
# - In a PyInstaller one-file build, use the directory of the executable
# - In dev (non-frozen), use the directory of this source file
if getattr(sys, 'frozen', False):
	BASE_DIR = os.path.dirname(sys.executable)
else:
	BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env from the base directory (supports both dev and packaged exe)
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Config stored next to the executable (or next to this file in dev)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

class AudioSegmenter:
	def __init__(self, sample_rate: int, frame_duration_ms: int, vad_level: int):
		self.sample_rate = sample_rate
		self.frame_duration_ms = frame_duration_ms
		self.frame_bytes = int(sample_rate * frame_duration_ms / 1000) * 2  # int16 mono
		self.vad = webrtcvad.Vad(vad_level)
		self.trailing_silence_frames_needed = int(TRAILING_SILENCE_MS / frame_duration_ms)
		self.max_frames_per_segment = int(MAX_SEGMENT_SECONDS * 1000 / frame_duration_ms)

	def stream_segments(self, stop_event: threading.Event):
		"""
		Generator that yields PCM16 mono bytes for each detected speech segment.
		Blocks reading from microphone using sounddevice RawInputStream.
		"""
		current_segment = bytearray()
		silence_run = 0
		frames_in_segment = 0
		
		with sd.RawInputStream(
			samplerate=self.sample_rate,
			channels=CHANNELS,
			dtype='int16',
			blocksize=int(self.sample_rate * self.frame_duration_ms / 1000),
		) as stream:
			while not stop_event.is_set():
				data, overflowed = stream.read(int(self.sample_rate * self.frame_duration_ms / 1000))
				if overflowed:
					# Drop frame on overflow
					continue
				frame_bytes = bytes(data)
				if len(frame_bytes) != self.frame_bytes:
					continue
				is_speech = self.vad.is_speech(frame_bytes, self.sample_rate)
				if is_speech:
					current_segment.extend(frame_bytes)
					frames_in_segment += 1
					silence_run = 0
					# If segment grows too long, flush early
					if frames_in_segment >= self.max_frames_per_segment:
						if current_segment:
							yield bytes(current_segment)
						current_segment = bytearray()
						frames_in_segment = 0
						silence_run = 0
				else:
					if frames_in_segment > 0:
						silence_run += 1
						# If enough trailing silence, flush segment
						if silence_run >= self.trailing_silence_frames_needed:
							if current_segment:
								yield bytes(current_segment)
							current_segment = bytearray()
							frames_in_segment = 0
							silence_run = 0
				# Allow UI to breathe
				time.sleep(0)
		# Flush any remaining audio at stop
		if current_segment:
			yield bytes(current_segment)


def pcm16_mono_to_wav_bytes(pcm_bytes: bytes, sample_rate: int) -> bytes:
	buf = io.BytesIO()
	with wave.open(buf, 'wb') as wf:
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(sample_rate)
		wf.writeframes(pcm_bytes)
	return buf.getvalue()


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
	if _openai_client is None:
		raise RuntimeError("OpenAI client not initialized. Check installation and API key.")
	last_exc = None
	for model_name in ("gpt-4o-transcribe", "whisper-1"):
		try:
			bio = io.BytesIO(wav_bytes)
			bio.name = "segment.wav"
			resp = _openai_client.audio.transcriptions.create(
				model=model_name,
				file=bio,
			)
			text = getattr(resp, "text", None)
			if not text and isinstance(resp, dict):
				text = resp.get("text")
			return text or ""
		except Exception as e:
			last_exc = e
			continue
	raise last_exc if last_exc else RuntimeError("Transcription failed")


def type_text_via_clipboard(text: str):
	if not text:
		return
	pyperclip.copy(text)
	keyboard.press_and_release('ctrl+v')


def load_config_hotkey() -> str:
	try:
		with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
			cfg = json.load(f)
			hk = cfg.get('hotkey')
			if isinstance(hk, str) and hk.strip():
				return hk
	except Exception:
		pass
	return DEFAULT_HOTKEY


def save_config_hotkey(hotkey: str):
	try:
		cfg = { 'hotkey': hotkey }
		with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
			json.dump(cfg, f, ensure_ascii=False, indent=2)
	except Exception:
		pass


class VoiceDictApp:
	def __init__(self):
		self.listening = False
		self.stop_event = threading.Event()
		self.segments_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
		self.transcriber_thread: threading.Thread | None = None
		self.capturer_thread: threading.Thread | None = None
		self.hotkey_handle = None
		self.hotkey_string = load_config_hotkey()
		# tray removed
		# tray removed

		# UI
		import tkinter as tk
		self.tk = tk
		self.root = tk.Tk()
		self.root.title(APP_TITLE)
		self.root.attributes('-topmost', True)
		self.root.resizable(False, False)
		self.status_var = tk.StringVar(value='Idle')
		self.last_text_var = tk.StringVar(value='')

		self.header = tk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 12, "bold"))
		self.header.pack(padx=10, pady=(8, 2))
		self.status = tk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 10))
		self.status.pack(padx=10)
		self.last = tk.Label(self.root, textvariable=self.last_text_var, font=("Consolas", 9), wraplength=260, justify='left')
		self.last.pack(padx=10, pady=(6, 6))
		self.hint_var = tk.StringVar(value=f"Hotkey: {self.hotkey_string}")
		self.hint = tk.Label(self.root, textvariable=self.hint_var, font=("Segoe UI", 8))
		self.hint.pack(padx=10, pady=(0, 6))
		self.change_btn = tk.Button(self.root, text="Change Hotkey", command=self.begin_hotkey_capture)
		self.change_btn.pack(padx=10, pady=(0, 8))
		self.root.geometry("320x175")

		# Register hotkey
		self._register_hotkey(self.hotkey_string)

		# tray removed
		# tray removed

		# Graceful close
		self.root.protocol("WM_DELETE_WINDOW", self.on_close)

	def _set_status_ui(self, text: str):
		self.status_var.set(text)

	def set_status(self, text: str):
		try:
			self.root.after(0, self._set_status_ui, text)
		except Exception:
			pass

	def _set_last_text_ui(self, text: str):
		self.last_text_var.set(text)

	def set_last_text(self, text: str):
		preview = text.strip().replace('\n', ' ')
		if len(preview) > 120:
			preview = preview[:117] + '...'
		try:
			self.root.after(0, self._set_last_text_ui, preview)
		except Exception:
			pass

	def _set_hint_ui(self, text: str):
		self.hint_var.set(text)

	def update_hint(self):
		self.root.after(0, self._set_hint_ui, f"Hotkey: {self.hotkey_string}")

	def _register_hotkey(self, hotkey: str) -> bool:
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		try:
			self.hotkey_handle = keyboard.add_hotkey(hotkey, self.toggle_listening, suppress=False)
			return True
		except Exception as e:
			self.set_status(f"Hotkey failed: {e}")
			self.hotkey_handle = None
			return False

	def begin_hotkey_capture(self):
		# Temporarily remove current hotkey to avoid accidental toggles
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
				self.hotkey_handle = None
		except Exception:
			pass
		# Disable button and prompt
		try:
			self.change_btn.config(state='disabled')
		except Exception:
			pass
		self.set_status('Press the new hotkey (Esc to cancel)...')
		threading.Thread(target=self._hotkey_capture_thread, name='HotkeyCapture', daemon=True).start()

	def _hotkey_capture_thread(self):
		new_hotkey = None
		try:
			# Block until a combo is pressed; do not suppress
			new_hotkey = keyboard.read_hotkey(suppress=False)
		except Exception as e:
			self.set_status(f"Capture failed: {e}")
		finally:
			self.root.after(0, self._finish_hotkey_capture, new_hotkey)

	def _finish_hotkey_capture(self, new_hotkey: str | None):
		# Re-enable button
		try:
			self.change_btn.config(state='normal')
		except Exception:
			pass
		# Handle cancel
		if not new_hotkey or new_hotkey.lower() == 'esc':
			self.set_status('Hotkey change canceled')
			# Re-register previous hotkey
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			return
		# Try register new hotkey
		if self._register_hotkey(new_hotkey):
			self.hotkey_string = new_hotkey
			save_config_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status(f"Hotkey set: {self.hotkey_string}")
		else:
			# Failed: restore previous
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status('Failed to set hotkey')

	def toggle_listening(self):
		if self.listening:
			self.stop_listening()
		else:
			self.start_listening()

	def start_listening(self):
		if self.listening:
			return
		self.listening = True
		self.stop_event.clear()
		self.set_status('Listening...')
		play_start_beep()

		# Start transcriber worker
		self.transcriber_thread = threading.Thread(target=self._transcriber_loop, name='Transcriber', daemon=True)
		self.transcriber_thread.start()

		# Start audio capturer worker
		self.capturer_thread = threading.Thread(target=self._capture_loop, name='AudioCapture', daemon=True)
		self.capturer_thread.start()

	def stop_listening(self):
		if not self.listening:
			return
		self.listening = False
		self.stop_event.set()
		play_stop_beep()
		self.set_status('Stopping...')

		# Allow threads to drain
		time.sleep(0.05)
		self.set_status('Idle')

	def _capture_loop(self):
		segmenter = AudioSegmenter(SAMPLE_RATE, FRAME_DURATION_MS, VAD_AGGRESSIVENESS)
		try:
			for pcm_segment in segmenter.stream_segments(self.stop_event):
				if not self.listening:
					break
				wav_bytes = pcm16_mono_to_wav_bytes(pcm_segment, SAMPLE_RATE)
				try:
					self.segments_queue.put(wav_bytes, timeout=0.1)
				except queue.Full:
					# Drop if backlog
					pass
		except Exception as e:
			self.set_status(f"Audio error: {e}")

	def _transcriber_loop(self):
		while not self.stop_event.is_set() or not self.segments_queue.empty():
			try:
				wav_bytes = self.segments_queue.get(timeout=0.1)
			except queue.Empty:
				continue
			try:
				text = transcribe_wav_bytes(wav_bytes)
				if text:
					self.set_last_text(text)
					# Insert a trailing space to keep flowing
					type_text_via_clipboard(text + ' ')
			except Exception as e:
				self.set_status(f"Transcribe error: {e}")

	def _on_unmap(self, _event):
		# If minimized, hide to tray
		try:
			if self.root.state() == 'iconic':
				# tray removed
				pass
		except Exception:
			pass

	def _tray_show(self):
		try:
			self.root.deiconify()
			self.root.after(0, self.root.lift)
			# tray removed
		except Exception:
			pass

	def _tray_hide(self):
		try:
			self.root.withdraw()
			# tray removed
		except Exception:
			pass

	def _tray_exit(self):
		self.on_close()

	def on_close(self):
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		self.stop_event.set()
		self.listening = False
		# tray removed
		# tray removed
		self.root.after(50, self.root.destroy)


def main():
	api_key = os.environ.get('OPENAI_API_KEY', '')
	if not api_key:
		print("ERROR: OPENAI_API_KEY is not set. Create a .env or set env var.")
		# Proceed but likely to fail during transcription
	app = VoiceDictApp()
	print(f"{APP_TITLE} running. Global hotkey: {app.hotkey_string}")
	app.root.mainloop()


if __name__ == "__main__":
	main()

```

```python
import os
import sys
import threading
import queue
import time
import io
import wave
import json

import sounddevice as sd
import webrtcvad
import keyboard
import pyperclip

# Windows-only simple beeps
try:
	import winsound
	def play_start_beep():
		winsound.Beep(1200, 120)
	def play_stop_beep():
		winsound.Beep(600, 120)
except Exception:
	def play_start_beep():
		pass
	def play_stop_beep():
		pass

# Load OpenAI client
from dotenv import load_dotenv

APP_TITLE = "VoiceDict"
DEFAULT_HOTKEY = "windows+shift+i"  # Default global hotkey
SAMPLE_RATE = 16000
FRAME_DURATION_MS = 20  # 10/20/30ms supported by WebRTC VAD
CHANNELS = 1
VAD_AGGRESSIVENESS = 2  # 0-3, higher is more aggressive
MAX_SEGMENT_SECONDS = 15.0
TRAILING_SILENCE_MS = 600  # how much silence to consider end of utterance
# Determine base directory for resources/config
# - In a PyInstaller one-file build, use the directory of the executable
# - In dev (non-frozen), use the directory of this source file
if getattr(sys, 'frozen', False):
	BASE_DIR = os.path.dirname(sys.executable)
else:
	BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure .env and config.json exist next to the executable
ENV_PATH = os.path.join(BASE_DIR, ".env")
# Create .env if missing with empty key
try:
	if not os.path.exists(ENV_PATH):
		with open(ENV_PATH, 'w', encoding='utf-8') as _f:
			_f.write("OPENAI_API_KEY=\n")
except Exception:
	pass

# Load .env from the base directory (supports both dev and packaged exe)
load_dotenv(ENV_PATH)

# Config stored next to the executable (or next to this file in dev)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

class AudioSegmenter:
	def __init__(self, sample_rate: int, frame_duration_ms: int, vad_level: int):
		self.sample_rate = sample_rate
		self.frame_duration_ms = frame_duration_ms
		self.frame_bytes = int(sample_rate * frame_duration_ms / 1000) * 2  # int16 mono
		self.vad = webrtcvad.Vad(vad_level)
		self.trailing_silence_frames_needed = int(TRAILING_SILENCE_MS / frame_duration_ms)
		self.max_frames_per_segment = int(MAX_SEGMENT_SECONDS * 1000 / frame_duration_ms)

	def stream_segments(self, stop_event: threading.Event):
		"""
		Generator that yields PCM16 mono bytes for each detected speech segment.
		Blocks reading from microphone using sounddevice RawInputStream.
		"""
		current_segment = bytearray()
		silence_run = 0
		frames_in_segment = 0
		
		with sd.RawInputStream(
			samplerate=self.sample_rate,
			channels=CHANNELS,
			dtype='int16',
			blocksize=int(self.sample_rate * self.frame_duration_ms / 1000),
		) as stream:
			while not stop_event.is_set():
				data, overflowed = stream.read(int(self.sample_rate * self.frame_duration_ms / 1000))
				if overflowed:
					# Drop frame on overflow
					continue
				frame_bytes = bytes(data)
				if len(frame_bytes) != self.frame_bytes:
					continue
				is_speech = self.vad.is_speech(frame_bytes, self.sample_rate)
				if is_speech:
					current_segment.extend(frame_bytes)
					frames_in_segment += 1
					silence_run = 0
					# If segment grows too long, flush early
					if frames_in_segment >= self.max_frames_per_segment:
						if current_segment:
							yield bytes(current_segment)
						current_segment = bytearray()
						frames_in_segment = 0
						silence_run = 0
				else:
					if frames_in_segment > 0:
						silence_run += 1
						# If enough trailing silence, flush segment
						if silence_run >= self.trailing_silence_frames_needed:
							if current_segment:
								yield bytes(current_segment)
							current_segment = bytearray()
							frames_in_segment = 0
							silence_run = 0
				# Allow UI to breathe
				time.sleep(0)
		# Flush any remaining audio at stop
		if current_segment:
			yield bytes(current_segment)


def pcm16_mono_to_wav_bytes(pcm_bytes: bytes, sample_rate: int) -> bytes:
	buf = io.BytesIO()
	with wave.open(buf, 'wb') as wf:
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(sample_rate)
		wf.writeframes(pcm_bytes)
	return buf.getvalue()


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
	if _openai_client is None:
		raise RuntimeError("OpenAI client not initialized. Check installation and API key.")
	last_exc = None
	for model_name in ("gpt-4o-transcribe", "whisper-1"):
		try:
			bio = io.BytesIO(wav_bytes)
			bio.name = "segment.wav"
			resp = _openai_client.audio.transcriptions.create(
				model=model_name,
				file=bio,
			)
			text = getattr(resp, "text", None)
			if not text and isinstance(resp, dict):
				text = resp.get("text")
			return text or ""
		except Exception as e:
			last_exc = e
			continue
	raise last_exc if last_exc else RuntimeError("Transcription failed")


def type_text_via_clipboard(text: str):
	if not text:
		return
	pyperclip.copy(text)
	keyboard.press_and_release('ctrl+v')


def load_config_hotkey() -> str:
	try:
		with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
			cfg = json.load(f)
			hk = cfg.get('hotkey')
			if isinstance(hk, str) and hk.strip():
				return hk
	except Exception:
		pass
	return DEFAULT_HOTKEY


def save_config_hotkey(hotkey: str):
	try:
		cfg = { 'hotkey': hotkey }
		with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
			json.dump(cfg, f, ensure_ascii=False, indent=2)
	except Exception:
		pass


class VoiceDictApp:
	def __init__(self):
		self.listening = False
		self.stop_event = threading.Event()
		self.segments_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
		self.transcriber_thread: threading.Thread | None = None
		self.capturer_thread: threading.Thread | None = None
		self.hotkey_handle = None
		self.hotkey_string = load_config_hotkey()
		# tray removed
		# tray removed

		# UI
		import tkinter as tk
		self.tk = tk
		self.root = tk.Tk()
		self.root.title(APP_TITLE)
		self.root.attributes('-topmost', True)
		self.root.resizable(False, False)
		self.status_var = tk.StringVar(value='Idle')
		self.last_text_var = tk.StringVar(value='')

		self.header = tk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 12, "bold"))
		self.header.pack(padx=10, pady=(8, 2))
		self.status = tk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 10))
		self.status.pack(padx=10)
		self.last = tk.Label(self.root, textvariable=self.last_text_var, font=("Consolas", 9), wraplength=260, justify='left')
		self.last.pack(padx=10, pady=(6, 6))
		self.hint_var = tk.StringVar(value=f"Hotkey: {self.hotkey_string}")
		self.hint = tk.Label(self.root, textvariable=self.hint_var, font=("Segoe UI", 8))
		self.hint.pack(padx=10, pady=(0, 6))
		self.change_btn = tk.Button(self.root, text="Change Hotkey", command=self.begin_hotkey_capture)
		self.change_btn.pack(padx=10, pady=(0, 8))
		self.root.geometry("320x175")

		# Register hotkey
		self._register_hotkey(self.hotkey_string)

		# tray removed
		# tray removed

		# Graceful close
		self.root.protocol("WM_DELETE_WINDOW", self.on_close)

	def _set_status_ui(self, text: str):
		self.status_var.set(text)

	def set_status(self, text: str):
		try:
			self.root.after(0, self._set_status_ui, text)
		except Exception:
			pass

	def _set_last_text_ui(self, text: str):
		self.last_text_var.set(text)

	def set_last_text(self, text: str):
		preview = text.strip().replace('\n', ' ')
		if len(preview) > 120:
			preview = preview[:117] + '...'
		try:
			self.root.after(0, self._set_last_text_ui, preview)
		except Exception:
			pass

	def _set_hint_ui(self, text: str):
		self.hint_var.set(text)

	def update_hint(self):
		self.root.after(0, self._set_hint_ui, f"Hotkey: {self.hotkey_string}")

	def _register_hotkey(self, hotkey: str) -> bool:
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		try:
			self.hotkey_handle = keyboard.add_hotkey(hotkey, self.toggle_listening, suppress=False)
			return True
		except Exception as e:
			self.set_status(f"Hotkey failed: {e}")
			self.hotkey_handle = None
			return False

	def begin_hotkey_capture(self):
		# Temporarily remove current hotkey to avoid accidental toggles
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
				self.hotkey_handle = None
		except Exception:
			pass
		# Disable button and prompt
		try:
			self.change_btn.config(state='disabled')
		except Exception:
			pass
		self.set_status('Press the new hotkey (Esc to cancel)...')
		threading.Thread(target=self._hotkey_capture_thread, name='HotkeyCapture', daemon=True).start()

	def _hotkey_capture_thread(self):
		new_hotkey = None
		try:
			# Block until a combo is pressed; do not suppress
			new_hotkey = keyboard.read_hotkey(suppress=False)
		except Exception as e:
			self.set_status(f"Capture failed: {e}")
		finally:
			self.root.after(0, self._finish_hotkey_capture, new_hotkey)

	def _finish_hotkey_capture(self, new_hotkey: str | None):
		# Re-enable button
		try:
			self.change_btn.config(state='normal')
		except Exception:
			pass
		# Handle cancel
		if not new_hotkey or new_hotkey.lower() == 'esc':
			self.set_status('Hotkey change canceled')
			# Re-register previous hotkey
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			return
		# Try register new hotkey
		if self._register_hotkey(new_hotkey):
			self.hotkey_string = new_hotkey
			save_config_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status(f"Hotkey set: {self.hotkey_string}")
		else:
			# Failed: restore previous
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status('Failed to set hotkey')

	def toggle_listening(self):
		if self.listening:
			self.stop_listening()
		else:
			self.start_listening()

	def start_listening(self):
		if self.listening:
			return
		self.listening = True
		self.stop_event.clear()
		self.set_status('Listening...')
		play_start_beep()

		# Start transcriber worker
		self.transcriber_thread = threading.Thread(target=self._transcriber_loop, name='Transcriber', daemon=True)
		self.transcriber_thread.start()

		# Start audio capturer worker
		self.capturer_thread = threading.Thread(target=self._capture_loop, name='AudioCapture', daemon=True)
		self.capturer_thread.start()

	def stop_listening(self):
		if not self.listening:
			return
		self.listening = False
		self.stop_event.set()
		play_stop_beep()
		self.set_status('Stopping...')

		# Allow threads to drain
		time.sleep(0.05)
		self.set_status('Idle')

	def _capture_loop(self):
		segmenter = AudioSegmenter(SAMPLE_RATE, FRAME_DURATION_MS, VAD_AGGRESSIVENESS)
		try:
			for pcm_segment in segmenter.stream_segments(self.stop_event):
				if not self.listening:
					break
				wav_bytes = pcm16_mono_to_wav_bytes(pcm_segment, SAMPLE_RATE)
				try:
					self.segments_queue.put(wav_bytes, timeout=0.1)
				except queue.Full:
					# Drop if backlog
					pass
		except Exception as e:
			self.set_status(f"Audio error: {e}")

	def _transcriber_loop(self):
		while not self.stop_event.is_set() or not self.segments_queue.empty():
			try:
				wav_bytes = self.segments_queue.get(timeout=0.1)
			except queue.Empty:
				continue
			try:
				text = transcribe_wav_bytes(wav_bytes)
				if text:
					self.set_last_text(text)
					# Insert a trailing space to keep flowing
					type_text_via_clipboard(text + ' ')
			except Exception as e:
				self.set_status(f"Transcribe error: {e}")

	def _on_unmap(self, _event):
		# If minimized, hide to tray
		try:
			if self.root.state() == 'iconic':
				# tray removed
				pass
		except Exception:
			pass

	def _tray_show(self):
		try:
			self.root.deiconify()
			self.root.after(0, self.root.lift)
			# tray removed
		except Exception:
			pass

	def _tray_hide(self):
		try:
			self.root.withdraw()
			# tray removed
		except Exception:
			pass

	def _tray_exit(self):
		self.on_close()

	def on_close(self):
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		self.stop_event.set()
		self.listening = False
		# tray removed
		# tray removed
		self.root.after(50, self.root.destroy)


def main():
	api_key = os.environ.get('OPENAI_API_KEY', '')
	if not api_key:
		print("ERROR: OPENAI_API_KEY is not set. Create a .env or set env var.")
		# Proceed but likely to fail during transcription
	app = VoiceDictApp()
	print(f"{APP_TITLE} running. Global hotkey: {app.hotkey_string}")
	app.root.mainloop()


if __name__ == "__main__":
	main()

```

```python
import os
import sys
import threading
import queue
import time
import io
import wave
import json

import sounddevice as sd
import webrtcvad
import keyboard
import pyperclip

# Windows-only simple beeps
try:
	import winsound
	def play_start_beep():
		winsound.Beep(1200, 120)
	def play_stop_beep():
		winsound.Beep(600, 120)
except Exception:
	def play_start_beep():
		pass
	def play_stop_beep():
		pass

# Load OpenAI client
from dotenv import load_dotenv

APP_TITLE = "VoiceDict"
DEFAULT_HOTKEY = "windows+shift+i"  # Default global hotkey
SAMPLE_RATE = 16000
FRAME_DURATION_MS = 20  # 10/20/30ms supported by WebRTC VAD
CHANNELS = 1
VAD_AGGRESSIVENESS = 2  # 0-3, higher is more aggressive
MAX_SEGMENT_SECONDS = 15.0
TRAILING_SILENCE_MS = 600  # how much silence to consider end of utterance
# Determine base directory for resources/config
# - In a PyInstaller one-file build, use the directory of the executable
# - In dev (non-frozen), use the directory of this source file
if getattr(sys, 'frozen', False):
	BASE_DIR = os.path.dirname(sys.executable)
else:
	BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure .env and config.json exist next to the executable
ENV_PATH = os.path.join(BASE_DIR, ".env")
# Create .env if missing with empty key
try:
	if not os.path.exists(ENV_PATH):
		with open(ENV_PATH, 'w', encoding='utf-8') as _f:
			_f.write("OPENAI_API_KEY=\n")
except Exception:
	pass

# Load .env from the base directory (supports both dev and packaged exe)
load_dotenv(ENV_PATH)

# Config stored next to the executable (or next to this file in dev)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
try:
	if not os.path.exists(CONFIG_PATH):
		with open(CONFIG_PATH, 'w', encoding='utf-8') as _f:
			_f.write('{"hotkey": "' + DEFAULT_HOTKEY + '"}')
except Exception:
	pass

class AudioSegmenter:
	def __init__(self, sample_rate: int, frame_duration_ms: int, vad_level: int):
		self.sample_rate = sample_rate
		self.frame_duration_ms = frame_duration_ms
		self.frame_bytes = int(sample_rate * frame_duration_ms / 1000) * 2  # int16 mono
		self.vad = webrtcvad.Vad(vad_level)
		self.trailing_silence_frames_needed = int(TRAILING_SILENCE_MS / frame_duration_ms)
		self.max_frames_per_segment = int(MAX_SEGMENT_SECONDS * 1000 / frame_duration_ms)

	def stream_segments(self, stop_event: threading.Event):
		"""
		Generator that yields PCM16 mono bytes for each detected speech segment.
		Blocks reading from microphone using sounddevice RawInputStream.
		"""
		current_segment = bytearray()
		silence_run = 0
		frames_in_segment = 0
		
		with sd.RawInputStream(
			samplerate=self.sample_rate,
			channels=CHANNELS,
			dtype='int16',
			blocksize=int(self.sample_rate * self.frame_duration_ms / 1000),
		) as stream:
			while not stop_event.is_set():
				data, overflowed = stream.read(int(self.sample_rate * self.frame_duration_ms / 1000))
				if overflowed:
					# Drop frame on overflow
					continue
				frame_bytes = bytes(data)
				if len(frame_bytes) != self.frame_bytes:
					continue
				is_speech = self.vad.is_speech(frame_bytes, self.sample_rate)
				if is_speech:
					current_segment.extend(frame_bytes)
					frames_in_segment += 1
					silence_run = 0
					# If segment grows too long, flush early
					if frames_in_segment >= self.max_frames_per_segment:
						if current_segment:
							yield bytes(current_segment)
						current_segment = bytearray()
						frames_in_segment = 0
						silence_run = 0
				else:
					if frames_in_segment > 0:
						silence_run += 1
						# If enough trailing silence, flush segment
						if silence_run >= self.trailing_silence_frames_needed:
							if current_segment:
								yield bytes(current_segment)
							current_segment = bytearray()
							frames_in_segment = 0
							silence_run = 0
				# Allow UI to breathe
				time.sleep(0)
		# Flush any remaining audio at stop
		if current_segment:
			yield bytes(current_segment)


def pcm16_mono_to_wav_bytes(pcm_bytes: bytes, sample_rate: int) -> bytes:
	buf = io.BytesIO()
	with wave.open(buf, 'wb') as wf:
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(sample_rate)
		wf.writeframes(pcm_bytes)
	return buf.getvalue()


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
	if _openai_client is None:
		raise RuntimeError("OpenAI client not initialized. Check installation and API key.")
	last_exc = None
	for model_name in ("gpt-4o-transcribe", "whisper-1"):
		try:
			bio = io.BytesIO(wav_bytes)
			bio.name = "segment.wav"
			resp = _openai_client.audio.transcriptions.create(
				model=model_name,
				file=bio,
			)
			text = getattr(resp, "text", None)
			if not text and isinstance(resp, dict):
				text = resp.get("text")
			return text or ""
		except Exception as e:
			last_exc = e
			continue
	raise last_exc if last_exc else RuntimeError("Transcription failed")


def type_text_via_clipboard(text: str):
	if not text:
		return
	pyperclip.copy(text)
	keyboard.press_and_release('ctrl+v')


def load_config_hotkey() -> str:
	try:
		with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
			cfg = json.load(f)
			hk = cfg.get('hotkey')
			if isinstance(hk, str) and hk.strip():
				return hk
	except Exception:
		pass
	return DEFAULT_HOTKEY


def save_config_hotkey(hotkey: str):
	try:
		cfg = { 'hotkey': hotkey }
		with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
			json.dump(cfg, f, ensure_ascii=False, indent=2)
	except Exception:
		pass


class VoiceDictApp:
	def __init__(self):
		self.listening = False
		self.stop_event = threading.Event()
		self.segments_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
		self.transcriber_thread: threading.Thread | None = None
		self.capturer_thread: threading.Thread | None = None
		self.hotkey_handle = None
		self.hotkey_string = load_config_hotkey()
		# tray removed
		# tray removed

		# UI
		import tkinter as tk
		self.tk = tk
		self.root = tk.Tk()
		self.root.title(APP_TITLE)
		self.root.attributes('-topmost', True)
		self.root.resizable(False, False)
		self.status_var = tk.StringVar(value='Idle')
		self.last_text_var = tk.StringVar(value='')

		self.header = tk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 12, "bold"))
		self.header.pack(padx=10, pady=(8, 2))
		self.status = tk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 10))
		self.status.pack(padx=10)
		self.last = tk.Label(self.root, textvariable=self.last_text_var, font=("Consolas", 9), wraplength=260, justify='left')
		self.last.pack(padx=10, pady=(6, 6))
		self.hint_var = tk.StringVar(value=f"Hotkey: {self.hotkey_string}")
		self.hint = tk.Label(self.root, textvariable=self.hint_var, font=("Segoe UI", 8))
		self.hint.pack(padx=10, pady=(0, 6))
		self.change_btn = tk.Button(self.root, text="Change Hotkey", command=self.begin_hotkey_capture)
		self.change_btn.pack(padx=10, pady=(0, 8))
		self.root.geometry("320x175")

		# Register hotkey
		self._register_hotkey(self.hotkey_string)

		# tray removed
		# tray removed

		# Graceful close
		self.root.protocol("WM_DELETE_WINDOW", self.on_close)

	def _set_status_ui(self, text: str):
		self.status_var.set(text)

	def set_status(self, text: str):
		try:
			self.root.after(0, self._set_status_ui, text)
		except Exception:
			pass

	def _set_last_text_ui(self, text: str):
		self.last_text_var.set(text)

	def set_last_text(self, text: str):
		preview = text.strip().replace('\n', ' ')
		if len(preview) > 120:
			preview = preview[:117] + '...'
		try:
			self.root.after(0, self._set_last_text_ui, preview)
		except Exception:
			pass

	def _set_hint_ui(self, text: str):
		self.hint_var.set(text)

	def update_hint(self):
		self.root.after(0, self._set_hint_ui, f"Hotkey: {self.hotkey_string}")

	def _register_hotkey(self, hotkey: str) -> bool:
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		try:
			self.hotkey_handle = keyboard.add_hotkey(hotkey, self.toggle_listening, suppress=False)
			return True
		except Exception as e:
			self.set_status(f"Hotkey failed: {e}")
			self.hotkey_handle = None
			return False

	def begin_hotkey_capture(self):
		# Temporarily remove current hotkey to avoid accidental toggles
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
				self.hotkey_handle = None
		except Exception:
			pass
		# Disable button and prompt
		try:
			self.change_btn.config(state='disabled')
		except Exception:
			pass
		self.set_status('Press the new hotkey (Esc to cancel)...')
		threading.Thread(target=self._hotkey_capture_thread, name='HotkeyCapture', daemon=True).start()

	def _hotkey_capture_thread(self):
		new_hotkey = None
		try:
			# Block until a combo is pressed; do not suppress
			new_hotkey = keyboard.read_hotkey(suppress=False)
		except Exception as e:
			self.set_status(f"Capture failed: {e}")
		finally:
			self.root.after(0, self._finish_hotkey_capture, new_hotkey)

	def _finish_hotkey_capture(self, new_hotkey: str | None):
		# Re-enable button
		try:
			self.change_btn.config(state='normal')
		except Exception:
			pass
		# Handle cancel
		if not new_hotkey or new_hotkey.lower() == 'esc':
			self.set_status('Hotkey change canceled')
			# Re-register previous hotkey
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			return
		# Try register new hotkey
		if self._register_hotkey(new_hotkey):
			self.hotkey_string = new_hotkey
			save_config_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status(f"Hotkey set: {self.hotkey_string}")
		else:
			# Failed: restore previous
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status('Failed to set hotkey')

	def toggle_listening(self):
		if self.listening:
			self.stop_listening()
		else:
			self.start_listening()

	def start_listening(self):
		if self.listening:
			return
		self.listening = True
		self.stop_event.clear()
		self.set_status('Listening...')
		play_start_beep()

		# Start transcriber worker
		self.transcriber_thread = threading.Thread(target=self._transcriber_loop, name='Transcriber', daemon=True)
		self.transcriber_thread.start()

		# Start audio capturer worker
		self.capturer_thread = threading.Thread(target=self._capture_loop, name='AudioCapture', daemon=True)
		self.capturer_thread.start()

	def stop_listening(self):
		if not self.listening:
			return
		self.listening = False
		self.stop_event.set()
		play_stop_beep()
		self.set_status('Stopping...')

		# Allow threads to drain
		time.sleep(0.05)
		self.set_status('Idle')

	def _capture_loop(self):
		segmenter = AudioSegmenter(SAMPLE_RATE, FRAME_DURATION_MS, VAD_AGGRESSIVENESS)
		try:
			for pcm_segment in segmenter.stream_segments(self.stop_event):
				if not self.listening:
					break
				wav_bytes = pcm16_mono_to_wav_bytes(pcm_segment, SAMPLE_RATE)
				try:
					self.segments_queue.put(wav_bytes, timeout=0.1)
				except queue.Full:
					# Drop if backlog
					pass
		except Exception as e:
			self.set_status(f"Audio error: {e}")

	def _transcriber_loop(self):
		while not self.stop_event.is_set() or not self.segments_queue.empty():
			try:
				wav_bytes = self.segments_queue.get(timeout=0.1)
			except queue.Empty:
				continue
			try:
				text = transcribe_wav_bytes(wav_bytes)
				if text:
					self.set_last_text(text)
					# Insert a trailing space to keep flowing
					type_text_via_clipboard(text + ' ')
			except Exception as e:
				self.set_status(f"Transcribe error: {e}")

	def _on_unmap(self, _event):
		# If minimized, hide to tray
		try:
			if self.root.state() == 'iconic':
				# tray removed
				pass
		except Exception:
			pass

	def _tray_show(self):
		try:
			self.root.deiconify()
			self.root.after(0, self.root.lift)
			# tray removed
		except Exception:
			pass

	def _tray_hide(self):
		try:
			self.root.withdraw()
			# tray removed
		except Exception:
			pass

	def _tray_exit(self):
		self.on_close()

	def on_close(self):
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		self.stop_event.set()
		self.listening = False
		# tray removed
		# tray removed
		self.root.after(50, self.root.destroy)


def main():
	api_key = os.environ.get('OPENAI_API_KEY', '')
	if not api_key:
		print("ERROR: OPENAI_API_KEY is not set. Create a .env or set env var.")
		# Proceed but likely to fail during transcription
	app = VoiceDictApp()
	print(f"{APP_TITLE} running. Global hotkey: {app.hotkey_string}")
	app.root.mainloop()


if __name__ == "__main__":
	main()

```

```python
import os
import sys
import threading
import queue
import time
import io
import wave
import json

import sounddevice as sd
import webrtcvad
import keyboard
import pyperclip

# Windows-only simple beeps
try:
	import winsound
	def play_start_beep():
		winsound.Beep(1200, 120)
	def play_stop_beep():
		winsound.Beep(600, 120)
except Exception:
	def play_start_beep():
		pass
	def play_stop_beep():
		pass

# Load OpenAI client
from dotenv import load_dotenv

APP_TITLE = "VoiceDict"
DEFAULT_HOTKEY = "windows+shift+i"  # Default global hotkey
SAMPLE_RATE = 16000
FRAME_DURATION_MS = 20  # 10/20/30ms supported by WebRTC VAD
CHANNELS = 1
VAD_AGGRESSIVENESS = 2  # 0-3, higher is more aggressive
MAX_SEGMENT_SECONDS = 15.0
TRAILING_SILENCE_MS = 600  # how much silence to consider end of utterance
# Determine base directory for resources/config
# - In a PyInstaller one-file build, use the directory of the executable
# - In dev (non-frozen), use the directory of this source file
if getattr(sys, 'frozen', False):
	BASE_DIR = os.path.dirname(sys.executable)
else:
	BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure .env and config.json exist next to the executable
ENV_PATH = os.path.join(BASE_DIR, ".env")
# Create .env if missing with empty key
try:
	if not os.path.exists(ENV_PATH):
		with open(ENV_PATH, 'w', encoding='utf-8') as _f:
			_f.write("OPENAI_API_KEY=\n")
except Exception:
	pass

# Load .env from the base directory (supports both dev and packaged exe)
load_dotenv(ENV_PATH)

# Config stored next to the executable (or next to this file in dev)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
try:
	if not os.path.exists(CONFIG_PATH):
		with open(CONFIG_PATH, 'w', encoding='utf-8') as _f:
			_f.write('{"hotkey": "' + DEFAULT_HOTKEY + '"}')
except Exception:
	pass

class AudioSegmenter:
	def __init__(self, sample_rate: int, frame_duration_ms: int, vad_level: int):
		self.sample_rate = sample_rate
		self.frame_duration_ms = frame_duration_ms
		self.frame_bytes = int(sample_rate * frame_duration_ms / 1000) * 2  # int16 mono
		self.vad = webrtcvad.Vad(vad_level)
		self.trailing_silence_frames_needed = int(TRAILING_SILENCE_MS / frame_duration_ms)
		self.max_frames_per_segment = int(MAX_SEGMENT_SECONDS * 1000 / frame_duration_ms)

	def stream_segments(self, stop_event: threading.Event):
		"""
		Generator that yields PCM16 mono bytes for each detected speech segment.
		Blocks reading from microphone using sounddevice RawInputStream.
		"""
		current_segment = bytearray()
		silence_run = 0
		frames_in_segment = 0
		
		with sd.RawInputStream(
			samplerate=self.sample_rate,
			channels=CHANNELS,
			dtype='int16',
			blocksize=int(self.sample_rate * self.frame_duration_ms / 1000),
		) as stream:
			while not stop_event.is_set():
				data, overflowed = stream.read(int(self.sample_rate * self.frame_duration_ms / 1000))
				if overflowed:
					# Drop frame on overflow
					continue
				frame_bytes = bytes(data)
				if len(frame_bytes) != self.frame_bytes:
					continue
				is_speech = self.vad.is_speech(frame_bytes, self.sample_rate)
				if is_speech:
					current_segment.extend(frame_bytes)
					frames_in_segment += 1
					silence_run = 0
					# If segment grows too long, flush early
					if frames_in_segment >= self.max_frames_per_segment:
						if current_segment:
							yield bytes(current_segment)
						current_segment = bytearray()
						frames_in_segment = 0
						silence_run = 0
				else:
					if frames_in_segment > 0:
						silence_run += 1
						# If enough trailing silence, flush segment
						if silence_run >= self.trailing_silence_frames_needed:
							if current_segment:
								yield bytes(current_segment)
							current_segment = bytearray()
							frames_in_segment = 0
							silence_run = 0
				# Allow UI to breathe
				time.sleep(0)
		# Flush any remaining audio at stop
		if current_segment:
			yield bytes(current_segment)


def pcm16_mono_to_wav_bytes(pcm_bytes: bytes, sample_rate: int) -> bytes:
	buf = io.BytesIO()
	with wave.open(buf, 'wb') as wf:
		wf.setnchannels(1)
		wf.setsampwidth(2)
		wf.setframerate(sample_rate)
		wf.writeframes(pcm_bytes)
	return buf.getvalue()


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
	if _openai_client is None:
		raise RuntimeError("OpenAI client not initialized. Check installation and API key.")
	last_exc = None
	for model_name in ("gpt-4o-transcribe", "whisper-1"):
		try:
			bio = io.BytesIO(wav_bytes)
			bio.name = "segment.wav"
			resp = _openai_client.audio.transcriptions.create(
				model=model_name,
				file=bio,
			)
			text = getattr(resp, "text", None)
			if not text and isinstance(resp, dict):
				text = resp.get("text")
			return text or ""
		except Exception as e:
			last_exc = e
			continue
	raise last_exc if last_exc else RuntimeError("Transcription failed")


def type_text_via_clipboard(text: str):
	if not text:
		return
	pyperclip.copy(text)
	keyboard.press_and_release('ctrl+v')


def load_config_hotkey() -> str:
	try:
		with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
			cfg = json.load(f)
			hk = cfg.get('hotkey')
			if isinstance(hk, str) and hk.strip():
				return hk
	except Exception:
		pass
	return DEFAULT_HOTKEY


def save_config_hotkey(hotkey: str):
	try:
		cfg = { 'hotkey': hotkey }
		with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
			json.dump(cfg, f, ensure_ascii=False, indent=2)
	except Exception:
		pass


class VoiceDictApp:
	def __init__(self):
		# Initialize OpenAI client after .env loaded
		global _openai_client
		try:
			from openai import OpenAI
			_openai_client = OpenAI()
		except Exception as _e:
			_openai_client = None
		self.listening = False
		self.stop_event = threading.Event()
		self.segments_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
		self.transcriber_thread: threading.Thread | None = None
		self.capturer_thread: threading.Thread | None = None
		self.hotkey_handle = None
		self.hotkey_string = load_config_hotkey()
		# tray removed
		# tray removed

		# UI
		import tkinter as tk
		self.tk = tk
		self.root = tk.Tk()
		self.root.title(APP_TITLE)
		self.root.attributes('-topmost', True)
		self.root.resizable(False, False)
		self.status_var = tk.StringVar(value='Idle')
		self.last_text_var = tk.StringVar(value='')

		self.header = tk.Label(self.root, text=APP_TITLE, font=("Segoe UI", 12, "bold"))
		self.header.pack(padx=10, pady=(8, 2))
		self.status = tk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 10))
		self.status.pack(padx=10)
		self.last = tk.Label(self.root, textvariable=self.last_text_var, font=("Consolas", 9), wraplength=260, justify='left')
		self.last.pack(padx=10, pady=(6, 6))
		self.hint_var = tk.StringVar(value=f"Hotkey: {self.hotkey_string}")
		self.hint = tk.Label(self.root, textvariable=self.hint_var, font=("Segoe UI", 8))
		self.hint.pack(padx=10, pady=(0, 6))
		self.change_btn = tk.Button(self.root, text="Change Hotkey", command=self.begin_hotkey_capture)
		self.change_btn.pack(padx=10, pady=(0, 8))
		self.root.geometry("320x175")

		# Register hotkey
		self._register_hotkey(self.hotkey_string)

		# tray removed
		# tray removed

		# Graceful close
		self.root.protocol("WM_DELETE_WINDOW", self.on_close)

	def _set_status_ui(self, text: str):
		self.status_var.set(text)

	def set_status(self, text: str):
		try:
			self.root.after(0, self._set_status_ui, text)
		except Exception:
			pass

	def _set_last_text_ui(self, text: str):
		self.last_text_var.set(text)

	def set_last_text(self, text: str):
		preview = text.strip().replace('\n', ' ')
		if len(preview) > 120:
			preview = preview[:117] + '...'
		try:
			self.root.after(0, self._set_last_text_ui, preview)
		except Exception:
			pass

	def _set_hint_ui(self, text: str):
		self.hint_var.set(text)

	def update_hint(self):
		self.root.after(0, self._set_hint_ui, f"Hotkey: {self.hotkey_string}")

	def _register_hotkey(self, hotkey: str) -> bool:
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		try:
			self.hotkey_handle = keyboard.add_hotkey(hotkey, self.toggle_listening, suppress=False)
			return True
		except Exception as e:
			self.set_status(f"Hotkey failed: {e}")
			self.hotkey_handle = None
			return False

	def begin_hotkey_capture(self):
		# Temporarily remove current hotkey to avoid accidental toggles
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
				self.hotkey_handle = None
		except Exception:
			pass
		# Disable button and prompt
		try:
			self.change_btn.config(state='disabled')
		except Exception:
			pass
		self.set_status('Press the new hotkey (Esc to cancel)...')
		threading.Thread(target=self._hotkey_capture_thread, name='HotkeyCapture', daemon=True).start()

	def _hotkey_capture_thread(self):
		new_hotkey = None
		try:
			# Block until a combo is pressed; do not suppress
			new_hotkey = keyboard.read_hotkey(suppress=False)
		except Exception as e:
			self.set_status(f"Capture failed: {e}")
		finally:
			self.root.after(0, self._finish_hotkey_capture, new_hotkey)

	def _finish_hotkey_capture(self, new_hotkey: str | None):
		# Re-enable button
		try:
			self.change_btn.config(state='normal')
		except Exception:
			pass
		# Handle cancel
		if not new_hotkey or new_hotkey.lower() == 'esc':
			self.set_status('Hotkey change canceled')
			# Re-register previous hotkey
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			return
		# Try register new hotkey
		if self._register_hotkey(new_hotkey):
			self.hotkey_string = new_hotkey
			save_config_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status(f"Hotkey set: {self.hotkey_string}")
		else:
			# Failed: restore previous
			self._register_hotkey(self.hotkey_string)
			self.update_hint()
			self.set_status('Failed to set hotkey')

	def toggle_listening(self):
		if self.listening:
			self.stop_listening()
		else:
			self.start_listening()

	def start_listening(self):
		if self.listening:
			return
		self.listening = True
		self.stop_event.clear()
		self.set_status('Listening...')
		play_start_beep()

		# Start transcriber worker
		self.transcriber_thread = threading.Thread(target=self._transcriber_loop, name='Transcriber', daemon=True)
		self.transcriber_thread.start()

		# Start audio capturer worker
		self.capturer_thread = threading.Thread(target=self._capture_loop, name='AudioCapture', daemon=True)
		self.capturer_thread.start()

	def stop_listening(self):
		if not self.listening:
			return
		self.listening = False
		self.stop_event.set()
		play_stop_beep()
		self.set_status('Stopping...')

		# Allow threads to drain
		time.sleep(0.05)
		self.set_status('Idle')

	def _capture_loop(self):
		segmenter = AudioSegmenter(SAMPLE_RATE, FRAME_DURATION_MS, VAD_AGGRESSIVENESS)
		try:
			for pcm_segment in segmenter.stream_segments(self.stop_event):
				if not self.listening:
					break
				wav_bytes = pcm16_mono_to_wav_bytes(pcm_segment, SAMPLE_RATE)
				try:
					self.segments_queue.put(wav_bytes, timeout=0.1)
				except queue.Full:
					# Drop if backlog
					pass
		except Exception as e:
			self.set_status(f"Audio error: {e}")

	def _transcriber_loop(self):
		while not self.stop_event.is_set() or not self.segments_queue.empty():
			try:
				wav_bytes = self.segments_queue.get(timeout=0.1)
			except queue.Empty:
				continue
			try:
				text = transcribe_wav_bytes(wav_bytes)
				if text:
					self.set_last_text(text)
					# Insert a trailing space to keep flowing
					type_text_via_clipboard(text + ' ')
			except Exception as e:
				self.set_status(f"Transcribe error: {e}")

	def _on_unmap(self, _event):
		# If minimized, hide to tray
		try:
			if self.root.state() == 'iconic':
				# tray removed
				pass
		except Exception:
			pass

	def _tray_show(self):
		try:
			self.root.deiconify()
			self.root.after(0, self.root.lift)
			# tray removed
		except Exception:
			pass

	def _tray_hide(self):
		try:
			self.root.withdraw()
			# tray removed
		except Exception:
			pass

	def _tray_exit(self):
		self.on_close()

	def on_close(self):
		try:
			if self.hotkey_handle is not None:
				keyboard.remove_hotkey(self.hotkey_handle)
		except Exception:
			pass
		self.stop_event.set()
		self.listening = False
		# tray removed
		# tray removed
		self.root.after(50, self.root.destroy)


def main():
	api_key = os.environ.get('OPENAI_API_KEY', '')
	if not api_key:
		print("ERROR: OPENAI_API_KEY is not set. Create a .env or set env var.")
		# Proceed but likely to fail during transcription
	app = VoiceDictApp()
	print(f"{APP_TITLE} running. Global hotkey: {app.hotkey_string}")
	app.root.mainloop()


if __name__ == "__main__":
	main()

```

- Теперь при старте приложение гарантированно создает файлы рядом с `VoiceDict.exe`:
  - `.env` с содержимым:
    ```
    OPENAI_API_KEY=
    ```
  - `config.json` с дефолтным хоткеем, если его нет.
- Ключ читается из `.env` в директории запуска перед инициализацией клиента OpenAI.
- Готовый exe: `dist\VoiceDict.exe`. Все зависимости упакованы внутрь.

Как пользоваться и раздать:
1) Скопируйте `dist\VoiceDict.exe` любому пользователю.
2) При первом запуске рядом создастся `.env` и `config.json`.
3) Откройте `.env` и вставьте свой ключ:
```
OPENAI_API_KEY=sk-...
```
4) Перезапустите приложение. Хоткей можно менять в UI, изменения сохранятся в `config.json`.

Примечания:
- На некоторых системах может потребоваться запуск от администратора для глобальных хоткеев.
- Возможное предупреждение SmartScreen — нажмите “More info” → “Run anyway”.

```plaintext
    OPENAI_API_KEY=
```

```plaintext
OPENAI_API_KEY=sk-...
```

---

**User**

Работает, теперь настрой полностью флоу билда в гитхабе.
Для тригера билда будет тег начиная с `v0.1.0`. 
Должна быть страница с релизами где можно скачать бинарники. В самом репо не должно быть бинарников

---

**Cursor**

I'll add a GitHub Actions workflow to build the Windows exe on tag pushes and upload it to the Releases page as an asset, ensuring no binaries are committed to the repo.

```yaml
name: release

on:
  push:
    tags:
      - 'v*'

permissions:
  contents: write

jobs:
  build-windows:
    runs-on: windows-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install build dependencies
        run: |
          python -m pip install --upgrade pip setuptools wheel
          python -m pip install -r requirements.txt
          python -m pip install pyinstaller webrtcvad-wheels

      - name: Build one-file Windows exe
        run: |
          pyinstaller --noconfirm --clean --onefile --windowed --name VoiceDict --additional-hooks-dir hooks voice_dict.py

      - name: Prepare zip artifact
        shell: pwsh
        run: |
          New-Item -ItemType Directory -Force -Path releases | Out-Null
          Compress-Archive -Path dist/VoiceDict.exe -DestinationPath releases/VoiceDict-Windows-x64.zip -Force

      - name: Upload CI artifact (zip)
        uses: actions/upload-artifact@v4
        with:
          name: VoiceDict-Windows-x64
          path: releases/VoiceDict-Windows-x64.zip

      - name: Create GitHub Release and upload asset
        uses: softprops/action-gh-release@v2
        with:
          files: releases/VoiceDict-Windows-x64.zip
          name: ${{ github.ref_name }}
          tag_name: ${{ github.ref_name }}
          draft: false
          prerelease: false

```

I'll add `*.spec` to `.gitignore` to ensure PyInstaller's spec file isn't accidentally committed, then confirm the workflow steps and share usage instructions for tag-triggered releases.

```ignore
# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class

# C extensions / debug
*.so
*.pyd
*.pdb

# Distribution / packaging
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
pip-wheel-metadata/
share/python-wheels/
*.egg-info/
.installed.cfg
*.egg
MANIFEST

# Virtual environments
.venv/
venv/
ENV/
env/
.venv.bak/
__pypackages__/

# Installer logs
pip-log.txt
pip-delete-this-directory.txt

# Unit test / coverage
htmlcov/
.tox/
.nox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
*.py,cover
.hypothesis/
.pytest_cache/

# Translations
*.mo
*.pot

# OS-specific
Thumbs.db
ehthumbs.db
Desktop.ini
$RECYCLE.BIN/
.DS_Store

# IDE / Editor
.vscode/
.idea/
*.code-workspace

# Environment files
.env
.env.*
!.env.example

# Logs
*.log
logs/
log/

# Audio/temp artifacts
temp_audio/
*.wav
*.mp3
*.flac

dist/
build/
*.spec

```

- Добавил GitHub Actions workflow для релизов: `.github/workflows/release.yml`
- Триггер: push тега, начинающегося с `v` (например, `v0.1.0`)
- Сборка: Windows (windows-latest), Python 3.12, `pyinstaller` с нашим кастомным хуком
- Результат: публикуется релиз с ассетом `VoiceDict-Windows-x64.zip` (внутри `VoiceDict.exe`)
- В репозиторий бинарники не коммитятся (`dist/`, `build/`, `*.spec` в `.gitignore`)

Как выпускать релиз:
1) Создайте и запушьте тег:
```
git tag v0.1.0
git push origin v0.1.0
```
2) Вкладка Releases в GitHub заполнится автоматически. С качаемым ассетом `VoiceDict-Windows-x64.zip`.

Если нужно изменять название файла/добавлять дополнительные сборки (например, x86), скажите — добавлю матрицу сборок.

```plaintext
git tag v0.1.0
git push origin v0.1.0
```

---

**User**

Обнови @README.md на английском, добавь ссылку на билды

---

**Cursor**

I'll update `README.md` to add a concise "Download" section with links to the GitHub Releases (latest and all), and clarify first-run behavior for the exe. Then I'll tweak the "Quick start" heading to indicate it's for source usage.

```markdown
## VoiceDict

Simple app for voice typing using the OpenAI API.
Type in your favorite IDE (e.g., Cursor).

![UI](./docs/ui.png)

### Download
- Windows x64: get the latest build from the Releases page: [Latest Release](../../releases/latest) · [All Releases](../../releases)
- Unzip and run `VoiceDict.exe`. On first launch, `.env` and `config.json` will be created next to the executable. Put your API key into `.env`:
```
OPENAI_API_KEY=sk-...
```

### Overview
- Voice typing into any Windows app window.
- Automatically detects input language.
- Global hotkey: Win+Shift+I — start/stop (can be changed in the UI).
- Transcribes EN/RU, pastes into the active input field.

### Quick start (from source)
- Set your `OPENAI_API_KEY` in a `.env` file (rename `.env.example` to `.env`).
- Run `run.bat` (creates venv and installs dependencies).

Hands‑free speech → text into any focused app (e.g., Cursor chat).

- Global hotkey: Win+Shift+I (toggle start/stop)
- Short sound effects on start/stop
- Transcribes English, Russian, and code reliably (OpenAI API)
- Types into the focused input of any opened app.
- Compact always‑on‑top UI shows status and last transcript.
- Emits short sound effects on start/stop.

### Requirements
- Windows 10+
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
### Notes
- Audio capture: 16 kHz mono, WebRTC VAD segments, up to ~15s per chunk
- Models: tries `gpt-4o-transcribe` first, falls back to `whisper-1`
- Typing: uses clipboard + Ctrl+V to preserve formatting and speed
- Beeps: Windows `winsound` (no external files needed)

### Troubleshooting
- API errors: verify `OPENAI_API_KEY` in `.env` and your account limits.
- Microphone busy: close other apps using the mic.

### Uninstall
- Deactivate and remove the virtual environment:
```bash
.\.venv\Scripts\deactivate
rmdir /s /q .venv
```

```

- Added a Download section to `README.md` with links to Latest and All Releases, and brief first-run instructions for the exe.
- Clarified that the Quick start section is for running from source.

---

**User**

Смотри, ты описал релиз, но ты не описал сам билд. Приложение не билдится. Я хочу, чтобы в GitHub, короче, был список билдов, чтобы можно было скачать ехе файл. 

---

**Cursor**

I'll add a CI workflow that builds on pushes/PRs and uploads artifacts, and update the release workflow to name assets with the tag. Then I'll summarize how to trigger both builds and where to download artifacts.

I'll create a CI workflow to build the exe on push/PR and upload it as an artifact, and update the release workflow to name assets with the tag so releases show downloadable builds.

```yaml
name: ci

on:
  push:
    branches:
      - '**'
  pull_request:

permissions:
  contents: read

jobs:
  build-windows:
    runs-on: windows-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install build dependencies
        run: |
          python -m pip install --upgrade pip setuptools wheel
          python -m pip install -r requirements.txt
          python -m pip install pyinstaller webrtcvad-wheels

      - name: Build one-file Windows exe
        run: |
          pyinstaller --noconfirm --clean --onefile --windowed --name VoiceDict --additional-hooks-dir hooks voice_dict.py

      - name: Package artifact (zip)
        shell: pwsh
        run: |
          New-Item -ItemType Directory -Force -Path releases | Out-Null
          $name = "VoiceDict-Windows-x64-${{ github.sha }}.zip"
          Compress-Archive -Path dist/VoiceDict.exe -DestinationPath ("releases/" + $name) -Force
          echo "ARTIFACT_NAME=$name" | Out-File -FilePath $env:GITHUB_ENV -Append

      - name: Upload build artifact
        uses: actions/upload-artifact@v4
        with:
          name: VoiceDict-Windows-x64
          path: releases/${{ env.ARTIFACT_NAME }}

```

```yaml
name: release

on:
  push:
    tags:
      - "v*"

permissions:
  contents: write

jobs:
  build-windows:
    runs-on: windows-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Install build dependencies
        run: |
          python -m pip install --upgrade pip setuptools wheel
          python -m pip install -r requirements.txt
          python -m pip install pyinstaller webrtcvad-wheels

      - name: Build one-file Windows exe
        run: |
          pyinstaller --noconfirm --clean --onefile --windowed --name VoiceDict --additional-hooks-dir hooks voice_dict.py

      - name: Prepare zip artifact
        shell: pwsh
        run: |
          New-Item -ItemType Directory -Force -Path releases | Out-Null
          $tag = "${{ github.ref_name }}"
          $zip = "VoiceDict-Windows-x64-" + $tag + ".zip"
          Compress-Archive -Path dist/VoiceDict.exe -DestinationPath ("releases/" + $zip) -Force
          echo "RELEASE_ZIP=$zip" | Out-File -FilePath $env:GITHUB_ENV -Append

      - name: Upload CI artifact (zip)
        uses: actions/upload-artifact@v4
        with:
          name: VoiceDict-Windows-x64
          path: releases/${{ env.RELEASE_ZIP }}

      - name: Create GitHub Release and upload asset
        uses: softprops/action-gh-release@v2
        with:
          files: releases/${{ env.RELEASE_ZIP }}
          name: ${{ github.ref_name }}
          tag_name: ${{ github.ref_name }}
          draft: false
          prerelease: false

```

- Я добавил два рабочих пайплайна в GitHub Actions:
  - CI билд на каждый push/PR: `.github/workflows/ci.yml`
    - Сборка `VoiceDict.exe` на `windows-latest`
    - Загружает zip-артефакт в “Actions → Job → Artifacts” (для быстрых проверок без релиза)
  - Релизный билд по тегу `v*`: `.github/workflows/release.yml`
    - Сборка `VoiceDict.exe`
    - Публикация релиза и загрузка ассета `VoiceDict-Windows-x64-<tag>.zip` на страницу Releases

Как скачать:
- Последний релиз: go to Releases → Latest Release. Файл будет называться вроде `VoiceDict-Windows-x64-v0.1.0.zip`.
- Артефакты CI: вкладка Actions → нужный workflow run → Artifacts.

Триггеры:
- Для релиза: создайте и запушьте тег, например:
```
git tag v0.1.0
git push origin v0.1.0
```
- Для CI: любой push/PR в репозиторий.

В репозитории бинарники не хранятся: `dist/`, `build/`, `*.spec` игнорируются.

```plaintext
git tag v0.1.0
git push origin v0.1.0
```

