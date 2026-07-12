from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.gemini_live_translate import GeminiLiveTranslator  # noqa: E402
from src.config import load_project_config  # noqa: E402
from src.logger import setup_logger  # noqa: E402


SAMPLE_RATE = 16000
CHUNK_MS = 100


def load_wav_as_pcm16(path: Path) -> bytes:
    audio, sample_rate = sf.read(str(path), always_2d=False)
    audio = np.asarray(audio)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)

    if np.issubdtype(audio.dtype, np.integer):
        max_value = np.iinfo(audio.dtype).max
        audio = audio.astype(np.float32) / max_value
    else:
        audio = audio.astype(np.float32)

    if sample_rate != SAMPLE_RATE:
        gcd = np.gcd(sample_rate, SAMPLE_RATE)
        audio = resample_poly(audio, SAMPLE_RATE // gcd, sample_rate // gcd).astype(np.float32)

    audio = np.clip(audio, -1.0, 1.0)
    return (audio * 32767).astype(np.int16).tobytes()


def chunk_bytes(data: bytes) -> list[bytes]:
    bytes_per_sample = 2
    frames_per_chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)
    chunk_size = frames_per_chunk * bytes_per_sample
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


def apply_network_config(config: dict) -> None:
    network_cfg = config.get("network", {})
    proxy_url = (network_cfg.get("proxy_url") or "").strip()
    no_proxy = (network_cfg.get("no_proxy") or "").strip()
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["ALL_PROXY"] = proxy_url
    if no_proxy:
        os.environ["NO_PROXY"] = no_proxy


async def run(path: Path) -> None:
    logger = setup_logger(True, "test-file")
    config = load_project_config(PROJECT_ROOT)
    apply_network_config(config)
    gemini_cfg = config.get("gemini", {})
    translator = GeminiLiveTranslator(
        model=gemini_cfg.get("model", "gemini-3.5-live-translate-preview"),
        target_language_code=gemini_cfg.get("target_language_code", "zh-Hans"),
        echo_target_language=bool(gemini_cfg.get("echo_target_language", False)),
        api_key=gemini_cfg.get("api_key") or None,
        api_key_env=gemini_cfg.get("api_key_env", "GEMINI_API_KEY"),
        sample_rate=SAMPLE_RATE,
        logger=logger,
        reconnect=False,
    )
    chunks = chunk_bytes(load_wav_as_pcm16(path))

    async def receive() -> None:
        async for event in translator.receive_translations():
            if event.error:
                print(f"[error] {event.error}")
            if event.input_text:
                print(f"[input] {event.input_text}")
            if event.output_text:
                print(f"[output] {event.output_text}")

    await translator.start()
    receive_task = asyncio.create_task(receive())
    try:
        for chunk in chunks:
            await translator.send_audio(chunk)
            await asyncio.sleep(CHUNK_MS / 1000)
        await asyncio.sleep(3)
    finally:
        receive_task.cancel()
        await asyncio.gather(receive_task, return_exceptions=True)
        await translator.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Gemini Live Translate with a local wav file.")
    parser.add_argument("wav_path", type=Path)
    args = parser.parse_args()

    if not args.wav_path.exists():
        raise SystemExit(f"Wav file not found: {args.wav_path}")

    asyncio.run(run(args.wav_path))


if __name__ == "__main__":
    main()
