#!/usr/bin/env python3
from __future__ import annotations

"""
DuckWild - decoupled PIR -> capture -> inference -> TX

Changes:
- Use persistent camera_cli.py "server" subprocess (Picamera2 stays warm)
  to make motion->capture fast on Pi Zero 2 W.

BUGFIX:
- Radio TX must be single-threaded. Do NOT call self.send() from worker threads.
  Instead, enqueue TX requests and transmit from tick() (main Duck thread).
"""

import argparse
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# ---- radio stack ----
from duck import Duck
from packet import DuckType, Topic, UnknownData, Duids

# ---- PIR / camera / detection config ----
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

# ---- IMPORTANT GPIO HARDENING ----
if GPIO is not None:
    _GPIO_ORIG_ADD = GPIO.add_event_detect

    def _gpio_add_event_detect_safe(
        channel: int,
        edge: int,
        callback=None,
        bouncetime: int | None = None,
    ):
        try:
            GPIO.remove_event_detect(channel)
        except Exception:
            pass
        return _GPIO_ORIG_ADD(channel, edge, callback=callback, bouncetime=bouncetime)

    GPIO.add_event_detect = _gpio_add_event_detect_safe  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE

CAMERA_CLI = PROJECT_ROOT / "camera_cli.py"
PIPELINE_DIR = PROJECT_ROOT / "species-pipeline"
DETECT_CLI = PROJECT_ROOT / "detect.py"

IMAGE_DIR = PROJECT_ROOT / "captured_images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

MOTION_PIN = 4  # BCM
DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.30

TICK_HZ = 1.0
MIN_CAPTURE_INTERVAL = 3.0

RADIO_MAX_PAYLOAD = 229

# subprocess timeouts (seconds)
CAMERA_TIMEOUT_S = 35
INFER_TIMEOUT_S = 12 * 60

# Camera server tuning (Picamera2)
CAMERA_WIDTH = 1024
CAMERA_HEIGHT = 768
CAMERA_WARMUP_MS = 0  # try 150-300 if exposure is awful
CAMERA_SERVER_STARTUP_S = 20.0
CAMERA_CMD_TIMEOUT_S = 15.0  # time to wait for OK/ERR after CAPTURE

# DUIDs are 8 bytes (per packet.py)
PAPA_DUID: bytes = Duids.PAPA.value


# ----------------------------
# DUID normalization utilities
# ----------------------------
def duid_from_int(n: int) -> bytes:
    return int(n).to_bytes(8, "big", signed=False)


def duid_from_hex(s: str) -> bytes:
    s = (s or "").strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s.zfill(16))


def normalize_duid(v) -> bytes:
    if isinstance(v, bytes):
        if len(v) != 8:
            raise ValueError(f"DUID must be 8 bytes, got {len(v)}")
        return v
    if isinstance(v, int):
        return duid_from_int(v)
    if isinstance(v, str):
        b = duid_from_hex(v)
        if len(b) != 8:
            raise ValueError(f"DUID must be 8 bytes, got {len(b)}")
        return b
    raise TypeError(f"Unsupported DUID type: {type(v)}")


