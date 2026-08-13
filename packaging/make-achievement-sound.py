#!/usr/bin/env python3
"""Generate the achievement unlock sound.

Kept in the repository rather than only its output, so the sound can be
retuned by anybody who thinks it is wrong, and so nobody has to wonder where a
binary in the source tree came from. No dependencies — the standard library
writes WAV files perfectly well.

The design, since "a nice sound" is not a specification:

  * A rising major arpeggio, C5–E5–G5. Rising because the event is good news;
    a major triad because it resolves, and an unresolved notification is a
    question mark at the exact moment somebody has just succeeded.
  * A piano-ish voice rather than a bell. A bell is all inharmonic partials
    and a long shimmering tail, which is the "chimey" quality — harmonic
    partials with a firm attack read as an instrument being played instead.
  * A string pad underneath, an octave down, arriving slightly late and
    swelling. This is the part that gives it body: the arpeggio alone is
    thin, and the pad is what makes it feel like a moment rather than a beep.
  * The last note rings roughly twice as long as the first two, so the phrase
    lands rather than stopping.

Run from the repository root:

    python packaging/make-achievement-sound.py
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 44100
DURATION = 1.35
OUTPUT = Path("rose_gamelab/data/achievement.wav")

# C5, E5, G5 — a major triad, played upwards.
ARPEGGIO = (523.25, 659.25, 783.99)
NOTE_GAP = 0.11

#: The pad plays the same chord an octave below, where it supports rather
#: than competes with the melody.
PAD_CHORD = tuple(frequency / 2 for frequency in ARPEGGIO)
PAD_START = 0.06


def piano(frequency: float, t: float, *, decay: float) -> float:
    """A struck-string voice: harmonic partials, firm attack, honest decay.

    Deliberately harmonic where a bell would be inharmonic, which is most of
    the difference between "chime" and "instrument", and softly struck: the
    attack is slow enough to have no ping in it.
    """
    if t < 0:
        return 0.0

    # The attack is a few milliseconds, not instant: an instant one is a click.
    attack = min(1.0, t / 0.016)
    envelope = attack * math.exp(-decay * t)

    # Quiet upper partials that die faster than the fundamental. The high ones ringing on
    # after the fundamental is exactly what reads as "chime", so they are cut
    # back and made to die faster than the note itself.
    return envelope * (
        1.00 * math.sin(2 * math.pi * frequency * t)
        + 0.20 * math.sin(2 * math.pi * frequency * 2 * t) * math.exp(-4.0 * t)
        + 0.05 * math.sin(2 * math.pi * frequency * 3 * t) * math.exp(-7.0 * t)
    )


def strings(frequency: float, t: float) -> float:
    """A soft pad underneath: swells in, supports the notes, gets out of the way.

    No vibrato, and the two voices are only a cent and a half apart. Both of
    those were audible as tremolo — a 5 Hz vibrato is literally a wobble, and
    voices three cents apart beat against each other roughly six times a
    second, which is the same wobble by another route. What is left is enough
    detune for warmth and not enough to hear as movement.
    """
    if t < 0:
        return 0.0

    swell = min(1.0, t / 0.22)                 # arrives, rather than appears
    # Starts letting go while the last note is still ringing, so the pad never
    # outlives the phrase it is supporting.
    release = math.exp(-3.4 * max(0.0, t - 0.34))

    value = 0.0
    for detune in (0.9985, 1.0015):
        f = frequency * detune
        value += (
            1.00 * math.sin(2 * math.pi * f * t)
            + 0.30 * math.sin(2 * math.pi * f * 2 * t)
            + 0.10 * math.sin(2 * math.pi * f * 3 * t)
        )

    return swell * release * value * 0.5


def glow(frequency: float, t: float) -> float:
    """A soft layer laid over the top: pure tone, no edge, slow to arrive.

    Nothing but a fundamental — no partials at all — so it has no brightness
    of its own to add. Its whole job is to sit over the struck notes and blunt
    what is left of their attack, the way a pad under a melody rounds it off.
    """
    if t < 0:
        return 0.0

    swell = min(1.0, t / 0.13)
    return swell * math.exp(-2.6 * t) * math.sin(2 * math.pi * frequency * t)


def bass(frequency: float, t: float) -> float:
    """One low note landing with the last of the arpeggio.

    This replaces the octave of sparkle that used to sit on top. Sparkle above
    the melody is more chime; weight below it is the phrase arriving somewhere.
    """
    if t < 0:
        return 0.0

    attack = min(1.0, t / 0.010)
    envelope = attack * math.exp(-4.0 * t)
    return envelope * (
        1.00 * math.sin(2 * math.pi * frequency * t)
        + 0.22 * math.sin(2 * math.pi * frequency * 2 * t) * math.exp(-4.0 * t)
    )


def build() -> list[int]:
    frames: list[int] = []
    total = int(RATE * DURATION)

    for index in range(total):
        t = index / RATE
        value = 0.0

        for position, frequency in enumerate(ARPEGGIO):
            start = position * NOTE_GAP
            # The last note is held: the phrase should land, not stop.
            decay = 3.2 if position == len(ARPEGGIO) - 1 else 5.0
            value += 0.42 * piano(frequency, t - start, decay=decay)

        for frequency in PAD_CHORD:
            value += 0.09 * strings(frequency, t - PAD_START)

        # The soft layer, following the arpeggio a beat behind each note.
        for position, frequency in enumerate(ARPEGGIO):
            value += 0.16 * glow(frequency, t - position * NOTE_GAP - 0.02)

        # Weight under the final note instead of shimmer above it.
        value += 0.30 * bass(ARPEGGIO[0] / 4, t - 2 * NOTE_GAP)

        # Fade the final 150ms so the file cannot end on a discontinuity.
        remaining = DURATION - t
        if remaining < 0.12:
            value *= remaining / 0.12

        frames.append(value)

    peak = max(abs(sample) for sample in frames) or 1.0
    # Leaves headroom rather than normalising to full scale, which clips on
    # anything that applies its own gain afterwards.
    # Quieter overall than a normalised file would be. This plays over a game
    # somebody is in the middle of; it needs to be noticed, not obeyed.
    return [int(max(-1.0, min(1.0, sample / peak * 0.55)) * 32000) for sample in frames]


def main() -> None:
    frames = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(OUTPUT), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"".join(struct.pack("<h", frame) for frame in frames))

    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size // 1024} KB, {DURATION:.1f}s)")


if __name__ == "__main__":
    main()
