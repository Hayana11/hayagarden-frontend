# Claude Code vision probe (P0)

- Claude Code version (VPS): `2.1.186`
- Local pin for comparison: `@anthropic-ai/claude-code@2.1.220`
- Invocation: `claude -p --input-format stream-json --output-format stream-json --verbose --include-partial-messages` with stdin kept open (resident style)

## Accepted user content shape

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {
        "type": "image",
        "source": {
          "type": "base64",
          "media_type": "image/png",
          "data": "<omitted>"
        }
      },
      {"type": "text", "text": "图片中央写了什么？只回答文字，不要解释。"}
    ]
  }
}
```

## Direct probe

- Marker in PNG: `HAYA_VISION_7319`
- Result: assistant/result text exactly `HAYA_VISION_7319`
- JSONL user event stores the same self-contained base64 image block

## Resume / forge-shaped probe

- Copied JSONL to a new session id (image block intact) and `--resume`
- Follow-up question about previous image → `HAYA_VISION_7319`
