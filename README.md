# Perp Farming Radar — MVP

Perp DEX 경쟁률 및 포인트 파밍 효율을 별도로 분석하는 Streamlit 대시보드입니다.

## 실행

```bash
pip install -r requirements.txt
streamlit run app.py
```

실행 후 브라우저에서 나타나는 주소를 열면 됩니다. 실제 데이터를 불러오려면 인터넷 연결이 필요합니다.

## 데이터 소스

- DefiLlama 공개 `/protocols`: 프로토콜 탐색, 등록일, TVL(선별 참고 자료)
- DefiLlama 공개 `/overview/open-interest`: 거래소별 OI (응답에 포함된 경우)
- Hyperliquid 공식 `/info` (`metaAndAssetCtxs`): 24h 거래량 및 시점 OI (시장 컨텍스트)
- 선택: DefiLlama Pro `/api/overview/derivatives`: 거래량 24h/7d 및 전주 비교
- 사용자가 제공하는 `manual_metrics.csv`: 활성 지갑 수, 참여자 증가, 주간 포인트 예산, 점수 기준과 프로젝트별 Volume/OI 등

주의: 일부 공개 API의 프로토콜명/범위가 서로 다릅니다. 자동 결합은 정규화된 정확한 이름 매칭에만 적용되며 필요 시 수동 CSV 이름을 맞춰 주세요. 중복 프로토콜, 비-PERP 및 누락값을 주의해야 합니다.

## 점수 해석

- 참가자 분석: 지갑 수가 있을 때만 경쟁도 percentile을 표시하고 신규 유입률을 별도 확인.
- 포인트 효율: `weekly_points / total_eligible_activity * user_eligible_activity` (단일 활동 비례 배분 모델일 때만). 계산은 포인트 배분 보장이 아닌 조건부 시뮬레이션입니다.
- 지표가 확인되지 않은 프로젝트는 N/A입니다. 총 거래량을 '유효 거래량'으로 자동 간주하지 않습니다.
- 고래/시빌/다중 지갑 및 유동성 이벤트로 지갑 수가 실제 사람 수보다 많을 수 있습니다.
- 시점 OI는 **시간가중 OI가 아닙니다.** 시간이 필요한 OI 채굴 시뮬레이션에 사용하지 마세요.

## 데이터 지속성

앱에서 '스냅샷 저장'을 누르면 로컬 `radar_snapshots.csv` 파일에 저장합니다. 서버/클라우드에 자동 저장되지 않습니다. 원본 API 호출은 실시간 fetch/캐시 간격을 따르며 24시간 영속 수집은 하지 않습니다.

## 보안

읽기 전용 데이터만 사용합니다. 지갑 연결/서명/매매 기능이 없고 API 키는 서버 세션에서만 이용합니다. 유료 API 키는 공유 환경에 입력하지 마세요.

## Windows 빠른 실행

1. Python 3.10 이상을 설치합니다.
2. ZIP 압축을 해제하고 `perp-farming-radar/start_windows.bat`를 더블클릭합니다.
3. 브라우저에서 로컬 대시보드를 확인합니다.

처음 실행 시 패키지 설치가 필요하며 Python이 PATH에 설정되어 있어야 합니다.

## 관심 목록(기본 포함)

`watchlist.csv`에 Variational, Nado, StandX, N1, Arcus, Entropy, QFEX, TxFlow, robinhoodchain.lighter, Pacifica, hello.trade를 등록했습니다.
이 목록은 우선순위를 정하는 **워치리스트**이며, 각각의 활성 지갑/포인트 데이터가 자동 연동되었다는 의미는 아닙니다.
특히 `robinhoodchain.lighter`의 시장 및 포인트는 일반 `Lighter` 통계와 임의로 합산하지 않습니다.
