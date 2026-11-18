# Sampled sound discs

Sampled audio through the BBC Micro's own sound chip, the SN76489, with no extra hardware. The
codec chooses one volume write per sample from all 4,096 volume combinations, as measured on a real Model B.

| Disc | Machine | What | Run it in jsbeeb |
|---|---|---|---|
| `sn_stream.adl` | Master 128, ADFS | 41 s of President Kennedy at Rice University, 1962, streamed from disc as it reads, 15.6 kHz | [play](https://bbc.xania.org/?disc=https://raw.githubusercontent.com/8bitkick/beeb-remote/sn-audio-discs/discs/sn_stream.adl&autoboot&model=MasterADFS&noseek) |
| `bemybaby_stream.adl` | Master 128, ADFS | the first 41 s of *Be My Baby* (The Ronettes, 1963), streamed from disc, 15.6 kHz, its loudest 0.3% of samples limited and the full range used, for loudness | [play](https://bbc.xania.org/?disc=https://raw.githubusercontent.com/8bitkick/beeb-remote/sn-audio-discs/discs/bemybaby_stream.adl&autoboot&model=MasterADFS&noseek) |
| `sn_master.dsd` | Master 128, DFS | 5.4 s from RAM at 20.8 kHz: side 0 Kennedy, side 2 *Funkorama* (`*RUN :2.PLAY`) | [play](https://bbc.xania.org/?disc=https://raw.githubusercontent.com/8bitkick/beeb-remote/sn-audio-discs/discs/sn_master.dsd&autoboot&model=Master&noseek) |

On a real Master, boot `sn_stream.adl` with SHIFT+A+BREAK and `sn_master.dsd` with SHIFT+BREAK.
Both were checked in beebjit and jsbeeb, every byte reaching the sound chip, but haven't yet been played on a
real Master. The levels come from one Model B's chip, so a Master will be a little off.

The Kennedy speech is from NASA's video of it, which is public domain. *Be My Baby* (Spector,
Greenwich, Barry), recorded by The Ronettes, is an excerpt used as a technical demonstration; the
recording and song belong to their rights holders. *Funkorama* is by
Kevin MacLeod (incompetech.com), licensed under CC BY 4.0.

`&noseek` in the links turns off jsbeeb's disc drive sounds.
