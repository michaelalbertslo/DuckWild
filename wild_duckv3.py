#!/usr/bin/env python3
from __future__ import annotations

"""
Transmit Format:

2026-01-23 23:14:43,254 [INFO] TX bytes=197 raw=b'{"species":"990ae9dd-7a59-4344-afcb-1b7b21368000;mammalia;
primates;hominidae;homo;sapiens;human","confidence":0.8297,
"image":"capture_20260123_231331_939295.jpg","ts":"2026-01-23T23:14:43.254122Z"}'

"""

import json
import logging
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

# ---- radio stack ----
from duck import Duck
from packet import DuckType, Topic, UnknownData

# need to change this to make a dynamic DUID for each duck
TARGET_DUID = 8331  # .DUID

# ---- PIR / camera / detection config ----
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

CAMERA_CLI = Path("/home/micha/DuckWild/DuckWild/camera_cli.py")
PIPELINE_DIR = Path("/home/micha/DuckWild/DuckWild/species-pipeline")
DETECT_CLI = Path("/home/micha/DuckWild/DuckWild/detect.py")

IMAGE_DIR = Path("/home/micha/DuckWild/DuckWild/captured_images")
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

MOTION_PIN = 4  # BCM
# make this dynamic based on GPS ? A parameter --GPS to turn all that on
DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.50

TICK_HZ = 1.0  # run loop will call tick() ~1x/sec

# If you want to rate-limit captures later, you can set a minimum
# interval between captures (in seconds). For now, capture everything.
MIN_CAPTURE_INTERVAL = 0.5

RADIO_MAX_PAYLOAD = 229  # bytes, payload only


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
        # compact separators; no ASCII-escaping to keep UTF-8 small
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    rounded_conf = round(float(conf), 4)

    # 1) Try full keys
    full = {"species": species, "confidence": rounded_conf, "image": image_name, "ts": ts_iso}
    b = enc(full)
    if len(b) <= max_bytes:
        return b, full

    # 2) Try compact keys
    compact = {"s": species, "c": rounded_conf, "i": image_name, "t": ts_iso}
    b = enc(compact)
    if len(b) <= max_bytes:
        return b, compact

    # 3) Trim species progressively until it fits
    s = species
    # Try a few trims quickly, then per-char if needed
    for cut in (160, 120, 100, 80, 64, 48, 40, 32, 24, 16):
        if len(s) > cut:
            s = s[:cut]
        b = enc({**compact, "s": s})
        if len(b) <= max_bytes:
            return b, {"s": s, "c": rounded_conf, "i": image_name, "t": ts_iso}

    # Final per-char trim
    while len(s) > 1:
        s = s[:-1]
        b = enc({**compact, "s": s})
        if len(b) <= max_bytes:
            return b, {"s": s, "c": rounded_conf, "i": image_name, "t": ts_iso}

    # 4) Minimal: species hash (stable 12 hex chars)
    sid = hashlib.sha1(species.encode("utf-8")).hexdigest()[:12]
    minimal = {"sid": sid, "c": rounded_conf, "i": image_name, "t": ts_iso}
    b = enc(minimal)
    # This should always fit, but assert safety
    if len(b) > max_bytes:
        # as ultra fallback, drop image name
        minimal.pop("i", None)
        b = enc(minimal)
    return b, minimal


# ---------- small helpers ----------
def _timestamped_image_path() -> Path:
    # Use microseconds to avoid name collisions in motion bursts
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return IMAGE_DIR / f"capture_{stamp}.jpg"


def _speciesnet_json_for_image(image_path: Path) -> Path:
    # Unique predictions JSON per run to avoid resume logic
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return PIPELINE_DIR / f"speciesnet_{image_path.stem}_{stamp}.json"


def _run_camera_cli_capture(output_path: Path) -> None:
    """Capture a photo immediately (GPIO handled by FSM)."""
    cmd = [sys.executable, str(CAMERA_CLI), "photo", "-o", str(output_path)]
    logging.info("Running camera CLI: %s", " ".join(cmd))
    # Prevent indefinite hangs if camera stack wedges
    subprocess.run(cmd, check=True, timeout=20)


