#!/usr/bin/env python3

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Tuple

try:
    import RPi.GPIO as GPIO   # same lib you use in the camera CLI
except ImportError:
    GPIO = None

# ---- PATHS: adjust these for your setup ----
# Path to your camera CLI script
CAMERA_CLI = Path("/home/micha/DuckWild/camera_cli.py")

# Path to your SpeciesNet wrapper (the detect.py you pasted)
DETECT_CLI = Path("/home/micha/DuckWild/species-pipeline/detect.py")

# Where detect.py writes its predictions JSON
PIPELINE_DIR = Path("/home/micha/DuckWild/species-pipeline")
PREDICTIONS_JSON = PIPELINE_DIR / "speciesnet_results_fsm.json"

# Directory for captured images
IMAGE_DIR = Path("/home/micha/DuckWild/captured_images")
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

# GPIO pin for PIR motion sensor (BCM numbering)
MOTION_PIN = 4   # match your hardware; your camera CLI defaults to 4

# SpeciesNet defaults (same as in your detect.py)
DEFAULT_COUNTRY = "USA"
DEFAULT_ADMIN1 = "CA"
DEFAULT_MIN_CONF = 0.50


def timestamped_image_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return IMAGE_DIR / f"capture_{stamp}.jpg"


def run_camera_cli_capture(output_path: Path) -> None:
    """
    Call your camera CLI to take a photo immediately.
    NOTE: no --wait-for-gpio here; GPIO is handled by the FSM.
    """
    cmd = [sys.executable, str(CAMERA_CLI), "photo", "-o", str(output_path)]
    logging.info("Running camera CLI: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def run_speciesnet_cli(image_path: Path) -> None:
    # Make sure we *don't* resume on old data
    if PREDICTIONS_JSON.exists():
        PREDICTIONS_JSON.unlink()

    cmd = [
        "uv", "run", "python",
        str(DETECT_CLI),
        str(image_path),
        "--country", DEFAULT_COUNTRY,
        "--admin1_region", DEFAULT_ADMIN1,
        "--min_conf", str(DEFAULT_MIN_CONF),
        "--json", str(PREDICTIONS_JSON),
    ]
    logging.info("Running SpeciesNet CLI: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(PIPELINE_DIR))



def parse_species_from_json(image_path: Path) -> Tuple[str, float]:
    """
    Read species + confidence from SpeciesNet JSON for this image.
    """
    if not PREDICTIONS_JSON.exists():
        raise RuntimeError(f"Predictions JSON not found: {PREDICTIONS_JSON}")

    with PREDICTIONS_JSON.open("r", encoding="utf-8") as f:
        data = json.load(f)

    preds = data.get("predictions", [])
    if not preds:
        raise RuntimeError("No predictions found in JSON")

    filename = image_path.name
    # Prefer predictions that match our file name
    candidates = [p for p in preds if Path(p.get("filepath", "")).name == filename]
    if not candidates:
        candidates = preds  # fallback: highest confidence overall

    best = max(
        candidates,
        key=lambda p: float(p.get("prediction_score") or 0.0),
    )
    species = best.get("prediction", "")
    conf = float(best.get("prediction_score") or 0.0)

    if not species:
        raise RuntimeError("Empty species in prediction")

    return species, conf


class State(Enum):
    IDLE = auto()
    PROCESSING = auto()
    ERROR = auto()


class DetectorFSM:
    def __init__(self):
        self.state = State.IDLE
        self._setup_gpio()

    def _setup_gpio(self):
        if GPIO is None:
            raise RuntimeError("RPi.GPIO not available; run on Pi with GPIO library.")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(MOTION_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.add_event_detect(
            MOTION_PIN, GPIO.RISING, callback=self._on_motion, bouncetime=500
        )
        logging.info("Configured PIR on BCM pin %d", MOTION_PIN)

    # ----------------- motion callback -----------------

    def _on_motion(self, channel: int):
        logging.info("Motion detected on pin %d", channel)
        # For now, do the work inline. If it gets slow, move this to a thread.
        if self.state == State.IDLE:
            self.handle_motion_event()
        else:
            logging.info("Ignoring motion because state=%s", self.state.name)

    # ----------------- main handler --------------------

    def handle_motion_event(self):
        try:
            self.state = State.PROCESSING

            # 1) Capture image
            image_path = timestamped_image_path()
            logging.info("Capturing image -> %s", image_path)
            run_camera_cli_capture(image_path)

            # 2) Run SpeciesNet and read species
            run_speciesnet_cli(image_path)
            species, conf = parse_species_from_json(image_path)

            # Log + print the result
            logging.info("SpeciesNet result: %s (%.3f)", species, conf)
            print(f"Detected species: {species} (confidence={conf:.3f})")

        except subprocess.CalledProcessError as e:
            logging.error("Subprocess error: %s", e)
            self.state = State.ERROR
        except Exception:
            logging.exception("Unexpected error in motion handler")
            self.state = State.ERROR
        else:
            self.state = State.IDLE

    # ----------------- main loop -----------------------

    def run(self):
        logging.info("FSM running. Waiting for motion events...")
        try:
            while True:
                # later you can also poll radio here
                time.sleep(0.1)
        except KeyboardInterrupt:
            logging.info("KeyboardInterrupt, shutting down")
        finally:
            if GPIO:
                GPIO.cleanup()
            logging.info("GPIO cleaned up. Bye.")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    fsm = DetectorFSM()
    fsm.run()


if __name__ == "__main__":
    main()
