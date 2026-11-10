"""Pinned German Piper voices used by the bot and model service."""

DEFAULT_VOICE_ID = "de_DE-thorsten-medium"

VOICE_CATALOG = (
    {
        "id": DEFAULT_VOICE_ID,
        "name": "Thorsten",
        "model": "de_DE-thorsten-medium.onnx",
        "repository_path": "de/de_DE/thorsten/medium",
    },
    {
        "id": "de_DE-eva_k-x_low",
        "name": "Eva K.",
        "model": "de_DE-eva_k-x_low.onnx",
        "repository_path": "de/de_DE/eva_k/x_low",
        "dataset_attribution": "M-AILABS Speech Dataset",
    },
    {
        "id": "de_DE-karlsson-low",
        "name": "Karlsson",
        "model": "de_DE-karlsson-low.onnx",
        "repository_path": "de/de_DE/karlsson/low",
        "dataset_attribution": "M-AILABS Speech Dataset",
    },
)

VOICE_MODEL_FILES = {voice["id"]: voice["model"] for voice in VOICE_CATALOG}
VOICE_NAMES = {voice["id"]: voice["name"] for voice in VOICE_CATALOG}


def voice_name(voice_id):
    return VOICE_NAMES.get(voice_id, voice_id)
