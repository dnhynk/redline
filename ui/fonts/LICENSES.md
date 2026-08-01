# ui/fonts — 동봉 폰트의 출처와 라이선스

전부 **SIL Open Font License 1.1 (OFL)** 이다. 각 woff2의 name 테이블(저작권·라이선스
고지)은 서브셋 과정에서 **보존**했다(`name_IDs='*'`). 런타임 CDN 의존은 0 — 이 파일들이
`/ui/fonts/`로 로컬 서빙된다 (design-polish.md §1).

| 파일 | 폰트 / 버전 | 역할 | 저작권 | 출처(빌드 시 1회 다운로드) |
|---|---|---|---|---|
| `PretendardVariable.subset.woff2` | Pretendard v1.3.9 (가변 wght 45–920) | UI 산세리프 — 교정 부호·라벨, 한글·라틴 한 벌 | © 2021–2023 Kil Hyung-jin | github.com/orioncactus/pretendard (npm `pretendard@1.3.9`) |
| `NotoSerifKR-Regular.subset.woff2` `NotoSerifKR-Bold.subset.woff2` | Noto Serif KR (Serif 2.003, SubsetOTF/KR, 400·700 정적) | 원고 세리프 — 한글 | © 2017–2024 Adobe, with Reserved Font Name 'Noto' | github.com/notofonts/noto-cjk (`Serif/SubsetOTF/KR`) |
| `SourceSerif4Variable-latin.woff2` | Source Serif 4 (가변 wght 200–900, latin) | 원고 세리프 — 라틴 (스택에서 한글보다 먼저 받는다) | © 2014–2021 Adobe, with Reserved Font Name 'Source' | github.com/adobe-fonts/source-serif (npm `@fontsource-variable/source-serif-4@5.2.5`) |
| `JetBrainsMonoVariable-latin.woff2` | JetBrains Mono (가변 wght 100–800, latin) | 데이터 모노 — E번호·수치·raw 레인 | © 2020 The JetBrains Mono Project Authors | github.com/JetBrains/JetBrainsMono (npm `@fontsource-variable/jetbrains-mono@5.2.5`) |

## 서브셋 내역 (fonttools 4.60.1 `pyftsubset`, flavor=woff2)

- **한글 2종(Pretendard·Noto Serif KR)**: 현대 한글 음절 **전체 U+AC00–D7A3(11,172자) 유지** —
  자유 입력이므로 흔한 2,350자만 남기면 무대에서 폴백이 샌다. 라틴(U+0000–00FF)·
  일반 문장부호·화살표·「」·호환 자모·전각 포함. 잘라낸 것은 한자·가나·세로쓰기
  글리프(vert)·옛한글 조합 클로저다. Noto Serif KR은 gvar가 무거워(15.9MB) 실사용
  웨이트 400·700의 **정적 인스턴스**로 고정했다.
- **라틴 2종**: Fontsource가 배포하는 latin 서브셋 가변 woff2를 그대로 쓴다.
- 검수: `한글뷁쀍똠빵퀭희「」·…—%↗→↓✓₩` 전 글자 커버 확인 (빌드 스크립트가 cmap 검사).

## SIL Open Font License 1.1 요지

원문: https://openfontlicense.org · https://scripts.sil.org/OFL

폰트의 사용·수정·재배포(서브셋 포함)를 허용한다. 조건: (1) 폰트 자체를 단독으로
판매하지 않는다 (2) 수정본 재배포 시 저작권·라이선스 고지를 유지한다 (3) Reserved
Font Name('Source', 'Noto')을 수정본의 새 이름으로 쓰지 않는다 — 우리는 이름을 바꾸지
않고 서브셋만 했고 고지는 각 파일 name 테이블에 그대로 있다 (4) 폰트는 OFL로만
재배포된다. 이 저장소의 코드 라이선스와 무관하게 폰트 파일에는 OFL이 적용된다.
