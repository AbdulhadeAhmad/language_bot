"""Blocking CPU inference adapters; executed only in a model service's worker thread."""

import wave

from app.voice_catalog import DEFAULT_VOICE_ID, VOICE_MODEL_FILES


def speech_chunks(text, limit=1000):
    text = " ".join(text.split())
    while len(text) > limit:
        boundary = text.rfind(" ", 0, limit + 1)
        if boundary < 1:
            boundary = limit
        yield text[:boundary]
        text = text[boundary:].lstrip()
    if text:
        yield text


def encode_mp3(path):
    import io

    import av

    output = io.BytesIO()
    with av.open(str(path)) as source, av.open(output, "w", format="mp3") as destination:
        stream = destination.add_stream("libmp3lame", rate=24000)
        stream.layout, stream.bit_rate = "mono", 64000
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=24000)
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted):
                    destination.mux(packet)
        for converted in resampler.resample(None):
            for packet in stream.encode(converted):
                destination.mux(packet)
        for packet in stream.encode(None):
            destination.mux(packet)
    return output.getvalue()


def load(kind, model_dir, threads):
    if kind == "asr":
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(model_dir / "asr"),
            device="cpu",
            compute_type="int8",
            cpu_threads=threads,
            num_workers=1,
            local_files_only=True,
        )

        def infer(job, output):
            segments, info = model.transcribe(job.input_path, language="de", beam_size=5)
            if info.duration > 120.5:
                raise ValueError("AudioDurationExceeded")
            text = "".join(segment.text for segment in segments).strip()
            if not text or len(text) > 10000:
                raise ValueError("TranscriptionLength")
            return text, None

        return infer
    if kind == "translation":
        import torch
        from transformers import MarianMTModel, MarianTokenizer

        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        tokenizer = MarianTokenizer.from_pretrained(
            model_dir / "translation", local_files_only=True
        )
        model = MarianMTModel.from_pretrained(
            model_dir / "translation", local_files_only=True
        ).eval()

        def infer(job, output):
            chunks, current = [], ""
            for word in job.input_text.split():
                proposed = f"{current} {word}".strip()
                if len(tokenizer.encode(proposed)) > 400:
                    if not current or len(tokenizer.encode(word)) > 400:
                        raise ValueError("TranslationTokenLimit")
                    chunks.append(current)
                    current = word
                else:
                    current = proposed
            if current:
                chunks.append(current)
            translated = []
            with torch.inference_mode():
                for chunk in chunks:
                    inputs = tokenizer(chunk, return_tensors="pt", truncation=False)
                    tokens = model.generate(**inputs, max_new_tokens=512, num_beams=4)
                    if tokens[0, -1].item() != model.config.eos_token_id:
                        raise ValueError("TranslationOutputLimit")
                    translated.append(tokenizer.decode(tokens[0], skip_special_tokens=True))
            text = " ".join(translated).strip()
            if not text or len(text) > 20000:
                raise ValueError("TranslationLength")
            return text, None

        return infer
    if kind == "tts":
        from piper import PiperVoice

        models = {
            voice_id: PiperVoice.load(str(model_dir / "tts" / filename), use_cuda=False)
            for voice_id, filename in VOICE_MODEL_FILES.items()
        }

        def infer(job, output):
            voice_id = job.voice_id or DEFAULT_VOICE_ID
            model = models.get(voice_id)
            if model is None:
                raise ValueError("UnknownVoice")
            with wave.open(str(output), "wb") as stream:
                for index, chunk in enumerate(speech_chunks(job.input_text)):
                    model.synthesize_wav(chunk, stream, set_wav_format=index == 0)
            with wave.open(str(output), "rb") as stream:
                if stream.getnframes() == 0:
                    raise ValueError("EmptyAudio")
            return None, str(output)

        return infer
    raise ValueError("Unknown model kind")
