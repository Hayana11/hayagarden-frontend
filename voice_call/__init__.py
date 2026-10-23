"""Voice calls: Fyodor rings, Haya answers, speech goes through the usual chat brain.

Pieces, each testable on its own:
- sentences: cut the streamed reply into speakable lines
- tts: ElevenLabs synthesis with a per-sentence content cache
- store: calls and their lines in SQLite
- brain: hand one utterance to the normal chat pipeline, stream the reply back
- service: the call state machine tying them together
"""