# ----------------------------
# Persistent camera_cli.py server
# ----------------------------
class CameraCliServer:
    """
    Runs camera_cli.py in a persistent "server" mode and sends CAPTURE commands.

    Protocol:
      server prints: READY
      parent sends:  CAPTURE <path>
      server replies: OK  or  ERR <message>
      parent sends: EXIT
      server replies: BYE
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.is_alive():
                return

            cmd = [
                sys.executable,
                "-u",
                str(CAMERA_CLI),
                "server",
                "--width",
                str(CAMERA_WIDTH),
                "--height",
                str(CAMERA_HEIGHT),
                "--warmup-ms",
                str(CAMERA_WARMUP_MS),
            ]
            logging.info("Starting camera server: %s", " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )

            assert self._proc.stdout is not None
            deadline = time.time() + CAMERA_SERVER_STARTUP_S
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if line:
                    line = line.strip()
                    if line == "READY":
                        logging.info("Camera server READY")
                        return
                    if line.startswith("FATAL"):
                        err = self._read_stderr_tail()
                        self.stop()
                        raise RuntimeError(f"Camera server failed: {line}\n{err}")
                    logging.info("Camera server: %s", line)
                else:
                    if self._proc.poll() is not None:
                        err = self._read_stderr_tail()
                        rc = self._proc.returncode
                        self.stop()
                        raise RuntimeError(f"Camera server exited during startup rc={rc}\n{err}")
                    time.sleep(0.05)

            err = self._read_stderr_tail()
            self.stop()
            raise TimeoutError(f"Camera server startup timeout (no READY)\n{err}")

    def _read_stderr_tail(self, max_chars: int = 4000) -> str:
        try:
            if not self._proc or self._proc.stderr is None:
                return ""
            data = self._proc.stderr.read()
            if not data:
                return ""
            return data[-max_chars:]
        except Exception:
            return ""

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None

        if not proc:
            return

        try:
            if proc.poll() is None:
                try:
                    assert proc.stdin is not None
                    proc.stdin.write("EXIT\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
        finally:
            try:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass

    def capture(self, output_path: Path) -> None:
        self.start()
        assert self._proc is not None
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None

        with self._lock:
            self._proc.stdin.write(f"CAPTURE {output_path}\n")
            self._proc.stdin.flush()

            deadline = time.time() + CAMERA_CMD_TIMEOUT_S
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if line:
                    line = line.strip()
                    if line == "OK":
                        return
                    if line.startswith("ERR"):
                        raise RuntimeError(line)
                    # ignore other log lines
                    continue

                if self._proc.poll() is not None:
                    err = self._read_stderr_tail()
                    rc = self._proc.returncode
                    raise RuntimeError(f"Camera server died rc={rc}\n{err}")

                time.sleep(0.01)

            raise TimeoutError("Camera CAPTURE command timed out")


# ----------------------------
# rest of your helpers
# ----------------------------
def _default_speciesnet_model_name() -> str:
    env = os.environ.get("SPECIESNET_MODEL")
    if env:
        return env
    if (PIPELINE_DIR / "info.json").exists():
        return str(PIPELINE_DIR)
    try:
        from speciesnet import DEFAULT_MODEL  # type: ignore

        return str(DEFAULT_MODEL)
    except Exception:
        return "kaggle:google/speciesnet/pyTorch/v4.0.2a/1"


def _build_wild_payload(
    species: str,
    conf: float,
    image_name: str,
    ts_iso: str,
    max_bytes: int = RADIO_MAX_PAYLOAD,
) -> tuple[bytes, dict]:
    import hashlib

    def enc(obj: dict) -> bytes:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    rounded_conf = round(float(conf), 4)

    full = {"species": species, "confidence": rounded_conf, "image": image_name, "ts": ts_iso}
    b = enc(full)
    if len(b) <= max_bytes:
        return b, full

    compact = {"s": species, "c": rounded_conf, "i": image_name, "t": ts_iso}
    b = enc(compact)
    if len(b) <= max_bytes:
        return b, compact

    s = species
    for cut in (160, 120, 100, 80, 64, 48, 40, 32, 24, 16):
        if len(s) > cut:
            s = s[:cut]
        b = enc({**compact, "s": s})
        if len(b) <= max_bytes:
            return b, {"s": s, "c": rounded_conf, "i": image_name, "t": ts_iso}

    while len(s) > 1:
        s = s[:-1]
        b = enc({**compact, "s": s})
        if len(b) <= max_bytes:
            return b, {"s": s, "c": rounded_conf, "i": image_name, "t": ts_iso}

    sid = hashlib.sha1(species.encode("utf-8")).hexdigest()[:12]
    minimal = {"sid": sid, "c": rounded_conf, "i": image_name, "t": ts_iso}
    b = enc(minimal)
    if len(b) > max_bytes:
        minimal.pop("i", None)
        b = enc(minimal)
    return b, minimal


def _timestamped_image_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return IMAGE_DIR / f"capture_{stamp}.jpg"


def _speciesnet_json_for_image(image_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return PIPELINE_DIR / f"speciesnet_{image_path.stem}_{stamp}.json"


def _safe_unlink(path: Optional[Path]) -> None:
    if not path:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception:
        logging.exception("Failed to delete file: %s", path)


def _run_with_timeout_killpg(cmd: list[str], *, cwd: str | None, timeout_s: float) -> None:
    proc = subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logging.error("Command timed out; killing process group: %s", " ".join(cmd))
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            logging.exception("Failed to kill process group")
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def _run_speciesnet_cli(image_path: Path, *, predictions_json: Path) -> None:
    cmd = [
        sys.executable,
        str(DETECT_CLI),
        str(image_path),
        "--country",
        DEFAULT_COUNTRY,
        "--admin1_region",
        DEFAULT_ADMIN1,
        "--min_conf",
        str(DEFAULT_MIN_CONF),
        "--json",
        str(predictions_json),
    ]
    logging.info("Running SpeciesNet CLI: %s", " ".join(cmd))
    _run_with_timeout_killpg(cmd, cwd=str(PIPELINE_DIR), timeout_s=INFER_TIMEOUT_S)


def _parse_species_from_predictions(image_path: Path, predictions_dict: dict) -> Tuple[str, float]:
    preds = predictions_dict.get("predictions", [])
    if not preds:
        raise RuntimeError("No predictions found in predictions dict")

    filename = image_path.name
    candidates = [p for p in preds if Path(p.get("filepath", "")).name == filename]
    if not candidates:
        candidates = preds

    best = max(candidates, key=lambda p: float(p.get("prediction_score") or 0.0))
    species = best.get("prediction", "")
    conf = float(best.get("prediction_score") or 0.0)
    if not species:
        raise RuntimeError("Empty species in prediction")
    return species, conf


@dataclass(frozen=True)
class _SpeciesNetTask:
    image_path: Path
    country: str
    admin1_region: str
    result_q: "queue.Queue[_SpeciesNetResult]"


@dataclass(frozen=True)
class _SpeciesNetResult:
    predictions: Optional[dict]
    error: Optional[BaseException]


class WildDuck(Duck):
    def __init__(
        self,
        duid: bytes,
        target_duid: bytes = PAPA_DUID,
        *,
        mode: str = "test",
        speciesnet_model: Optional[str] = None,
    ):
        duid = normalize_duid(duid)
        target_duid = normalize_duid(target_duid)

        super().__init__(DuckType.UNKNOWN, duid, 1)
        self._target_duid = target_duid
        self._tick_counter = 0
        self._alive = True

        mode_norm = (mode or "test").strip().lower()
        if mode_norm not in ("test", "deploy"):
            raise ValueError("mode must be one of: test, deploy")
        self._mode = mode_norm
        self._test_mode = mode_norm == "test"
        self._deploy_mode = mode_norm == "deploy"

        self._motion_q: "queue.Queue[float]" = queue.Queue()
        self._image_q: "queue.Queue[Path]" = queue.Queue()

        self._speciesnet_q: "queue.Queue[_SpeciesNetTask]" = queue.Queue(maxsize=8)

        # BUGFIX: all radio TX requests go through this queue and are sent from tick() only.
        self._tx_q: "queue.Queue[tuple[bytes, Topic, UnknownData]]" = queue.Queue(maxsize=32)

        self._bad_rx = 0
        self._last_tick_ts = time.time()
        self._last_motion_ts = 0.0
        self._last_capture_done_ts = 0.0
        self._last_infer_done_ts = 0.0

        self._capture_in_progress = False
        self._capture_start_ts = 0.0
        self._infer_in_progress = False
        self._infer_start_ts = 0.0

        self._capture_stuck_s = float(CAMERA_TIMEOUT_S + 10)
        self._infer_stuck_s = 10 * 60.0  # leave as-is

        self._last_capture_ts = 0.0

        # NEW: persistent camera_cli server
        self._camera = CameraCliServer()
        self._camera.start()

        self._speciesnet_model_name = speciesnet_model or _default_speciesnet_model_name()
        self._speciesnet_ready = threading.Event()
        self._speciesnet_init_error: Optional[BaseException] = None

        self._capture_thread = threading.Thread(
            target=self._capture_worker_loop, name="capture-worker", daemon=True
        )
        self._infer_thread = threading.Thread(
            target=self._inference_worker_loop, name="infer-worker", daemon=True
        )
        self._speciesnet_thread = threading.Thread(
            target=self._speciesnet_worker_loop, name="speciesnet-worker", daemon=True
        )
        self._wd_thread = threading.Thread(
            target=self._hard_watchdog_loop, name="hard-watchdog", daemon=True
        )

        self._setup_gpio()

        self._speciesnet_thread.start()
        self._capture_thread.start()
        self._infer_thread.start()
        self._wd_thread.start()

        logging.info(
            "WildDuck ready (duid=%s, target_dduid=%s, mode=%s, speciesnet_model=%s)",
            duid.hex(),
            target_duid.hex(),
            self._mode,
            self._speciesnet_model_name,
        )

    def _setup_gpio(self) -> None:
        if GPIO is None:
            raise RuntimeError("RPi.GPIO not available on this system.")

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(MOTION_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

        try:
            GPIO.remove_event_detect(MOTION_PIN)
        except Exception:
            pass

        try:
            GPIO.add_event_detect(MOTION_PIN, GPIO.RISING, callback=self._on_motion, bouncetime=300)
        except Exception:
            logging.exception("Failed to add_event_detect for PIR pin %d", MOTION_PIN)
            os._exit(120)

        logging.info("Configured PIR on BCM pin %d", MOTION_PIN)

    def _on_motion(self, channel: int):
        try:
            t = time.time()
            self._last_motion_ts = t
            self._motion_q.put_nowait(t)
        except Exception:
            logging.exception("Exception in GPIO motion callback")

    def tick(self):
        self._tick_counter += 1
        self._last_tick_ts = time.time()

        # RX poll (main thread)
        try:
            if hasattr(self, "try_receive_nowait"):
                pkt = self.try_receive_nowait()  # type: ignore[attr-defined]
                if pkt:
                    try:
                        self._handle_rx(pkt)
                    except Exception as e:
                        self._bad_rx += 1
                        logging.warning("RX handler error (bad_rx=%d): %r", self._bad_rx, e)
        except Exception as e:
            self._bad_rx += 1
            logging.warning("Receive poll failed (bad_rx=%d): %r", self._bad_rx, e)

        # BUGFIX: TX drain from main thread only (radio stack is often not thread-safe)
        for _ in range(3):  # limit per tick
            try:
                dduid, topic, data = self._tx_q.get_nowait()
            except queue.Empty:
                break
            try:
                self.send(dduid, topic, data)
            except Exception:
                logging.exception("Radio TX failed")

        self._soft_watchdog()

    def _handle_rx(self, pkt) -> None:
        logging.info(
            "RX: topic=%s sduid=%s dduid=%s muid=%s hop=%s",
            getattr(pkt, "topic", None),
            getattr(pkt, "sduid", b"").hex(),
            getattr(pkt, "dduid", b"").hex(),
            getattr(pkt, "muid", b"").hex(),
            getattr(pkt, "hop_count", None),
        )

    def _soft_watchdog(self) -> None:
        now = time.time()

        if not self._capture_thread.is_alive():
            logging.error("capture-worker died; restarting")
            self._capture_thread = threading.Thread(
                target=self._capture_worker_loop, name="capture-worker", daemon=True
            )
            self._capture_thread.start()

        if not self._infer_thread.is_alive():
            logging.error("infer-worker died; restarting")
            self._infer_thread = threading.Thread(
                target=self._inference_worker_loop, name="infer-worker", daemon=True
            )
            self._infer_thread.start()

        if not self._speciesnet_thread.is_alive():
            logging.error("speciesnet-worker died; restarting (model will reload)")
            self._speciesnet_ready.clear()
            self._speciesnet_init_error = None
            self._speciesnet_thread = threading.Thread(
                target=self._speciesnet_worker_loop, name="speciesnet-worker", daemon=True
            )
            self._speciesnet_thread.start()

        # keep camera server alive (restart only if dead)
        if not self._camera.is_alive():
            logging.error("camera server died; restarting")
            try:
                self._camera.start()
            except Exception:
                logging.exception("camera server restart failed")

        if self._motion_q.qsize() > 50:
            self._drain_queue(self._motion_q, "motion_q", max_items=1000)

        if self._image_q.qsize() > 20:
            self._drain_queue(self._image_q, "image_q", max_items=1000)

        if self._capture_in_progress and (now - self._capture_start_ts) > self._capture_stuck_s:
            logging.error("Capture stuck: in_progress for %.1fs", now - self._capture_start_ts)
            self._drain_queue(self._motion_q, "motion_q", max_items=1000)

        if self._infer_in_progress and (now - self._infer_start_ts) > self._infer_stuck_s:
            logging.error("Inference stuck: in_progress for %.1fs", now - self._infer_start_ts)
            self._drain_queue(self._image_q, "image_q", max_items=1000)

    def _hard_watchdog_loop(self):
        # (leave as-is; you can tune further)
        TICK_DEAD_S = 60.0 * 3
        CAPTURE_HARD_STUCK_S = CAMERA_TIMEOUT_S + 30
        INFER_HARD_STUCK_S = 15 * 60

        while self._alive:
            time.sleep(2.0)
            now = time.time()

            if now - self._last_tick_ts > TICK_DEAD_S:
                logging.critical("HARD WATCHDOG: tick stalled (%.1fs). Exiting.", now - self._last_tick_ts)
                os._exit(101)

            if self._capture_in_progress and (now - self._capture_start_ts) > CAPTURE_HARD_STUCK_S:
                logging.critical("HARD WATCHDOG: capture wedged for %.1fs. Exiting.", now - self._capture_start_ts)
                os._exit(102)

            if (
                self._infer_in_progress
                and self._image_q.qsize() > 0
                and (now - self._infer_start_ts) > INFER_HARD_STUCK_S
            ):
                logging.critical(
                    "HARD WATCHDOG: inference wedged for %.1fs with backlog=%d. Exiting.",
                    now - self._infer_start_ts,
                    self._image_q.qsize(),
                )
                os._exit(103)

    @staticmethod
    def _drain_queue(q: "queue.Queue", name: str, max_items: int = 1000) -> None:
        n = 0
        try:
            while n < max_items:
                q.get_nowait()
                n += 1
        except queue.Empty:
            pass
        if n:
            logging.warning("Drained %d items from %s", n, name)

    # ---- Worker: capture ----
    def _capture_worker_loop(self):
        while self._alive:
            try:
                _ = self._motion_q.get(timeout=0.25)
            except queue.Empty:
                continue

            now = time.time()
            if MIN_CAPTURE_INTERVAL > 0.0 and (now - self._last_capture_ts) < MIN_CAPTURE_INTERVAL:
                continue

            image_path = _timestamped_image_path()
            t0 = time.time()
            self._capture_in_progress = True
            self._capture_start_ts = t0
            try:
                logging.info("Capturing image -> %s", image_path)

                # fast capture via persistent camera server
                self._camera.capture(image_path)

                dt = time.time() - t0
                logging.info("Camera capture finished in %.2fs -> %s", dt, image_path)

                self._image_q.put(image_path)
                self._last_capture_ts = now
                self._last_capture_done_ts = time.time()
            except Exception:
                logging.exception("Camera capture failed")
            finally:
                self._capture_in_progress = False

    # ---- Worker: SpeciesNet model (resident) ----
    def _speciesnet_worker_loop(self):
        model = None
        try:
            if str(PIPELINE_DIR) not in sys.path:
                sys.path.insert(0, str(PIPELINE_DIR))

            from speciesnet.multiprocessing import SpeciesNet  # type: ignore

            if self._deploy_mode:
                try:
                    from absl import logging as absl_logging  # type: ignore

                    absl_logging.set_verbosity(absl_logging.ERROR)
                except Exception:
                    pass

            logging.info("Loading SpeciesNet model (resident): %s", self._speciesnet_model_name)
            model = SpeciesNet(self._speciesnet_model_name, multiprocessing=False)
            logging.info("SpeciesNet model loaded (resident)")
        except BaseException as e:
            self._speciesnet_init_error = e
            logging.exception("Failed to initialize resident SpeciesNet model")
        finally:
            self._speciesnet_ready.set()

        while self._alive:
            try:
                task = self._speciesnet_q.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                if model is None:
                    task.result_q.put(_SpeciesNetResult(predictions=None, error=self._speciesnet_init_error))
                    continue

                preds = model.predict(
                    filepaths=[str(task.image_path)],
                    country=task.country,
                    admin1_region=task.admin1_region,
                    run_mode="single_thread",
                    progress_bars=False,
                    predictions_json=None,
                )
                if preds is None:
                    raise RuntimeError("SpeciesNet.predict returned None unexpectedly")
                task.result_q.put(_SpeciesNetResult(predictions=preds, error=None))
            except BaseException as e:
                task.result_q.put(_SpeciesNetResult(predictions=None, error=e))

    def _speciesnet_predict(self, image_path: Path) -> dict:
        if not self._speciesnet_ready.is_set():
            if not self._speciesnet_ready.wait(timeout=INFER_TIMEOUT_S):
                logging.error("Resident SpeciesNet init timed out")
                if self._deploy_mode:
                    logging.critical("Deploy mode requires resident model; exiting for restart.")
                    os._exit(130)
                json_path = _speciesnet_json_for_image(image_path)
                _run_speciesnet_cli(image_path, predictions_json=json_path)
                with json_path.open("r", encoding="utf-8") as f:
                    return json.load(f)

        if self._speciesnet_init_error is not None:
            if self._deploy_mode:
                logging.critical("Deploy mode requires resident model; init failed: %r", self._speciesnet_init_error)
                os._exit(131)
            json_path = _speciesnet_json_for_image(image_path)
            _run_speciesnet_cli(image_path, predictions_json=json_path)
            with json_path.open("r", encoding="utf-8") as f:
                return json.load(f)

        result_q: "queue.Queue[_SpeciesNetResult]" = queue.Queue(maxsize=1)
        task = _SpeciesNetTask(
            image_path=image_path,
            country=DEFAULT_COUNTRY,
            admin1_region=DEFAULT_ADMIN1,
            result_q=result_q,
        )
        try:
            self._speciesnet_q.put(task, timeout=2.0)
        except queue.Full:
            logging.error("SpeciesNet queue full")
            if self._deploy_mode:
                logging.critical("Deploy mode requires resident inference availability; exiting for restart.")
                os._exit(132)
            json_path = _speciesnet_json_for_image(image_path)
            _run_speciesnet_cli(image_path, predictions_json=json_path)
            with json_path.open("r", encoding="utf-8") as f:
                return json.load(f)

        try:
            result = result_q.get(timeout=INFER_TIMEOUT_S)
        except queue.Empty as e:
            raise subprocess.TimeoutExpired(cmd="SpeciesNet(resident)", timeout=INFER_TIMEOUT_S) from e

        if result.error is not None:
            raise result.error
        assert result.predictions is not None
        return result.predictions

    # ---- Worker: inference + enqueue TX ----
    def _inference_worker_loop(self):
        while self._alive:
            try:
                image_path = self._image_q.get(timeout=0.25)
            except queue.Empty:
                continue

            t0 = time.time()
            self._infer_in_progress = True
            self._infer_start_ts = t0

            json_path: Optional[Path] = _speciesnet_json_for_image(image_path) if self._test_mode else None

            try:
                predictions = self._speciesnet_predict(image_path)

                if self._test_mode and json_path is not None:
                    json_path.parent.mkdir(parents=True, exist_ok=True)
                    with json_path.open("w", encoding="utf-8") as f:
                        json.dump(predictions, f, ensure_ascii=False, indent=1)

                species, conf = _parse_species_from_predictions(image_path, predictions)

                if conf < float(DEFAULT_MIN_CONF):
                    continue

                ts_iso = datetime.utcnow().isoformat() + "Z"
                payload_bytes, payload_obj = _build_wild_payload(
                    species=species,
                    conf=conf,
                    image_name=image_path.name,
                    ts_iso=ts_iso,
                    max_bytes=RADIO_MAX_PAYLOAD,
                )

                if self._test_mode:
                    logging.info("TX: %s", json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False))

                # BUGFIX: enqueue TX; main thread will call self.send()
                try:
                    self._tx_q.put(
                        (self._target_duid, Topic.WILD, UnknownData(payload_bytes)),
                        timeout=1.0,
                    )
                except queue.Full:
                    logging.error("TX queue full; dropping TX for %s", image_path.name)

                self._last_infer_done_ts = time.time()
                logging.info("Inference finished in %.2fs for %s", time.time() - t0, image_path.name)

            except Exception:
                logging.exception("Unexpected error in inference worker")
            finally:
                if self._deploy_mode:
                    _safe_unlink(image_path)
                    _safe_unlink(json_path)
                self._infer_in_progress = False

    def close(self):
        self._alive = False

        try:
            self._camera.stop()
        except Exception:
            pass

        try:
            self._capture_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._infer_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            self._speciesnet_thread.join(timeout=1.0)
        except Exception:
            pass

        if GPIO:
            try:
                try:
                    GPIO.remove_event_detect(MOTION_PIN)
                except Exception:
                    pass
                GPIO.cleanup()
            except Exception:
                pass


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DuckWild PIR -> capture -> SpeciesNet -> radio TX")
    p.add_argument("--mode", choices=("test", "deploy"), default="test")
    p.add_argument("--speciesnet-model", default=None)
    p.add_argument("--duid", default="0x0000000000000539")
    p.add_argument("--target-duid", default="0x0000000000000000")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()

    log_level = logging.INFO if args.mode == "test" else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    )

    duck = WildDuck(
        duid=normalize_duid(args.duid),
        target_duid=normalize_duid(args.target_duid),
        mode=str(args.mode),
        speciesnet_model=args.speciesnet_model,
    )
    try:
        duck.run()
    except KeyboardInterrupt:
        logging.info("Shutting down")
    finally:
        duck.close()
