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