def _run_speciesnet_cli(image_path: Path) -> Path:
    """Run SpeciesNet once for a single image; return the JSON path."""
    predictions_json = _speciesnet_json_for_image(image_path)

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
    # Prevent indefinite hangs if model run wedges
    subprocess.run(cmd, check=True, cwd=str(PIPELINE_DIR), timeout=15 * 60)
    return predictions_json


def _parse_species_from_json(image_path: Path, json_path: Path) -> Tuple[str, float]:
    if not json_path.exists():
        raise RuntimeError(f"Predictions JSON not found: {json_path}")
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    preds = data.get("predictions", [])
    if not preds:
        raise RuntimeError("No predictions found in JSON")

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


# ---------- Duck with decoupled capture/inference ----------
class WildDuck(Duck):
    """
    Decoupled design:
      - PIR ISR enqueues motion timestamps
      - Capture worker pulls motion, takes photos, and enqueues image paths
      - Inference worker pulls image paths, runs SpeciesNet + radio send
      - tick() remains non-blocking for receive

    Added robustness:
      - Hardened RX handling so decode failures don't wedge the loop
      - Watchdog restarts dead workers and drains queues if stuck
      - Timeouts on camera/model subprocesses
    """

    def __init__(self, duid: int, target_duid: int = TARGET_DUID):
        super().__init__(DuckType.UNKNOWN, duid, 1)
        self._target_duid = target_duid
        self._tick_counter = 0
        self._alive = True

        # Queues
        self._motion_q: "queue.Queue[float]" = queue.Queue()  # timestamps from PIR
        self._image_q: "queue.Queue[Path]" = queue.Queue()  # captured image paths

        # --- Health / watchdog ---
        self._bad_rx = 0
        self._last_tick_ts = time.time()
        self._last_motion_ts = 0.0
        self._last_capture_done_ts = 0.0
        self._last_infer_done_ts = 0.0

        # Stuck detection thresholds (tune as needed)
        self._capture_stuck_s = 30.0  # camera should never take > ~30s
        self._infer_stuck_s = 10 * 60.0  # inference can be slow; give it time

        # Workers
        self._capture_thread = threading.Thread(
            target=self._capture_worker_loop, name="capture-worker", daemon=True
        )
        self._infer_thread = threading.Thread(
            target=self._inference_worker_loop, name="infer-worker", daemon=True
        )

        # Capture pacing
        self._last_capture_ts = 0.0

        self._setup_gpio()
        self._capture_thread.start()
        self._infer_thread.start()

        logging.info("WildDuck ready (duid=%s, target_dduid=%s)", duid, target_duid)

    # ---- GPIO ----
    def _setup_gpio(self) -> None:
        if GPIO is None:
            raise RuntimeError("RPi.GPIO not available on this system.")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(MOTION_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.add_event_detect(MOTION_PIN, GPIO.RISING, callback=self._on_motion, bouncetime=500)
        logging.info("Configured PIR on BCM pin %d", MOTION_PIN)

    def _on_motion(self, channel: int):
        t = time.time()
        self._last_motion_ts = t
        logging.info("Motion detected on pin %d", channel)
        try:
            self._motion_q.put_nowait(t)
        except queue.Full:
            # Unbounded by default, but keep guard
            logging.warning("Motion queue full; dropping timestamp at %f", t)

    # ---- Duck tick / receive ----
    def tick(self):
        """Non-blocking; called by Duck.run()."""
        self._tick_counter += 1
        self._last_tick_ts = time.time()

        # ---- RX poll (hardened) ----
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
            # If Duck internals occasionally bubble up decode failures, count and continue
            self._bad_rx += 1
            logging.warning("Receive poll failed (bad_rx=%d): %r", self._bad_rx, e)

        # ---- Watchdog / recovery ----
        self._watchdog()

        if self._tick_counter % int(max(1, TICK_HZ * 10)) == 0:
            logging.debug(
                "tick: motion_q=%d image_q=%d bad_rx=%d cap_alive=%s inf_alive=%s",
                self._motion_q.qsize(),
                self._image_q.qsize(),
                self._bad_rx,
                self._capture_thread.is_alive(),
                self._infer_thread.is_alive(),
            )

    def _handle_rx(self, pkt) -> None:
        logging.info("RX: %r", pkt)
        # Add any relay/forward rules here if needed

    def _watchdog(self) -> None:
        """Recover from stuck/dead worker threads so PIR can't 'hang' the system."""
        now = time.time()

        # 1) Restart worker threads if they died
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

        # 2) Detect "motion happening but capture not completing"
        if self._last_motion_ts and (now - self._last_motion_ts) < 10.0:
            if self._last_capture_done_ts and (now - self._last_capture_done_ts) > self._capture_stuck_s:
                logging.error(
                    "Capture appears stuck: last_capture_done=%.1fs ago (motion seen %.1fs ago)",
                    now - self._last_capture_done_ts,
                    now - self._last_motion_ts,
                )
                # Best-effort: clear backlog so we can recover
                self._drain_queue(self._motion_q, "motion_q")

        # 3) If inference is jammed for a long time, drop old images so capture can continue
        if self._last_infer_done_ts and (now - self._last_infer_done_ts) > self._infer_stuck_s:
            logging.error(
                "Inference appears stuck for %.1fs; draining image queue",
                now - self._last_infer_done_ts,
            )
            self._drain_queue(self._image_q, "image_q")

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

    # ---- Worker: capture (producer) ----
    def _capture_worker_loop(self):
        while self._alive:
            try:
                _ = self._motion_q.get(timeout=0.25)
            except queue.Empty:
                continue

            # Optional pacing between captures
            now = time.time()
            if MIN_CAPTURE_INTERVAL > 0.0:
                if (now - self._last_capture_ts) < MIN_CAPTURE_INTERVAL:
                    logging.debug("Skipping capture due to MIN_CAPTURE_INTERVAL")
                    continue

            image_path = _timestamped_image_path()
            try:
                logging.info("Capturing image -> %s", image_path)
                _run_camera_cli_capture(image_path)
                self._image_q.put(image_path)  # unbounded; don't lose frames
                self._last_capture_ts = now
                self._last_capture_done_ts = time.time()
            except subprocess.TimeoutExpired:
                logging.error("Camera capture timed out")
            except subprocess.CalledProcessError as e:
                logging.error("Camera capture failed: %s", e)
            except Exception:
                logging.exception("Unexpected error in capture worker")

    # ---- Worker: inference (consumer) ----
    def _inference_worker_loop(self):
        while self._alive:
            try:
                image_path = self._image_q.get(timeout=0.25)
            except queue.Empty:
                continue

            try:
                json_path = _run_speciesnet_cli(image_path)
                species, conf = _parse_species_from_json(image_path, json_path)

                logging.info("SpeciesNet result: %s (%.3f)", species, conf)
                print(f"Detected species: {species} (confidence={conf:.3f})")

                ts_iso = datetime.utcnow().isoformat() + "Z"
                payload_bytes, payload_obj = _build_wild_payload(
                    species=species,
                    conf=conf,
                    image_name=image_path.name,
                    ts_iso=ts_iso,
                    max_bytes=RADIO_MAX_PAYLOAD,
                )

                # Visibility: print the exact JSON we’re sending and its size
                wire_json_str = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False)
                logging.info("TX JSON (len=%d): %s", len(payload_bytes), wire_json_str)
                logging.info("TX bytes=%d raw=%r", len(payload_bytes), payload_bytes)
                logging.info("TX hex=%s", payload_bytes.hex())

                # Send the exact bytes that we sized to <=229
                self.send(
                    self._target_duid,
                    Topic.WILD,
                    UnknownData(payload_bytes),
                )
                logging.info("Sent WILD payload to dduid=%s", self._target_duid)
                self._last_infer_done_ts = time.time()

            except subprocess.TimeoutExpired:
                logging.error("SpeciesNet timed out for %s", image_path)
            except subprocess.CalledProcessError as e:
                logging.error("SpeciesNet subprocess error: %s", e)
            except Exception:
                logging.exception("Unexpected error in inference worker")

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
        if GPIO:
            try:
                GPIO.cleanup()
            except Exception:
                pass


# ---- example entrypoint wiring Duck.run() ----
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    # pick a stable DUID for this device
    my_duid = 1337
    duck = WildDuck(duid=my_duid, target_duid=TARGET_DUID)
    try:
        duck.run()
    except KeyboardInterrupt:
        logging.info("Shutting down")
    finally:
        duck.close()
