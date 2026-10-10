# CLAUDE.md

## YouTube work (yt-* skills in `.claude/skills/`)

- **Voice profile:** `scripts/voice-provisional.md` is the canonical, committed voice reference.
  The skills ask for `~/.claude/youtube/voice.md`. That path does not survive a cloud session, so
  read the repo file, and copy it there if a skill needs it:
  `mkdir -p ~/.claude/youtube && cp scripts/voice-provisional.md ~/.claude/youtube/voice.md`.
  Make voice edits in the repo file. The profile is PROVISIONAL (not learned from the creator's
  videos); say so whenever you rely on it.
- **Scripts:** live in `scripts/`. Never overwrite an earlier version; save a new `-vN` file.
- **Facts:** every documentary script carries a claims ledger (FACT / INTERPRETATION /
  UNRESOLVED, with a source). Don't invent numbers, quotes or sources.
- **No media generation** (images, voiceover, video) unless the user asks for it.
