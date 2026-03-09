import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from picamera2 import Picamera2
else:
    from sys_modules import picamera2  # isort:skip
    from picamera2 import Picamera2  # isort:skip

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover - requires Raspberry Pi hardware
    GPIO = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a still photo or record a video with Picamera2 (fast path)."
    )
    parser.add_argument(
        "mode",
        choices=["photo", "video"],
        help="capture a still photo or record a video",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="file to write (default: timestamped file in current directory)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=5.0,
        help="video length in seconds (video mode only)",
    )
    parser.add_argument(
        "--wait-for-gpio",
        action="store_true",
        help=(
            "wait until the configured GPIO pin goes HIGH before capturing "
            "(photo mode only)"
        ),
    )
    parser.add_argument(
        "--gpio-pin",
        type=int,
        default=4,
        help="BCM pin number to monitor when --wait-for-gpio is used (default: 4)",
    )

    # ---- SPEED TUNING ----
    parser.add_argument(
        "--width",
        type=int,
        default=1024,
        help="capture width for photo mode (lower is faster). Default: 1024",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=768,
        help="capture height for photo mode (lower is faster). Default: 768",
    )
    parser.add_argument(
        "--warmup-ms",
        type=int,
        default=0,
        help="optional warmup delay in milliseconds after start (0 = instant). Try 150-300 if exposure is bad.",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help=(
            "keep the camera running after capture (useful if you run this as a long-lived process). "
            "If not set, camera stops after operation."
        ),
    )

    return parser.parse_args()


def timestamped_filename(extension: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return Path(f"capture_{stamp}.{extension}")


def _gpio_setup_input(pin: int) -> None:
    if GPIO is None:
        raise RuntimeError("RPi.GPIO is required for GPIO triggers. Install it and run on a Raspberry Pi.")
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)


def wait_for_gpio_high(pin: int) -> None:
    _gpio_setup_input(pin)
    print(f"Waiting for GPIO pin {pin} to go HIGH...")
    try:
        GPIO.wait_for_edge(pin, GPIO.RISING)
    finally:
        GPIO.cleanup(pin)
    print(f"GPIO pin {pin} went HIGH. Capturing photo...")

def camera_server_main(picam2: Picamera2, width: int, height: int, warmup_ms: int) -> None:
    # init once
    cfg = picam2.create_still_configuration(main={"size": (int(width), int(height))})
    picam2.configure(cfg)
    picam2.start()
    if warmup_ms > 0:
        time.sleep(warmup_ms / 1000.0)

    print("READY", flush=True)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line == "EXIT":
            print("BYE", flush=True)
            break
        if line.startswith("CAPTURE "):
            try:
                _, out = line.split(" ", 1)
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                picam2.capture_file(out)
                print("OK", flush=True)
            except Exception as e:
                print(f"ERR {e}", flush=True)
        else:
            print("ERR unknown_command", flush=True)


def capture_photo_fast(picam2: Picamera2, output_path: Path, *, width: int, height: int, warmup_ms: int) -> None:
    """
    Fast capture path:
    - configure once
    - start once
    - optional tiny warmup (ms)
    - capture immediately
    """
    cfg = picam2.create_still_configuration(
        main={"size": (int(width), int(height))}
    )
    picam2.configure(cfg)
    picam2.start()

    if warmup_ms > 0:
        time.sleep(warmup_ms / 1000.0)

    picam2.capture_file(str(output_path))


def record_video(picam2: Picamera2, output_path: Path, duration: float) -> None:
    picam2.configure(picam2.create_video_configuration())
    picam2.start_recording(str(output_path))
    time.sleep(duration)
    picam2.stop_recording()


def main() -> None:
    args = parse_args()
    picam2 = Picamera2()
    output = args.output

    try:
        if args.mode == "server":
            camera_server_main(picam2, args.width, args.height, args.warmup_ms)
            return
        if args.mode == "photo":
            if args.wait_for_gpio:
                # IMPORTANT: This is the "instant" usage: run once, block on GPIO, capture immediately.
                wait_for_gpio_high(args.gpio_pin)

            if output is None:
                output = timestamped_filename("jpg")

            capture_photo_fast(
                picam2,
                output,
                width=args.width,
                height=args.height,
                warmup_ms=args.warmup_ms,
            )

            if not args.keep_alive:
                picam2.stop()

        else:  # video mode
            if args.wait_for_gpio:
                raise ValueError("GPIO triggering is only supported in photo mode")
            if output is None:
                output = timestamped_filename("mp4")

            record_video(picam2, output, args.duration)

            if not args.keep_alive:
                picam2.stop()

    finally:
        try:
            picam2.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()