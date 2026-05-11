from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import statistics
import time
from urllib.parse import urlencode

import websockets

SARVAM_STT_WS_URL = "wss://api.sarvam.ai/speech-to-text/ws"
SARVAM_TTS_WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"
DEFAULT_STT_MODEL = "saarika:v2.5"
DEFAULT_TTS_MODEL = "bulbul:v3-beta"
DEFAULT_SAMPLE_RATE = 16000

LANGUAGE_TEXT = {
    "hi-IN": "नमस्ते, दिल्ली के लिए एक छोटी यात्रा योजना बताइए।",
    "te-IN": "నమస్తే, హైదరాబాద్ కోసం ఒక చిన్న ప్రయాణ ప్రణాళిక చెప్పండి.",
    "ta-IN": "வணக்கம், சென்னை பயணத்திற்கு ஒரு சிறிய திட்டம் சொல்லுங்கள்.",
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Sarvam streaming STT/TTS latency.")
    parser.add_argument("--language", choices=["hi-IN", "te-IN", "ta-IN"])
    parser.add_argument("--text")
    parser.add_argument("--all", action="store_true", help="Probe hi-IN, te-IN, and ta-IN.")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise SystemExit("SARVAM_API_KEY must be set")

    languages = list(LANGUAGE_TEXT) if args.all else [args.language or "hi-IN"]
    rows = []
    for language in languages:
        text = args.text or LANGUAGE_TEXT[language]
        row = await probe_language(
            api_key=api_key,
            language=language,
            text=text,
            sample_rate=args.sample_rate,
            tts_model=args.tts_model,
        )
        rows.append(row)
        print("ROW_JSON " + json.dumps(row, ensure_ascii=False), flush=True)

    summary = {
        "tts_first_chunk_p50_ms": statistics.median([row["tts_first_chunk_ms"] for row in rows]),
        "stt_first_partial_p50_ms": statistics.median(
            [row["stt_first_partial_ms"] for row in rows]
        ),
        "rows": rows,
    }
    print("SUMMARY_JSON " + json.dumps(summary, ensure_ascii=False), flush=True)


async def probe_language(
    *,
    api_key: str,
    language: str,
    text: str,
    sample_rate: int,
    tts_model: str,
) -> dict[str, object]:
    tts_result = await probe_tts(
        api_key=api_key,
        language=language,
        text=text,
        sample_rate=sample_rate,
        model=tts_model,
    )
    stt_result = await probe_stt(
        api_key=api_key,
        language=language,
        audio=tts_result["audio"],
        sample_rate=sample_rate,
    )
    return {
        "language": language,
        "tts_model": tts_model,
        "tts_first_chunk_ms": tts_result["first_chunk_ms"],
        "stt_first_partial_ms": stt_result["first_partial_ms"],
        "stt_transcript": stt_result["transcript"],
    }


async def probe_tts(
    *, api_key: str, language: str, text: str, sample_rate: int, model: str
) -> dict[str, object]:
    started = time.perf_counter()
    first_chunk_ms: int | None = None
    chunks: list[bytes] = []
    speaker = "aditya" if model.startswith("bulbul:v3") else "anushka"

    async with websockets.connect(
        build_tts_ws_url(model),
        extra_headers={"api-subscription-key": api_key},
        ping_interval=20,
        ping_timeout=20,
    ) as connection:
        await connection.send(
            json.dumps(
                {
                    "type": "config",
                    "data": {
                        "model": model,
                        "target_language_code": language,
                        "speaker": speaker,
                        "speech_sample_rate": str(sample_rate),
                        "output_audio_codec": "linear16",
                        "pace": 1.0,
                        "enable_preprocessing": True,
                        "min_buffer_size": 50,
                        "max_chunk_length": 150,
                    },
                }
            )
        )
        await connection.send(json.dumps({"type": "text", "data": {"text": text}}))
        await connection.send(json.dumps({"type": "flush"}))
        async with asyncio.timeout(15):
            async for raw in connection:
                message = parse_tts_message(raw)
                if message["error"]:
                    raise RuntimeError(message["error"])
                if message["audio"]:
                    if first_chunk_ms is None:
                        first_chunk_ms = elapsed_ms(started)
                    audio = message["audio"]
                    if isinstance(audio, bytes):
                        chunks.append(audio)
                if message["final"]:
                    break

    return {"first_chunk_ms": first_chunk_ms or 0, "audio": b"".join(chunks)}


async def probe_stt(
    *, api_key: str, language: str, audio: object, sample_rate: int
) -> dict[str, object]:
    if not isinstance(audio, bytes) or not audio:
        return {"first_partial_ms": 0, "transcript": ""}

    started = time.perf_counter()
    async with websockets.connect(
        build_stt_ws_url(language=language, sample_rate=sample_rate),
        extra_headers={"api-subscription-key": api_key},
        ping_interval=20,
        ping_timeout=20,
    ) as connection:
        await connection.send(
            json.dumps(
                {
                    "audio": {
                        "data": base64.b64encode(audio).decode("ascii"),
                        "sample_rate": str(sample_rate),
                        "encoding": "audio/wav",
                    }
                }
            )
        )
        await connection.send(json.dumps({"type": "flush"}))
        async with asyncio.timeout(15):
            async for raw in connection:
                message = parse_stt_message(raw)
                if message["error"]:
                    raise RuntimeError(message["error"])
                transcript = message["transcript"]
                if transcript:
                    return {
                        "first_partial_ms": elapsed_ms(started),
                        "transcript": transcript,
                    }

    return {"first_partial_ms": 0, "transcript": ""}


def build_tts_ws_url(model: str) -> str:
    return f"{SARVAM_TTS_WS_URL}?{urlencode({'model': model, 'send_completion_event': 'true'})}"


def build_stt_ws_url(*, language: str, sample_rate: int) -> str:
    return (
        f"{SARVAM_STT_WS_URL}?"
        f"{
            urlencode(
                {
                    'language-code': language,
                    'model': DEFAULT_STT_MODEL,
                    'sample_rate': str(sample_rate),
                    'input_audio_codec': 'pcm_s16le',
                    'high_vad_sensitivity': 'true',
                    'flush_signal': 'true',
                    'vad_signals': 'false',
                }
            )
        }"
    )


def parse_tts_message(raw_message: str | bytes) -> dict[str, object]:
    payload = json.loads(raw_message)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"audio": b"", "final": False, "error": None}
    if payload.get("type") == "error":
        return {"audio": b"", "final": False, "error": str(data.get("message") or payload)}
    if payload.get("type") == "event" and data.get("event_type") == "final":
        return {"audio": b"", "final": True, "error": None}
    if payload.get("type") == "audio":
        audio = data.get("audio")
        if isinstance(audio, str):
            return {"audio": base64.b64decode(audio), "final": False, "error": None}
    return {"audio": b"", "final": False, "error": None}


def parse_stt_message(raw_message: str | bytes) -> dict[str, object]:
    payload = json.loads(raw_message)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"transcript": "", "error": None}
    if payload.get("type") == "error" or "error" in data:
        return {"transcript": "", "error": str(data.get("error") or data.get("message") or payload)}
    transcript = data.get("transcript")
    return {
        "transcript": transcript.strip() if isinstance(transcript, str) else "",
        "error": None,
    }


def elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


if __name__ == "__main__":
    asyncio.run(main())
