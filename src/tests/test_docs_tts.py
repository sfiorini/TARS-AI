"""Docs regression tests (stories D1-D6): the TTS docs must match the code.

Docs-only guard: these tests read docs/*.md and assert the keys/behavior that
were added to the TTS stack (fishaudio_stream, fishaudio_latency, tts_volume,
piper lazy-load, current fallback chain, troubleshooting coverage) stay
documented. They fail whenever a doc page drifts from the code again.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


# --- D1 + D2: fishaudio_stream / fishaudio_latency in voice-pipeline.md -------

def test_voice_pipeline_documents_fishaudio_stream():
    doc = _read("voice-pipeline.md")
    assert "fishaudio_stream" in doc
    # default True, pcm/44100 streaming, ~1s slices, low time-to-first-audio
    assert "True" in doc
    assert "44100" in doc
    assert "pcm" in doc

def test_voice_pipeline_documents_fishaudio_latency():
    doc = _read("voice-pipeline.md")
    assert "fishaudio_latency" in doc
    for value in ("low", "balanced", "normal"):
        assert value in doc
    # the old hardcoded non-streaming body claim must be gone
    assert 'latency: "normal"' not in doc


def test_voice_pipeline_documents_stream_and_nonstream_paths():
    doc = _read("voice-pipeline.md")
    # True = HTTP streaming (pcm 44100, ~1s WAV slices); False = whole-clip wav
    assert "whole-clip" in doc or "whole clip" in doc


# --- D3: tts_volume -----------------------------------------------------------

def test_configuration_documents_tts_volume():
    doc = _read("configuration.md")
    assert "tts_volume" in doc
    assert "0.0" in doc and "2.0" in doc


def test_configuration_documents_speechify_streaming_keys():
    doc = _read("configuration.md")
    assert "speechify_stream" in doc
    assert "speechify_output_format" in doc
    assert "pcm_24000" in doc


def test_voice_pipeline_documents_tts_volume():
    doc = _read("voice-pipeline.md")
    assert "tts_volume" in doc


# --- D4: piper lazy-load wording ----------------------------------------------

def test_voice_pipeline_piper_lazy_load():
    doc = _read("voice-pipeline.md")
    assert "lazy" in doc
    # the old "kept resident, loaded once at startup" claim must be gone
    assert "loaded once at startup" not in doc
    assert "kept resident" not in doc


# --- Speechify streaming (S-M3-11) ---------------------------------------------

def test_voice_pipeline_documents_speechify_streaming():
    doc = _read("voice-pipeline.md")
    assert "speechify_stream" in doc
    assert "speechify_output_format" in doc
    assert "/v1/audio/stream" in doc
    assert "pcm_24000" in doc
    assert "mid-stream" in doc


# --- D5: server-mode chain -----------------------------------------------------

def test_server_mode_current_chain():
    doc = _read("server-mode.md")
    # current chain is fishaudio-first (configure_tars_client.sh), not elevenlabs
    assert "fishaudio" in doc
    assert "ttsoption = elevenlabs" not in doc


# --- D6: troubleshooting / web-ui / running ------------------------------------

def test_troubleshooting_has_tts_section():
    doc = _read("troubleshooting.md")
    assert "## TARS is silent" in doc or "## TTS" in doc
    # failover warnings with reasons
    assert "failing over" in doc
    # Fish 402 quota exhaustion + free-tier note
    assert "402" in doc
    assert "QuotaExhausted" in doc
    # missing API key skips
    assert "API_KEY" in doc
    # per-utterance deadline error
    assert "deadline" in doc
    # pw-play fallback
    assert "pw-play" in doc


def test_web_ui_mentions_tts_provider_selector():
    doc = _read("web-ui.md")
    assert "ttsoption" in doc
    assert "primary" in doc


def test_running_mentions_tts_failover_logs():
    doc = _read("running.md")
    assert "TTS" in doc
    assert "failing over" in doc or "failover" in doc


# --- docs rules ----------------------------------------------------------------

def test_no_local_machine_names_in_docs():
    import re

    for md in sorted(DOCS.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        # word boundary: 'mini' as a hostname, not the substring in 'minimum'
        assert not re.search(r"\bmini\b", text), f"{md.name} mentions local machine name 'mini'"
        assert "/opt/docker" not in text, f"{md.name} mentions /opt/docker"


def test_mkdocs_nav_matches_docs_files():
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    doc_files = {p.name for p in DOCS.glob("*.md")}
    nav_files = {
        line.strip().split()[-1]
        for line in mkdocs.splitlines()
        if line.strip().endswith(".md")
    }
    assert doc_files == nav_files, (
        f"mkdocs.yml nav out of sync with docs/: only-docs={doc_files - nav_files}, "
        f"only-nav={nav_files - doc_files}"
    )


# --- Piper off-loop streaming + speaker_id behaviour change (S-M4-11) --------

def test_voice_pipeline_documents_piper_offloop_and_speaker_fix():
    doc = _read("voice-pipeline.md")
    assert "event loop" in doc or "to_thread" in doc
    assert "SynthesisConfig" in doc
    assert "piper_multispeaker" in doc


# --- Measured on-robot numbers (S-M5-05) ---------------------------------------

def test_voice_pipeline_records_measured_piper_ttfa():
    doc = _read("voice-pipeline.md")
    # measured on the robot (Pi 5, piper-tts 1.8.0, sox 14.4.2), not estimated
    assert "0.280 s" in doc
    assert "1.183 s" in doc
    # the old first-audio estimate must be gone
    assert "0.3–0.6 s" not in doc
    # synthesis stays documented as off the event loop
    assert "event loop" in doc


def test_voice_pipeline_speechify_latency_distinguishes_server_ttfb_from_first_slice():
    doc = _read("voice-pipeline.md")
    # measured first playable chunk on the robot (warm session, pcm_24000)
    assert "0.929 s" in doc
    # the doc must honestly distinguish the server's first byte from the
    # first playable slice (the client coalesces raw PCM into ~1 s slices)
    assert "first byte" in doc
    assert "first playable" in doc
    assert "Server-Timing" in doc
    # the old misleading claims must be gone
    assert "time-to-first-audio ≈ **0.1 s**" not in doc
    assert "starts speaking in ≈ 0.1 s" not in doc
    # verified output format recorded (24 kHz mono 16-bit)
    assert "sampwidth=2" in doc
