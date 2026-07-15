from pathlib import Path


REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"
RUNPOD_INPUT = (
    Path(__file__).resolve().parents[2]
    / "runpod_flash_extractor"
    / "requirements.custom-image.in"
)
SPACY_PIN = "spacy==3.8.14"
MODEL_PIN = (
    "en_core_web_sm @ "
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
    "#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
)


def _active_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_canonical_images_share_exact_spacy_and_model_pins() -> None:
    backend = _active_lines(REQUIREMENTS)
    runpod = _active_lines(RUNPOD_INPUT)

    assert backend.count(SPACY_PIN) == 1
    assert backend.count(MODEL_PIN) == 1
    assert runpod.count(SPACY_PIN) == 1
    assert runpod.count(MODEL_PIN) == 1
