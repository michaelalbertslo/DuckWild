#!/usr/bin/env python3
from __future__ import annotations

"""
DuckWild - decoupled PIR -> capture -> inference -> TX

Hardening highlights:
- Idempotent GPIO edge registration via a safe wrapper that first remove_event_detect()
  (also protects LoRaRF/SX126x IRQ registration like GPIO 16)
- GPIO callbacks never raise
- Camera subprocess runs in its own process group; killpg on timeout
- SpeciesNet model stays resident in a dedicated worker thread (no per-image load/unload)
- Soft watchdog (drain queues / restart dead worker threads)
- Hard watchdog (os._exit) if tick/capture/inference truly wedges (pair with systemd Restart=always)

Run modes:
- test: keep current verbosity; keep captured images + JSON outputs on disk
- deploy: suppress most logs/prints; DO NOT write JSON; delete image (and any temp JSON) regardless of success/failure
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
from packet import DuckType, Topic, UnknownData

TARGET_DUID = 8331  # destination node

# ---- PIR / camera / detection config ----
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

# ---- IMPORTANT GPIO HARDENING ----
# This makes GPIO.add_event_detect "idempotent" for ANY pin.
# It prevents crashes like:
#   lgpio.error: 'bad event request'
# when a library (LoRaRF/SX126x) tries to add_event_detect() twice for the same pin (e.g., IRQ GPIO 16).
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
PROJECT_ROOT = HERE  # or HERE.parent if you want one level up

CAMERA_CLI = PROJECT_ROOT / "camera_cli.py"
PIPELINE_DIR = PROJECT_ROOT / "species-pipeline"
DETECT_CLI = PROJECT_ROOT / "detect.py"

IMAGE_DIR = PROJECT_ROOT / "captured_images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

MOTION_PIN = 4  # BCM
DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.50

TICK_HZ = 1.0
MIN_CAPTURE_INTERVAL = 0.5

RADIO_MAX_PAYLOAD = 229  # payload only (your app-level limit)

# subprocess timeouts (seconds)
CAMERA_TIMEOUT_S = 20
INFER_TIMEOUT_S = 15 * 60


def _default_speciesnet_model_name() -> str:
    """
    Resolve the model identifier/path for SpeciesNet.

    Priority:
      1) $SPECIESNET_MODEL
      2) If PIPELINE_DIR looks like a local model folder (contains info.json), use it
      3) speciesnet.DEFAULT_MODEL if available
      4) Hardcoded last-resort DEFAULT_MODEL string
    """
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
    """
    Return (payload_bytes, obj_used) guaranteed <= max_bytes.

    Strategy:
      1) Full keys
      2) Compact keys
      3) Trim species
      4) Minimal with species hash
    """
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
    """
    Run a command in its own process group and SIGKILL the whole group on timeout.
    Prevents lingering children (common cause of long-term wedging).
    """
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


def _run_camera_cli_capture(output_path: Path) -> None:
    cmd = [sys.executable, str(CAMERA_CLI), "photo", "-o", str(output_path)]
    logging.info("Running camera CLI: %s", " ".join(cmd))
    _run_with_timeout_killpg(cmd, cwd=None, timeout_s=CAMERA_TIMEOUT_S)


def _run_speciesnet_cli(image_path: Path, *, predictions_json: Path) -> None:
    """
    Fallback: runs detect.py as a subprocess (loads/unloads models per image).

    NOTE:
      - In deploy mode, we avoid JSON writing entirely by NOT using this fallback.
        If you need deploy mode to survive a resident-model failure, you'd have to allow
        temporary JSON or add a stdout parsing pathway in detect.py.
    """
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
    """
    Decoupled design:
      - PIR ISR enqueues motion timestamps
      - Capture worker pulls motion, takes photos, and enqueues image paths
      - Inference worker pulls image paths, runs SpeciesNet + radio send
      - SpeciesNet worker holds model open and executes per-image inference
      - tick() remains non-blocking for receive
    """

    def __init__(
        self,
        duid: int,
        target_duid: int = TARGET_DUID,
        *,
        mode: str = "test",
        speciesnet_model: Optional[str] = None,
    ):
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

        # SpeciesNet worker request queue (inference thread -> model thread)
        self._speciesnet_q: "queue.Queue[_SpeciesNetTask]" = queue.Queue(maxsize=8)

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
        self._infer_stuck_s = 60.0

        self._last_capture_ts = 0.0

        # SpeciesNet model state (lives in _speciesnet_thread)
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

        # Start threads
        self._speciesnet_thread.start()
        self._capture_thread.start()
        self._infer_thread.start()
        self._wd_thread.start()

        logging.info(
            "WildDuck ready (duid=%s, target_dduid=%s, mode=%s, speciesnet_model=%s)",
            duid,
            target_duid,
            self._mode,
            self._speciesnet_model_name,
        )

    # ---- GPIO ----
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
            GPIO.add_event_detect(MOTION_PIN, GPIO.RISING, callback=self._on_motion, bouncetime=500)
        except Exception:
            logging.exception("Failed to add_event_detect for PIR pin %d", MOTION_PIN)
            os._exit(120)

        logging.info("Configured PIR on BCM pin %d", MOTION_PIN)

    def _on_motion(self, channel: int):
        try:
            t = time.time()
            self._last_motion_ts = t
            logging.info("Motion detected on pin %d", channel)
            self._motion_q.put_nowait(t)
        except Exception:
            logging.exception("Exception in GPIO motion callback")

    # ---- Duck tick / receive ----
    def tick(self):
        self._tick_counter += 1
        self._last_tick_ts = time.time()

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

        self._soft_watchdog()

        if self._tick_counter % int(max(1, TICK_HZ * 10)) == 0:
            logging.debug(
                "tick: motion_q=%d image_q=%d bad_rx=%d cap_alive=%s inf_alive=%s sn_alive=%s cap_inprog=%s inf_inprog=%s",
                self._motion_q.qsize(),
                self._image_q.qsize(),
                self._bad_rx,
                self._capture_thread.is_alive(),
                self._infer_thread.is_alive(),
                self._speciesnet_thread.is_alive(),
                self._capture_in_progress,
                self._infer_in_progress,
            )

    def _handle_rx(self, pkt) -> None:
        logging.info("RX: %r", pkt)

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
        TICK_DEAD_S = 10.0
        CAPTURE_HARD_STUCK_S = CAMERA_TIMEOUT_S + 30
        INFER_HARD_STUCK_S = 75

        while self._alive:
            time.sleep(2.0)
            now = time.time()

            if now - self._last_tick_ts > TICK_DEAD_S:
                logging.critical("HARD WATCHDOG: tick stalled (%.1fs). Exiting.", now - self._last_tick_ts)
                os._exit(101)

            if self._capture_in_progress and (now - self._capture_start_ts) > CAPTURE_HARD_STUCK_S:
                logging.critical("HARD WATCHDOG: capture wedged for %.1fs. Exiting.", now - self._capture_start_ts)
                os._exit(102)

            if self._infer_in_progress and self._image_q.qsize() > 0 and (now - self._infer_start_ts) > INFER_HARD_STUCK_S:
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
                logging.debug("Skipping capture due to MIN_CAPTURE_INTERVAL")
                continue

            image_path = _timestamped_image_path()
            t0 = time.time()
            self._capture_in_progress = True
            self._capture_start_ts = t0
            try:
                logging.info("Capturing image -> %s", image_path)
                _run_camera_cli_capture(image_path)
                dt = time.time() - t0
                logging.info("Camera capture finished in %.2fs -> %s", dt, image_path)

                self._image_q.put(image_path)
                self._last_capture_ts = now
                self._last_capture_done_ts = time.time()
            except subprocess.TimeoutExpired:
                logging.error("Camera capture timed out (killed process group)")
            except subprocess.CalledProcessError as e:
                logging.error("Camera capture failed: %s", e)
            except Exception:
                logging.exception("Unexpected error in capture worker")
            finally:
                self._capture_in_progress = False

    # ---- Worker: SpeciesNet model (resident) ----
    def _speciesnet_worker_loop(self):
        """
        Dedicated worker that keeps SpeciesNet model loaded and serves inference requests.
        """
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
                    predictions_json=None,  # <- keep in-memory
                )
                if preds is None:
                    raise RuntimeError("SpeciesNet.predict returned None unexpectedly")
                task.result_q.put(_SpeciesNetResult(predictions=preds, error=None))
            except BaseException as e:
                task.result_q.put(_SpeciesNetResult(predictions=None, error=e))

    def _speciesnet_predict(self, image_path: Path) -> dict:
        """
        Get predictions from the resident SpeciesNet worker thread.

        Deploy mode requirement: do NOT write JSON.
        Therefore:
          - In deploy mode, if resident model init fails, we hard-exit to let systemd restart.
          - In test mode, we can fall back to detect.py and write JSON as before.
        """
        if not self._speciesnet_ready.is_set():
            if not self._speciesnet_ready.wait(timeout=INFER_TIMEOUT_S):
                logging.error("Resident SpeciesNet init timed out")
                if self._deploy_mode:
                    logging.critical("Deploy mode requires resident model; exiting for restart.")
                    os._exit(130)
                # test mode fallback
                json_path = _speciesnet_json_for_image(image_path)
                _run_speciesnet_cli(image_path, predictions_json=json_path)
                with json_path.open("r", encoding="utf-8") as f:
                    return json.load(f)

        if self._speciesnet_init_error is not None:
            if self._deploy_mode:
                logging.critical("Deploy mode requires resident model; init failed: %r", self._speciesnet_init_error)
                os._exit(131)
            # test mode fallback
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
                # Avoid silent degradation in deploy; restart.
                logging.critical("Deploy mode requires resident inference availability; exiting for restart.")
                os._exit(132)
            # test mode fallback
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

    # ---- Worker: inference + TX ----
    def _inference_worker_loop(self):
        while self._alive:
            try:
                image_path = self._image_q.get(timeout=0.25)
            except queue.Empty:
                continue

            t0 = time.time()
            self._infer_in_progress = True
            self._infer_start_ts = t0

            # In test mode we save JSON like before; in deploy we do not.
            json_path: Optional[Path] = _speciesnet_json_for_image(image_path) if self._test_mode else None

            try:
                predictions = self._speciesnet_predict(image_path)

                # TEST MODE: write JSON results to disk (as before)
                if self._test_mode and json_path is not None:
                    json_path.parent.mkdir(parents=True, exist_ok=True)
                    with json_path.open("w", encoding="utf-8") as f:
                        json.dump(predictions, f, ensure_ascii=False, indent=1)

                species, conf = _parse_species_from_predictions(image_path, predictions)

                logging.info("SpeciesNet result: %s (%.3f)", species, conf)
                if self._test_mode:
                    print(f"Detected species: {species} (confidence={conf:.3f})")

                # If below min confidence, skip TX
                if conf < float(DEFAULT_MIN_CONF):
                    logging.info(
                        "Below min confidence (%.3f < %.3f); skipping TX for %s",
                        conf,
                        float(DEFAULT_MIN_CONF),
                        image_path.name,
                    )
                    if self._test_mode:
                        print(f"Skipping TX: conf {conf:.3f} below min_conf {float(DEFAULT_MIN_CONF):.3f}")
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
                    wire_json_str = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False)
                    logging.info("TX JSON (len=%d): %s", len(payload_bytes), wire_json_str)
                    logging.info("TX bytes=%d raw=%r", len(payload_bytes), payload_bytes)
                    logging.info("TX hex=%s", payload_bytes.hex())

                self.send(self._target_duid, Topic.WILD, UnknownData(payload_bytes))
                logging.info("Sent WILD payload to dduid=%s", self._target_duid)

                dt = time.time() - t0
                logging.info("Inference finished in %.2fs for %s", dt, image_path.name)
                self._last_infer_done_ts = time.time()

            except subprocess.TimeoutExpired:
                logging.error("SpeciesNet timed out for %s", image_path)
            except subprocess.CalledProcessError as e:
                logging.error("SpeciesNet subprocess error: %s", e)
            except Exception:
                logging.exception("Unexpected error in inference worker")
            finally:
                # DEPLOY MODE: always delete image (and any json_path, if ever created) regardless of success/failure.
                if self._deploy_mode:
                    _safe_unlink(image_path)
                    _safe_unlink(json_path)
                self._infer_in_progress = False

    # ---- cleanup ----
    def close(self):
        self._alive = False

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
    p.add_argument(
        "--mode",
        choices=("test", "deploy"),
        default="test",
        help="test keeps images/JSON and prints/logs; deploy deletes artifacts and does not write JSON",
    )
    p.add_argument(
        "--speciesnet-model",
        default=None,
        help="SpeciesNet model identifier or local model folder. Defaults to $SPECIESNET_MODEL or speciesnet.DEFAULT_MODEL.",
    )
    p.add_argument("--duid", type=int, default=1337, help="Local Duck DUID")
    p.add_argument("--target-duid", type=int, default=TARGET_DUID, help="Destination Duck DUID")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()

    # In test mode, keep existing verbosity. In deploy, suppress info/debug spam.
    log_level = logging.INFO if args.mode == "test" else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    duck = WildDuck(
        duid=int(args.duid),
        target_duid=int(args.target_duid),
        mode=str(args.mode),
        speciesnet_model=args.speciesnet_model,
    )
    try:
        duck.run()
    except KeyboardInterrupt:
        logging.info("Shutting down")
    finally:
        duck.close()