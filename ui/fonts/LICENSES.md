# Bundled fonts

The fonts in this folder are licensed under the SIL Open Font License 1.1. [OFL.txt](OFL.txt) holds the full license text and the upstream copyright notices. The MIT license of this repository does not cover these files.

The server serves the fonts from `/ui/fonts/`, so the UI loads no font from a CDN. Subsetting kept each file's `name` table (`name_IDs='*'`), so every file still carries its own copyright and license records.

| File | Font and version | Used for | Copyright record in the file | Upstream |
|---|---|---|---|---|
| `PretendardVariable.subset.woff2` | Pretendard 1.3.9, variable (wght 45–920) | UI sans serif: marks and labels, Hangul and Latin | Copyright © 2023 Kil Hyung-jin | [orioncactus/pretendard](https://github.com/orioncactus/pretendard) (npm `pretendard@1.3.9`) |
| `NotoSerifKR-Regular.subset.woff2`, `NotoSerifKR-Bold.subset.woff2` | Noto Serif KR 2.003 (`Serif/SubsetOTF/KR`), static 400 and 700 | Manuscript serif, Hangul | © 2017-2024 Adobe; Noto is a trademark of Google Inc. | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) |
| `SourceSerif4Variable-latin.woff2` | Source Serif 4 (4.004), variable (wght 200–900), Latin subset | Manuscript serif, Latin; listed before the Hangul face in the font stack | © 2014 - 2021 Adobe Systems Incorporated, with Reserved Font Name 'Source' | [adobe-fonts/source-serif](https://github.com/adobe-fonts/source-serif) (npm `@fontsource-variable/source-serif-4@5.2.5`) |
| `JetBrainsMonoVariable-latin.woff2` | JetBrains Mono 2.211, variable (wght 100–800), Latin subset | Data mono: evidence IDs, numbers, the raw event stream | Copyright 2020 The JetBrains Mono Project Authors | [JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono) (npm `@fontsource-variable/jetbrains-mono@5.2.5`) |

## Subsetting

The files were made once with fonttools 4.60.1 `pyftsubset`, flavor `woff2`.

- Pretendard and Noto Serif KR keep all 11,172 modern Hangul syllables (U+AC00–D7A3). Input is free text, and keeping only the 2,350 common syllables would let fallback glyphs appear on screen. Latin (U+0000–00FF), general punctuation, arrows, 「」, compatibility jamo and full-width forms are kept. Hanja, kana, vertical-writing glyphs (`vert`) and the Old Hangul composition closure are removed.
- Noto Serif KR's variable `gvar` table is large (15.9 MB), so the two weights in use, 400 and 700, are static instances.
- Source Serif 4 and JetBrains Mono are Fontsource's Latin-subset variable woff2 files, unchanged.
- The build script checked that the cmap covers every character of `한글뷁쀍똠빵퀭희「」·…—%↗→↓✓₩`.

## License terms in short

The OFL allows use, modification and redistribution of the fonts, subsets included, on four conditions. A font file is not sold by itself. Redistributed copies keep the copyright notice and the license. A modified version does not use a Reserved Font Name. The fonts are redistributed only under the OFL.

These files are subsets of the original fonts. Their family names are unchanged, and the copyright and license records remain in each file's `name` table.
