# Measured response curves (appendix)

Audio-measured laws for the pad-record and event fields whose byte values map
nonlinearly to sound. **All measurements were made on EP-133 OS 2.5.1 via the
device's own class-compliant USB-audio output** (OS 2.5+): synthesized bytes →
FILE write → playback over external MIDI clock → capture → analysis
(onset detection + RMS ring-out, −40 dB release threshold; audio onsets
matched captured MIDI note-ons within ±9 ms). One variable per run, one
isolated hit per data point. Byte locations for every field are in
[project-format.md](project-format.md).

Re-verify on any firmware change: these are 2.5.1 claims.

## Amplitude (byte 16)

**dB-domain fader, not linear gain.** Default 100 ≈ unity; ~0.5 dB/unit below
100; soft +6 dB ceiling above. 9-point isolated-hit sweep (levels relative to
the value-100 reference at −6 dB in the capture chain):

| byte | level (dB) |
|---:|---:|
| 10 | −47 |
| 21 | −39 |
| 40 | −28 |
| 60 | −18 |
| 82 | −10 |
| 100 | −6 (≈ unity ref) |
| 130 | −2 |
| 160 | −1 |
| 200 | −0.5 |

## Velocity (event byte 4)

Loudness curve is **shallower than linear amplitude**. 9-point isolated-hit
sweep, dB relative to velocity 127:

| velocity | dB rel 127 |
|---:|---:|
| 8 | −21.2 |
| 16 | −15.4 |
| 32 | −9.8 |
| 46 | −6.9 |
| 64 | −4.5 |
| 79 | −3.0 |
| 96 | −1.7 |
| 112 | −0.8 |
| 127 | 0 (ref) |

Values ≥ 128 play at velocity 1 — the field is not plain 8-bit.

## Release (byte 20)

Semantics first (verified): **255 = play-through in every play mode** (event
duration irrelevant). Below 255, **one-shot mode decays from note-ON**
(duration-independent); **key/legato modes sustain to note-off then fade**.
Release 0 chokes to a ~20 ms click — the classic "drums play sudden" failure.

Decay-time law in one-shot, measured on a sustained chop:
**time ≈ (byte/70)² seconds**. Published points from the 10-point sweep:

| byte | decay (s) |
|---:|---:|
| 16 | 0.04 |
| 32 | 0.23 |
| 48 | 0.48 |
| 64 | 0.84 |
| 96 | 1.89 |
| 128 | 3.25 |

Content-dependent on already-decaying samples (the sample's own envelope wins
when it is faster). At note-off in key/legato, release 64 measured ≈ 145 ms
fade.

## Attack (byte 19)

Attack ramp. 10–90 % rise measured on a 250 ms kick:

| byte | rise (ms) |
|---:|---:|
| 0 | 2 |
| 64 | 12 |
| 160 | 61 |
| 255 | 73 (time-to-peak 106 ms) |

Long ramps also cut peak level ~12 dB on short samples — the apparent byte
ceiling was sample-length truncation, and the ATK *fader* reaches ramp times
in the seconds range; re-derive the ceiling on a long sample before relying
on the 255 point.

## Pan (byte 18)

i8, −16..+16, negative = left. **Constant-power law**: ±16 fully mutes the far
channel (digital zero); ±8 measured ≈ −4.8 dB on the far channel / +1.2 dB on
the near channel (5-point sweep).

## Pitch law (event note byte 3 vs pad root byte 24)

**Playback rate = 2^((note − root)/12), in every play mode including
one-shot** — plain resampling, no time-stretch. Verified by ring-duration
ratios: a 1 s trimmed chop played at note −18 st rang 2,843 ms
(2.83× = 2^(18/12)); note = root restores true pitch exactly. The event note
byte is simultaneously the emitted MIDI note during playback. Pad pitch
byte 17 (±12 st, verified by resample ratio) and pitch-fraction byte 26
(cents/100; +50 measured +52 cents) stack on top.

Practical consequence (the finding that motivated these probes): triggering
pads at physical MIDI notes 36–47 against the default root 60 plays every
sound 13–24 semitones slow — a −24 st kick is a dull thud with no transient.
Fix by writing note = root, or setting each pad's root byte to its own
physical note.

## Group LVL fader law

`settings` fader base / automation value for parameter LVL:
**amplitude ∝ (value/32767)²** (40 dB/decade), steepening below ~15 % of
range. 7-point dose–response plus automation staircase points. The base f32
and the automation u16 share one scale (`f32 = u16/32767`, cross-verified).

## Related timing facts (same rig)

- Trim length (bytes 8–11) truncates playback at the stored native-rate frame
  count, scaled by the repitch rate.
- MIDI transport **stop chokes ringing one-shots immediately**.
- Pads are monophonic; same-pad retriggers choke the prior voice
  (energy-additivity probe).
