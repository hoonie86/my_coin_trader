import asyncio
import pandas as pd
import numpy as np
import requests
from datetime import datetime
from datetime import datetime, timedelta
from config import logger

market_ref_rate = 0.0
is_buy_locked = False
panic_msg_sent = False
cooldown_dict = {}

def is_in_cooldown(symbol):
    if symbol in cooldown_dict:
        if datetime.now() < cooldown_dict[symbol]:
            return True
    return False
    
async def update_market_panic_status(current_avg):
    global market_ref_rate, is_buy_locked
    # 1. 최초 잠금: -3% 돌파 시
    if not is_buy_locked and current_avg <= -3.0:
        is_buy_locked = True
        market_ref_rate = current_avg
        logger.info(f"🚨 [시장잠금] 패닉 상태 감지 (기준점: {market_ref_rate:.2f}%)")
        msg = f"🚨 [시장잠금] 패닉 상태 감지\n기준점: {market_ref_rate:.2f}%\n현재 모든 매수가 중단됩니다."
        return True, msg # 호출부(main.py)에서 이 메시지를 받아 알림 전송
    # 2. 잠금 상태일 때 (해제 또는 바닥 갱신)
    elif is_buy_locked:
        # 1. 차등 해제 기준 설정
        if market_ref_rate <= -5.0:
            threshold = 2.0  # 폭락장 (-5% 이하) : 2.0% 이상 반등 시 해제 (신중)
        elif market_ref_rate <= -3.0:
            threshold = 1.5  # 하락장 (-3% ~ -5%) : 1.5% 이상 반등 시 해제 (보통)
        else:
            threshold = 1.0  # 일반조정 (0% ~ -3%)  : 1.0% 이상 반등 시 해제 (공격)

        # 2. 해제 조건 판단
        if current_avg >= market_ref_rate + threshold:
            is_buy_locked = False
            market_ref_rate = current_avg
            logger.info(f"✅ 시장 반등 확인({threshold}%): 매수 잠금 해제 (기준점: {market_ref_rate:.2f}%)")
        
        # 3. 바닥 실시간 추적 (사용자님 철학: 기준점은 하향 갱신만 허용)
        elif current_avg < market_ref_rate:
            market_ref_rate = current_avg
            logger.info(f"📉 시장 바닥 갱신: 기준점 하향 조정 ({market_ref_rate:.2f}%)")
    # 3. 해제 상태일 때 (재잠금/데드캣 방지)
    else:
        if current_avg <= market_ref_rate - 2.0:
            is_buy_locked = True
            market_ref_rate = current_avg
            # [추가] 상태 변화 시 메시지 리턴
            return True, f"🚨 [재잠금] 데드캣 방지 필터 작동\n기준점: {market_ref_rate:.2f}%"

    # [추가] 함수 맨 끝에 어떤 경우에도 에러 안 나게 빈 값 리턴
    return False, None

def get_bithumb_tick_size(price, direction=None):
    # [1] 기본 틱 사이즈 결정 (분석용 이격/기울기 계산의 기준점)
    if price < 10: tick = 0.001
    elif price < 100: tick = 0.01
    elif price < 1000: tick = 0.1
    elif price < 5000: tick = 1
    elif price < 10000: tick = 5
    elif price < 50000: tick = 10
    elif price < 100000: tick = 50
    else: tick = 100

    # [2] 상황별 반환 처리
    # 'direction' 인자가 있다면 매도 주문용 '가격'을 반환 (사용자 의도 반영)
    if direction == 'down':
        return price - tick  # 예: 54,300 - 100 = 54,200원
    
    # 'direction'이 없다면 분석용 '단위'를 반환 (기존 diff_185/slope_rate 로직 보호)
    return tick  # 예: 100원


def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    return 100 - (100 / (1 + (ema_up / ema_down)))


def get_warning_list():
    try:
        url = "https://api.bithumb.com/public/assetsstatus/ALL"
        res = requests.get(url, timeout=5).json()
        data = res.get('data', {})
        
        ###### [수정 시작: 거래중지(halt)뿐만 아니라 입출금 제한 종목까지 모두 차단] ######
        warning_coins = []
        for coin, info in data.items():
            # halt_status가 0이 아니거나(중지), 입금/출금이 하나라도 막혀있으면(0) 유의/위험으로 간주
            if info.get('halt_status', 0) != 0 or \
               info.get('deposit_status', 1) == 0 or \
               info.get('withdrawal_status', 1) == 0:
                warning_coins.append(coin)
        return warning_coins
        ###### [수정 끝] ######
        
    except Exception as e:
        logger.error(f"Warning List Fetch Error: {e}")
        return []


def get_updated_emergency_level(symbol, current_level, buy_type, rsi, is_3m_below_ma40, ma40_slope, is_converging, profit_pct, soaring_rate, has_rsi_spike, max_profit_pct, is_type3_stable):
    """
    Level 2 -> 1 (하향): 타입별 분기된 조건 적용
    Level 1 -> 0 (해제): 타입별 독립 기준 적용
    """
    # [교정] 타입별 하향(2->1) 조건 정의
    is_recovering_general = (str(buy_type) != '3' and is_3m_below_ma40)
    is_recovering_type3 = (str(buy_type) == '3' and is_type3_stable)

    # 1. 트리거 (0 -> 2): 진입 조건 (기존 유지)
    if current_level == 0:
        if (profit_pct >= 10.0 or has_rsi_spike or soaring_rate >= 2.0 or 
            (str(buy_type) == '3' and not is_type3_stable) or max_profit_pct >= 5.0):
            return 2
    
    # 2. 하향 (Level 2 -> 1): 타입별로 분기된 변수에 따라 전환
    if current_level == 2:
        if is_recovering_general or is_recovering_type3:
            logger.info(f"✅ {symbol} 지표 안정화 시작 -> Level 1(CAUTION) 전환")
            return 1
                
    # 3. 해제 (Level 1 -> 0): 타입별 독립 기준 적용
    if current_level == 1:
        if str(buy_type) == '3':
            # [Type 3 해제] 40선 우상향 및 185선 이격 축소
            if ma40_slope > 0 and is_converging and (not is_3m_below_ma40):
                logger.info(f"✨ {symbol} [Type 3 안정] 40선 반등 및 안착 확인으로 해제")
                return 0
        else:
            # [Type 1, 2 해제] RSI 50 미만 도달
            if rsi < 50:
                logger.info(f"✨ {symbol} [Type 1,2 안정] RSI 50 미만으로 해제")
                return 0
            
    return current_level

# [사용자 원본 버전 1]
def check_buy_signal_v1(df, symbol, warning_list):
    try:
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        curr_price = float(curr['close'])

        if pd.isna(curr['ma185']) or pd.isna(prev['ma185']):
            return False, "지표계산오류(NaN)"

        # 1. 기본 기울기 및 데이터 계산
        tick_size = get_bithumb_tick_size(curr['ma185'])
        if not tick_size or tick_size == 0: tick_size = 1
        diff_185 = (curr['ma185'] - prev['ma185']) / tick_size
        slope_rate = ((curr['ma185'] - prev['ma185']) / prev['ma185']) * 100
        
        # 2. [사용자 의도 반영] 하락 확인 기간 조정 (2일 전 vs 5시간 전)
        # 30분봉 기준: 2일 전(-96), 5시간 전(-10)
        ma185_past_2d = df['ma185'].iloc[-96] if len(df) >= 96 else df['ma185'].iloc[0]
        ma185_recent_5h = df['ma185'].iloc[-10] if len(df) >= 10 else df['ma185'].iloc[0]
        
        # 과거 2일간 하락세였는지 확인
        is_was_descending = ma185_recent_5h <= ma185_past_2d
        # 현재 반등 중(-0.06 이상)인지 확인
        is_turning_up = slope_rate >= -0.06

        # 3. [논리 결합] 충분히 하락했거나, 혹은 지금 반등/평행 중이면 통과
        if not (is_was_descending or is_turning_up):
            reason = f"185일선 추세 부적합(기울기:{slope_rate:.4f}%)"
            return False, reason, "", {} # data_dict가 필요하면 추가

        # 4. 급격한 폭락(-1.2 미만)은 여전히 방어
        if diff_185 < -1.2:
            return False, "185일선 급락 차단"

        # 여기서부터 gold_index 로직 시작...
        gold_index = -1
        for i in range(1, 97):
            if df['ma40'].iloc[-i - 1] < df['ma185'].iloc[-i - 1] and \
                    df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                gold_index = len(df) - i
                break

        if gold_index == -1: return False, ""
        bars_since_gold = len(df) - gold_index
        if bars_since_gold < 4: return False, ""

        # ##########################################################
        # [신규 추가] 골크 10봉 전 ~ 현재 직전봉까지의 최고가 필터
        # ##########################################################
        # 기존: gold_index 이후만 체크 -> 수정: 골크 10봉 전부터 체크 (사용자 의도)
        search_start_idx = max(0, gold_index - 10)
        # iloc[start:-1]을 사용하여 '현재 확정되지 않은 봉'을 제외한 직전봉까지의 고점을 봅니다.
        relevant_range = df.iloc[search_start_idx : -1]
        max_peak_price = relevant_range['high'].max()

        if curr_price < max_peak_price * 0.95:
            return False, f"고점({max_peak_price:,.0f}) 대비 5% 이상 이탈(설거지 방어)"
        # ##########################################################

        disparity_40 = abs(curr_price - curr['ma40']) / curr['ma40']
        if curr['rsi'] > 65: return False, ""
        disparity_gold = abs(curr['ma40'] - curr['ma185']) / curr['ma185']

        if curr_price > curr['ma40']:
            if disparity_40 <= 0.07:
                if -0.08 <= slope_rate < -0.01:
                    return True, "✅ [A] 185선 약하락 중 골든크로스"
                elif slope_rate >= -0.01:
                    if disparity_gold <= 0.005:
                        return True, "💎 [S+] 185선 평행/상승 & 40선 초밀착"
                    elif disparity_gold <= 0.015:
                        return True, "⭐ [S] 185선 평행/상승 & 40선 수렴 중"
                    else:
                        return True, "🚀 [A+] 185선 하락 멈춤 및 평행/우상향"
        return False, ""
    except Exception as e:
        logger.error(f"❌ 매수 신호 포착 중 오류 ({symbol}): {e}")
        return False, "에러발생"


# 긴급 감시 상태 저장 변수
emergency_mode = {}


# ---------- [신규] data_dict 전체 수치 채우기 (조건 탈락 여부와 관계없이) ----------
def _fill_data_dict_full(df, curr, prev, curr_price, symbol):
    """모든 수치(RSI, 이격도, 기울기 등)를 조건 탈락 여부와 관계없이 계산해 data_dict 반환."""
    ma40_val = float(curr['ma40']) if not pd.isna(curr.get('ma40')) else 0
    ma185_val = float(curr['ma185']) if not pd.isna(curr.get('ma185')) else 0
    rsi_val = float(curr['rsi']) if not pd.isna(curr.get('rsi')) else 50
    slope_rate = ((curr['ma185'] - prev['ma185']) / prev['ma185']) * 100 if prev.get('ma185') and prev['ma185'] != 0 else 0
    disparity_40 = abs(curr_price - curr['ma40']) / curr['ma40'] if curr.get('ma40') and curr['ma40'] > 0 else 999
    disparity_185 = abs(curr_price - curr['ma185']) / curr['ma185'] if curr.get('ma185') and curr['ma185'] > 0 else 999
    disparity_gold = abs(curr.get('ma40', 0) - curr['ma185']) / curr['ma185'] if curr.get('ma185') and curr['ma185'] > 0 else 999
    gold_index = -1
    for i in range(1, min(97, len(df))):
        if i + 1 <= len(df) and df['ma40'].iloc[-i - 1] < df['ma185'].iloc[-i - 1] and df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
            gold_index = len(df) - i
            break
    bars_since_gold = len(df) - gold_index if gold_index != -1 else -1
    base_period = 20
    recent_volumes = df['vol'].tail(base_period)
    base_avg_vol = recent_volumes.mean() if len(recent_volumes) else 0
    curr_vol = curr.get('vol', 0)
    vol_ratio = (curr_vol / base_avg_vol) if base_avg_vol and base_avg_vol > 0 else 0
    disparity_185_pct = (curr_price - ma185_val) / ma185_val * 100 if ma185_val and ma185_val != 0 else 0
    return {
        'rsi': rsi_val,
        'ma40_val': ma40_val,
        'ma185_val': ma185_val,
        'current_price': curr_price,
        'grade': '',
        'slope_rate': slope_rate,
        'disparity_40': disparity_40,
        'disparity_40_pct': disparity_40 * 100,
        'disparity_185': disparity_185,
        'disparity_185_pct': disparity_185_pct,
        'disparity_gold': disparity_gold,
        'bars_since_gold': bars_since_gold,
        'vol_ratio': vol_ratio,
        'has_volume_surge': (base_avg_vol and curr_vol >= base_avg_vol * 1.1),
        'max_vol_ratio': max((v / base_avg_vol for v in df['vol'].tail(3)) if base_avg_vol else [0], default=0),
    }


# ---------- [신규] 미지 패턴 라벨링: 정배열 / 단기역습 / 바닥탈출 ----------
def _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val):
    """
    조건 탈락 여부와 관계없이 패턴 태그만 산출.
    - [정배열]: 5일/20일/185일선이 위에서부터 순서대로 정렬 (가격 > ma5 > ma20 > ma185)
    - [단기역습]: 185일선 아래에서 5일선이 20일선을 뚫음 (골든크로스, 현재가 > ma20)
    - [바닥탈출]: RSI 25 이하에서 반등(직전 봉 대비 상승)
    """
    labels = []
    if ma5_val is not None and ma20_val is not None and ma185_val is not None:
        if curr_price > ma5_val and ma5_val > ma20_val and ma20_val > ma185_val:
            labels.append("정배열")
        if len(df) >= 3:
            prev_5, prev_20 = df['ma5'].iloc[-2], df['ma20'].iloc[-2]
            if not (pd.isna(prev_5) or pd.isna(prev_20)) and curr_price > ma20_val and prev_5 <= prev_20 and ma5_val > ma20_val:
                if curr_price < ma185_val:
                    labels.append("단기역습")
    if rsi_val is not None and rsi_val <= 25:
        if len(df) >= 2 and float(df['close'].iloc[-1]) > float(df['close'].iloc[-2]):
            labels.append("바닥탈출")
        else:
            labels.append("바닥근접")
    return labels


# [사용자 원본 버전 2 - 메인 사용 중인 로직]
# [확장] 하락장 대응 + 정배열 전환 + 급등 추적 모두 반영. 기존 로직 삭제 없이 주석/분기로 보강.
async def check_buy_signal(exchange, df, symbol, warning_list):
    """
    매수 신호 판단 함수 (4개 값 리턴)
    
    df_1m: optional. 1분봉 DataFrame (columns: time, open, high, low, close, vol).
           수급 돌파(1분봉 거래량 300% + 3분 내 3% 급등) 판별 시 사용. 없으면 30분봉 기준으로만 판별.
    
    Returns:
        tuple: (is_buy: bool, reason: str, grade: str, data_dict: dict)
    """
    grade = ""
    # [시장 방어막 체크]
    global is_buy_locked, market_ref_rate
    # [[ UPDATE: 잔고 및 데이터 오류, 재매수 제한 필터 ]]
    try:
        curr = df.iloc[-1]
        curr_price = float(curr['close'])
        if curr_price <= 0:
            return False, "⚠️ [데이터오류] 현재가 조회 실패(0)", "", {}

        if is_in_cooldown(symbol):
            return False, "⏱️ [Cooldown] 매도 후 재진입 제한 중", "", {}

        # # asyncio.wait_for를 사용하여 10초 응답 지연 시 강제 탈출
        # balance = await asyncio.wait_for(asyncio.to_thread(exchange.fetch_free_balance), timeout=10)
        # if balance.get('KRW', 0) < 5500:
        #     return False, "🚫 [잔고부족] 가용 KRW 부족 (수수료 포함 매수 루프 차단)", "", {}
    except asyncio.TimeoutError:
        return False, "⚠️ [네트워크지연] 빗썸 잔고조회 타임아웃", "", {}
    except Exception as e:
        return False, f"⚠️ 초기 필터링 에러: {e}", "", {}  

    if is_buy_locked:
        return False, f"DEBUG: 🚫 [시장잠금] Panic Filter 작동 중 (해제 기준:{-1 - market_ref_rate:.2f}% 상승)", "", {}

    # 기본 data_dict 초기화 (조건 탈락 여부와 관계없이 끝까지 계산해 빈칸 채움)
    data_dict = {}
    
    if len(df) < 285:
        return False, "데이터부족", "", data_dict

    # [기존 유지] 40/185일선 + RSI
    df.loc[df.index[-1], 'close'] = curr_price
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma40'] = df['close'].rolling(40).mean()
    df['ma90'] = df['close'].rolling(90).mean()
    df['ma185'] = df['close'].rolling(185).mean()
    df['rsi'] = calculate_rsi(df)

    curr = df.iloc[-1]
    prev = df.iloc[-2]
    curr_price = float(curr['close'])

    ###### 전수조사 반영: 모든 TYPE에서 사용하는 공통 변수 사전 정의 ######
    ma5_val = float(curr['ma5']) if not pd.isna(curr['ma5']) else curr_price
    ma40_val = float(curr['ma40']) if not pd.isna(curr['ma40']) else 0
    ma90_val = float(curr['ma90']) if not pd.isna(curr['ma90']) else 0
    ma185_val = float(curr['ma185']) if not pd.isna(curr['ma185']) else 0
    rsi_val = float(curr['rsi']) if not pd.isna(curr['rsi']) else 50

    prev_ma5 = float(prev['ma5'])
    prev_ma40 = float(prev['ma40'])
    prev_ma90 = float(prev['ma90'])
    prev_ma185 = float(prev['ma185'])

    # 이격도 계산 (현재 및 이전 봉)
    disparity_gold = abs(ma40_val - ma185_val) / ma185_val if ma185_val > 0 else 999
    prev_dis_gold = abs(prev_ma40 - prev_ma185) / prev_ma185 if prev_ma185 > 0 else 999
    slope_rate = ((ma185_val - prev_ma185) / prev_ma185) * 100 if prev_ma185 > 0 else 0
    
    # 40선/90선 골든크로스 상태 (TYPE 1, 2 공통 가중치 가드)
    is_40_90_gc = prev_ma40 <= prev_ma90 and ma40_val > ma90_val
    is_40_above_90 = ma40_val > ma90_val    

    # TYPE 3용 이격도
    disparity_5_185 = (ma5_val - ma185_val) / ma185_val * 100 if ma185_val > 0 else 0
    prev_disparity = (prev_ma5 - prev_ma185) / prev_ma185 * 100 if prev_ma185 > 0 else 0
    # [[ UPDATE: 골든크로스 기반 가변 윈도우 상단 방어 ]]
    gold_index = -1
    ###### [수정] bars_since_gold 초기값 선언 ###### 144봉(3일)
    bars_since_gold = 999 
    for i in range(1, 145):
        if i+1 < len(df):
            if df['ma40'].iloc[-i-1] < df['ma185'].iloc[-i-1] and df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                gold_index = len(df) - i
                bars_since_gold = i 
                break
    data_dict['bars_since_gold'] = bars_since_gold
    
    start_idx = max(0, gold_index - 20) if gold_index != -1 else max(0, len(df) - 25)
    window_df = df.iloc[start_idx:]
    win_low, win_high = window_df['low'].min(), window_df['high'].max()
    volatility = ((win_high - win_low) / win_low * 100) if win_low > 0 else 0
    
    curr_max_high_low = max(curr['open'], curr['close'])
    upper_wick = (curr['high'] - curr_max_high_low) / curr_max_high_low * 100
    
    if volatility >= 10.0:
        print(f"DEBUG: {symbol} 매수 탈락 - 변동성 과다({volatility:.1f}%)")
        return False, f"🚫 [상단방어] 구간 변동성({volatility:.1f}%) 및 저항 포착", "F", {}
        
    if curr_price < curr['high'] * 0.985:
        return False, f"🚫 [설거지방어] 고가대비 이탈(-1.5%↑)", "F", {}

    if upper_wick >= 2.0:
        print(f"DEBUG: {symbol} 매수 탈락 - 윗꼬리 과다({volatility:.1f}%)")
        return False, f"🚫 [윗꼬리 방어] 가격 변동성({volatility:.1f}%) 설거지 포착", "F", {}    
    # [가격 필터] 10원 미만 또는 10,000원 이상 → BTC 마켓 동전주/비정상 차단
    if curr_price < 1 or curr_price >= 10000:
        return False, "가격필터(BTC마켓)", "", data_dict

    # [유의 종목] 수급 돌파(S/S+) 포함 모든 매수 신호에서 투자유의 종목 제외 (먼저 검사)
    if symbol.split('/')[0] in warning_list:
        return False, "투자유의", "F", data_dict
    # 현재가(close) 대비 고가(high)의 순수 물리적 거리를 계산 (양봉 기준)
    upper_wick_dist_pct = (curr['high'] - curr_price) / curr_price * 100
    
    if upper_wick_dist_pct >= 2.0:
        return False, f"🚫 [저항과다] 윗꼬리(현재가대비):{upper_wick_dist_pct:.2f}%", "F", data_dict
        
    ###### [신규 추가] 스테이블 코인 및 185일선 고점(상위 30%) 원천 차단 ######
    # 1. 스테이블 코인 필터링 (USDC, USDT, DAI 등 차트 왜곡 종목)
    exclude_symbols = ['USDC', 'USDT', 'DAI', 'BUSD', 'USDE', 'USD1', 'USDP', 'GUSD']
    if symbol.split('/')[0] in exclude_symbols:
        return False, "제외종목(스테이블)", "", data_dict

    # 2. 185일선 상대적 위치 확인 (최근 200봉 기준 상위 30% 구간에 있으면 '이미 뜬 종목'으로 간주)
    # 밥그릇 패턴은 185선이 바닥에 깔려있어야 하므로, 고공행진 중인 185선은 무조건 거릅니다.
    lookback_range = 200
    recent_185 = df['ma185'].iloc[-lookback_range:] if len(df) >= lookback_range else df['ma185']
    min_185 = recent_185.min()
    max_185 = recent_185.max()
    curr_185 = float(curr['ma185']) if not pd.isna(curr['ma185']) else 0

    if max_185 > min_185 > 0:
        pos_185 = (curr_185 - min_185) / (max_185 - min_185)
        # 상위 30% (0.7 이상) 위치에 있다면 밥그릇 바닥이 아님
        is_high_pos_185 = pos_185 >= 0.5    # 상위 30%이면 is_high_pos_185 True

    # [수정] 골든크로스 여부 확인 후 '구간 변동성' 체크 수행
    if gold_index != -1:
        # 검사 시작점: 골든크로스 시점으로부터 20봉 전 (단, 인덱스 0보다 작으면 0)
        check_start_idx = max(0, gold_index - 20)
        
        # 동적 구간 설정: check_start_idx ~ 현재(-1)까지
        dynamic_window = df.iloc[check_start_idx:]
        
        win_low = dynamic_window['low'].min()
        win_high = dynamic_window['high'].max()
        
        # 구간 내 변동폭 계산
        dynamic_rise = ((win_high - win_low) / win_low * 100) if win_low > 0 else 0
        #print(f"DEBUG: {symbol} | 골크 발생. 골크점 : {199-gold_index} | 시작점 : {199-check_start_idx} | 저가: {win_low} | 고가: {win_high} | 급등락 크기: {dynamic_rise}% | 급등락YN: {data_dict.get('dynamic_rise_YN')}")
        # 7% 이상 급등락이 있었으면 탈락
        if dynamic_rise >= 10.0:
            data_dict['dynamic_rise_YN'] = 'Y'
            print(f"DEBUG: {symbol} | 골크 발생. 골크점 : {199-gold_index} | 시작점 : {199-check_start_idx} | 저가: {win_low} | 고가: {win_high} | 급등락 크기: {dynamic_rise:.2f}% | 급등락YN: {data_dict.get('dynamic_rise_YN')}")
            return False, f"🚫 [제외] 골크 전후 변동성 과다({dynamic_rise:.1f}% >= 5%)", "B", data_dict
        # else:
        #     print(f"DEBUG: {symbol} | 골크 발생. 골크점 : {199-gold_index} | 시작점 : {199-check_start_idx} | 저가: {win_low} | 고가: {win_high} | 급등락 크기: {dynamic_rise}% | 급등락YN: {data_dict.get('dynamic_rise_YN')}")
    else:
        # ###### [수정] 골든크로스 미발생 시 Type 3 여부에 따라 검사 구간 차등 적용 ######
        # Type 3(바닥 낚시) 후보군: 40선이 185선 아래 있고 RSI 40 이하 이력이 있을 때
        was_oversold_start = (df['rsi'].iloc[-30:] <= 40).any()
        
        if ma40_val < ma185_val and was_oversold_start:
            check_start_idx = -10  # Type 3는 최근 5시간(10봉)만 감시
        else:
            check_start_idx = -25  # 그 외 일반 미발생 종목은 기존 25봉 유지
        
        # 동적 구간 설정: check_start_idx ~ 현재(-1)까지
        dynamic_window = df.iloc[check_start_idx:]
        
        win_low = dynamic_window['low'].min()
        win_high = dynamic_window['high'].max()
        
        # 구간 내 변동폭 계산
        dynamic_rise = ((win_high - win_low) / win_low * 100) if win_low > 0 else 0
        # print(f"DEBUG: {symbol} | 골크 미발생. 시작점: {check_start_idx} | 저가: {win_low} | 고가: {win_high} | 급등락 크기: {dynamic_rise}% | 급등락YN: {data_dict.get('dynamic_rise_YN')}")
        # 7% 이상 급등락이 있었으면 탈락
        if dynamic_rise >= 10.0:
            data_dict['dynamic_rise_YN'] = 'Y'
            return False, f"🚫 [제외] 골크 미발생 변동성 과다({dynamic_rise:.1f}% >= 5%)", "B", data_dict
    bars_since_gold = len(df) - gold_index if gold_index != -1 else -1
    data_dict['bars_since_gold'] = bars_since_gold
    
    # if gold_index == -1:
    #     reason = "골든크로스 미발생"
    #     data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
    #     print(f"상태: {reason}")
    #     #return False, reason, "", data_dict
    
    # if bars_since_gold < 4:
    #     reason = f"골든크로스 후 {bars_since_gold}봉(4봉 미만, 필요:4봉 이상)"
    #     data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
    #     print(f"상태: {reason}")
    #     #return False, reason, "", data_dict
# ---------- [기존 유지 및 보강] 30분봉 기준 S+ 수급 ----------
    if len(df) >= 5:
        # 유의종목 차단
        if symbol.split('/')[0] in warning_list:
            return False, "유의종목차단(S+)", "", data_dict

        # [추가] 골크 전조 10봉 포함, 최근 15봉 내 최고가 계산 (설거지 방지용 기준점)
        max_peak_price = df['high'].iloc[-15:].max()

        avg_vol_5 = df['vol'].iloc[-7:-2].mean()
        volume_300 = (avg_vol_5 > 0 and float(curr['vol']) >= avg_vol_5 * 3)
        
        ############### 1. 과거 기준점 확보 (3개 전 종가)
        price_3bars_ago = float(df.iloc[-4]['close']) if len(df) >= 4 else 0
        
        ############### 2. 과거 대비 상승 폭 체크 (사용자님 기존 의도: 2% 이상)
        is_higher_than_past = (price_3bars_ago > 0 and (curr_price - price_3bars_ago) / price_3bars_ago >= 0.02)
        
        ############### 3. 현재 봉의 실시간 상태 체크 (폭락/음봉 방지)
        # 현재가가 시가보다 높거나 같아야 함 (최소한 도지나 양봉)
        is_not_falling = curr_price >= float(curr['open']) 
        # 혹은 직전 봉 종가보다 현재가가 높아야 함 (상승 추세 유지)
        is_trending_up = curr_price > float(prev['close'])
        
        ############### 4. 두 조건을 결합하여 최종 판정
        price_surge_2pct = is_higher_than_past and is_not_falling and is_trending_up
        
        # 30분봉 기준 과열 판단
        rsi_val = data_dict.get('rsi', 50) if data_dict else calculate_rsi(df).iloc[-1]

        if volume_300 and price_surge_2pct:
            # [수정] RSI 조건에 '고점 대비 5% 이탈 방지' 필터 결합
            if rsi_val < 50 and curr_price >= max_peak_price * 0.95:
                data_dict = _fill_data_dict_full(df, curr, prev, curr_price, symbol)
                data_dict['grade'] = 'S'
                return True, f"🔥 [S] 수급 급등(안전권 진입) - 세력 매집 의심", "S", data_dict
                
    # ---------- [공통] data_dict 전체 수치 채우기 (조건 탈락 여부와 관계없이) ----------
    ma40_val = float(curr['ma40']) if not pd.isna(curr['ma40']) else 0
    ma185_val = float(curr['ma185']) if not pd.isna(curr['ma185']) else 0
    rsi_val = float(curr['rsi']) if not pd.isna(curr['rsi']) else 50
    ma5_val = float(curr['ma5']) if not pd.isna(curr['ma5']) else None
    ma20_val = float(curr['ma20']) if not pd.isna(curr['ma20']) else None
    ma90_val = float(curr['ma90']) if not pd.isna(curr['ma90']) else None

    data_dict = _fill_data_dict_full(df, curr, prev, curr_price, symbol)

    is_was_descending = True  # 2일간 지속 하락 여부
    is_now_stabilized = True  # 5시간 전부터 안착 여부

    # 1단계: 과거 구간 (-96봉 ~ -10봉) 전수 조사 -> 모든 봉의 기울기가 -0.05 미만이어야 함
    if len(df) >= 97:
        for i in range(len(df)-96, len(df)-10):
            p_val, c_val = df['ma185'].iloc[i-1], df['ma185'].iloc[i]
            bar_slope = ((c_val - p_val) / p_val) * 100
            # 기울기 $Slope = \frac{C - P}{P} \times 100$
            if bar_slope > 0:  # 요동(반등)이나 완만한 구간이 단 하나라도 있으면 즉시 탈락
                is_was_descending = False
                break
    else: is_was_descending = False

    # 2단계: 현재 구간 (-10봉 ~ 현재) 전수 조사 -> 모든 봉의 기울기가 -0.05 이상이어야 함
    for i in range(len(df)-10, len(df)):
        p_val, c_val = df['ma185'].iloc[i-1], df['ma185'].iloc[i]
        bar_slope = ((c_val - p_val) / p_val) * 100
        if bar_slope < 0:  # 다시 하락세로 꺾이는 구간이 있으면 즉시 탈락
            is_now_stabilized = False
            break


    # 2. [기존 유지] 현재 기울기 수치
    diff_185 = (curr['ma185'] - prev['ma185']) / get_bithumb_tick_size(curr['ma185']) if get_bithumb_tick_size(curr['ma185']) else 0
    slope_rate = ((curr['ma185'] - prev['ma185']) / prev['ma185']) * 100 if prev['ma185'] and prev['ma185'] != 0 else 0
    data_dict['slope_rate'] = slope_rate
    # 185일선 대비 이격도(%): -5% 이하면 역추세 과매도 후보
    disparity_185_5 = (ma5_val - ma185_val) / ma185_val * 100 if ma185_val and ma185_val != 0 else 0
    disparity_90_40 = (ma40_val - ma90_val) / ma90_val * 100 if ma90_val and ma90_val != 0 else 0
    disparity_40_5 = abs(ma5_val - ma40_val) / ma40_val * 100 if ma40_val and ma40_val != 0 else 0
    data_dict['disparity_185_5'] = disparity_185_5
    data_dict['disparity_40_5'] = disparity_40_5

    # 3. [기존 유지] 안전장치: 급격한 수직 낙하만 방어 (중복 블록 제거: 아래 한 번만 유지)
    if diff_185 < -1.2:
        reason = f"185일선 급락(diff:{diff_185:.2f} < -1.2)"
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
        return False, reason, "", data_dict

    ###################  TYPE3 START ###################
    # 1. 기본 지표 및 과거 바닥(Oversold) 이력 확인
    ma5_val = float(curr['ma5']) if not pd.isna(curr['ma5']) else curr_price
    disparity_5_185 = (ma5_val - ma185_val) / ma185_val * 100 if ma185_val > 0 else 0
    
    prev_ma5 = float(prev['ma5'])
    prev_ma185 = float(prev['ma185'])
    prev_disparity = (prev_ma5 - prev_ma185) / prev_ma185 * 100 if prev_ma185 > 0 else 0

    # [핵심] 최근 30봉 이내에 RSI 40 이하를 찍으며 '바닥 초입'을 통과했었는지 확인
    was_oversold_start = (df['rsi'].iloc[-30:] <= 40).any()
    # 현재 봉(-1)을 제외한 직전 30봉 구간에서 단 한 번이라도 뚫었는지 확인
    has_prior_gc = (df['ma5'].iloc[-31:-1] > df['ma40'].iloc[-31:-1]).any()    

    # 2. 새끼 양봉 계산 (시가 대비 +0.0% ~ +2.0% 사이만 허용)
    candle_body_pct = ((curr_price - curr['open']) / curr['open'] * 100) if curr['open'] > 0 else 0
    target_disparity = -7.0 if slope_rate <= -0.035 else -4.0
    
    # [수정] 40선이 185선 아래여야 하며, 과거 바닥(RSI 40)을 통과한 종목을 추적 관리
    if ma40_val < ma185_val and was_oversold_start and data_dict.get('dynamic_rise_YN') != 'Y':
        if is_high_pos_185:
            return False, f"🚫 [TYPE3-제외] 185선 고점 구간({pos_185*100:.1f}%)", "B", data_dict

        # 가드 A: 5일선 이격도가 바닥권(-7%/-4%)이면서, 직전보다 수렴하고, 새끼 양봉일 때
        if disparity_5_185 <= target_disparity and disparity_5_185 > prev_disparity and -2.0 <= candle_body_pct <= 2.0:
            
            ###### [유지] 185선이 투매 수준으로 꺾이지 않았을 때만 진입 (slope_rate > -0.05)
            if slope_rate > -0.05:
                data_dict = _fill_data_dict_full(df, curr, prev, curr_price, symbol)
                curr_slope_40 = ((ma40_val - prev_ma40) / prev_ma40) * 100 if prev_ma40 > 0 else 0
                
                # [S급] 바닥권에서 5일선이 40선을 우상향 돌파 (강력 반등)
                # [추가] 격돌 판정을 위한 보조 지표 계산 (5선 기울기 및 5/40 이격도) ######
                ma5_slope = ((ma5_val - prev_ma5) / prev_ma5) * 100 if prev_ma5 > 0 else 0
                disparity_5_40 = ((ma5_val - ma40_val) / ma40_val) * 100 if ma40_val > 0 else 0
                 
                # [신규] 최근 5봉 이격도(5선-40선) 수렴 여부 체크 (5선이 40선에 점점 붙는지 확인)
                disps_5b = [abs(df['ma5'].iloc[-i] - df['ma40'].iloc[-i]) / df['ma40'].iloc[-i] * 100 if df['ma40'].iloc[-i] > 0 else 999 for i in range(1, 6)]
                is_converging_5b = all(disps_5b[i] < disps_5b[i+1] for i in range(4))

                # [수정] S급: 하한선(-0.5) 확장 + 상한선(0.03) 제한 + 5봉 수렴 조건(is_converging_5b) 추가
                if (not has_prior_gc) and (prev_ma5 <= prev_ma40) and (ma5_slope > 0) and (curr_slope_40 > -0.07) and (-0.8 < disparity_5_40 < 0) and is_converging_5b:
                    if  -0.5 <= disparity_5_40 < 0:
                        # [S+급] 40선 아래에서 격돌 중 (골크 전)
                        data_dict['grade'] = 'S+'
                        data_dict['multiplier'] = 2.0  # S급 이상일 때 2배 금액 배정
                        return True, f"💎 [TYPE3-S+] 골크 전 바닥 격돌 ({disparity_5_40:.2f}%)", "S+", data_dict
                    else:
                        # [S급] 40선 위로 막 돌파 (골크 후, 상한 0.5% 제한)
                        data_dict['grade'] = 'S'
                        return True, f"💎 [TYPE3-S] 골크 전 바닥 위치 ({disparity_5_40:.2f}%)", "S", data_dict
                else:
                    logger.info(f"DEBUG: {symbol} | [TYPE3-S] 탈락 이유 | 조건1(골크 미존재) :  {not has_prior_gc} and 조건2(prev_ma5-prev_ma40): {prev_ma5 - prev_ma40:.2f} <= 0 and 조건3(ma5_slope): {ma5_slope:.2f} > 0 and 조건4(curr_slope_40): {curr_slope_40 + 0.1:.2f} > 0 and 조건5(5/40 수렴): {is_converging_5b} and 조건6(이격): {disparity_5_40:.2f}")
                # [A급] 40선 기울기가 0 이상으로 전환 (추세 반전 확인)
                if curr_slope_40 >= 0:
                    data_dict['grade'] = 'A'
                    return True, f"🚀 [TYPE3-A] 바닥낚시 및 40선 추세 반전", "A", data_dict
                else:
                    logger.info(f"DEBUG: {symbol} | [TYPE3-A] 탈락 이유 | 조건1(curr_slope_40) : {curr_slope_40:.2f} > 0")    
                # [B급] 바닥낚시 조건은 만족하나 아직 돌파/반전 전 (알림용)
                data_dict['grade'] = 'B'
                return True, f"📢 [TYPE3-B] 바닥낚시 수렴 중 (알림)", "B", data_dict
    ###################  TYPE3 END ###################

    disparity_40 = abs(curr_price - curr['ma40']) / curr['ma40'] if curr['ma40'] > 0 else 999
    disparity_40_pct = disparity_40 * 100
    disparity_185 = abs(curr_price - curr['ma185']) / curr['ma185'] if curr['ma185'] > 0 else 999
    disparity_185_pct = disparity_185 * 100
    disparity_gold = abs(curr['ma40'] - curr['ma185']) / curr['ma185'] if curr['ma185'] > 0 else 999
    
    data_dict['disparity_40'] = disparity_40
    data_dict['disparity_40_pct'] = disparity_40_pct
    data_dict['disparity_185'] = disparity_185
    data_dict['disparity_185_pct'] = disparity_185_pct
    data_dict['disparity_gold'] = disparity_gold
    
    if rsi_val > 65:
        logger.info(f"DEBUG: {symbol} RSI 과열({rsi_val:.1f} > 65, 현재가:{curr_price:,.0f})")
        reason = f"RSI 과열({rsi_val:.1f} > 65, 현재가:{curr_price:,.0f})"
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
        return False, reason, "", data_dict

    # [정교화] 거래량 체크: 최근 20분 내 10% 이상 거래량 발생 여부
    base_period = 20
    recent_volumes = df['vol'].tail(base_period)
    base_avg_vol = recent_volumes.mean()
    
    recent_3bars = df['vol'].tail(3)
    has_volume_surge = False
    max_vol_ratio = 0
    for vol_val in recent_3bars:
        if base_avg_vol > 0:
            ratio = vol_val / base_avg_vol
            max_vol_ratio = max(max_vol_ratio, ratio)
            if ratio >= 1.1:
                has_volume_surge = True
    
    curr_vol = curr['vol']
    vol_ratio = (curr_vol / base_avg_vol) if base_avg_vol > 0 else 0
    
    data_dict['vol_ratio'] = vol_ratio
    data_dict['has_volume_surge'] = has_volume_surge
    data_dict['max_vol_ratio'] = max_vol_ratio

    # ==========================================================================
    # [TYPE1: 밥그릇 바닥 탈출 및 변곡점 포착]
    # 집중: 40선/185선 골든크로스 전후의 기울기 변화와 수렴도
    # ==========================================================================
        # 1. 40일선 위이거나, 아래라도 -3% 이내 근접 시 허용
    is_near_ma40 = abs(curr_price - ma40_val) / ma40_val <= 0.03 if ma40_val > 0 else False
    
    ###### [이식] 타점을 왼쪽으로 전진: 5일선-40일선 격돌(Collision) 로직 주입 ######
    ma5_slope = ((ma5_val - prev_ma5) / prev_ma5) * 100 if prev_ma5 > 0 else 0
    disparity_5_40 = ((ma5_val - ma40_val) / ma40_val) * 100 if ma40_val > 0 else 0
    # 5봉 연속 수렴 확인 (점점 선에 붙는 중인지 확인)
    disps_5b = [abs(df['ma5'].iloc[-i] - df['ma40'].iloc[-i]) / df['ma40'].iloc[-i] * 100 if df['ma40'].iloc[-i] > 0 else 999 for i in range(1, 6)]
    is_converging_5b = all(disps_5b[i] < disps_5b[i+1] for i in range(4))

        # 2. 이탈 방지: 40일선 기울기가 양수(우상향)이고 반드시 양봉 + 5일선 상승 중일 것
    # [교정] '하락 중 거래량 터짐' 오판 방지 및 방향성 확정
    is_upward_trend = (ma40_val > df['ma40'].iloc[-2]) and (ma5_slope > 0) and (curr_price >= curr['open'])

    if is_near_ma40 and is_upward_trend and disparity_40 <= 0.07 and data_dict.get('dynamic_rise_YN') != 'Y':
        ###### [신규] TYPE 1 전용: 185일선 밥그릇 흐름(지속 하락 후 안착) 전수 조사 ######
        strict_descending = True
        strict_stabilized = True

        # 1. 골든크로스 횟수 체크 (TYPE 1은 0회여야 함)
        gc_count_t1 = 0
        for i in range(1, 150):
            if i+1 < len(df):
                if df['ma40'].iloc[-i-1] <= df['ma185'].iloc[-i-1] and df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                    gc_count_t1 += 1
        stabilized_count = sum(1 for i in range(1, 11) if i+1 <= len(df) and df['ma185'].iloc[-i] >= df['ma185'].iloc[-i-1])
        # 2. 변곡점 포착: 최근 5봉은 상승 중이고, 10~5봉 전 구간은 하락/평행이었는지 확인
        is_turning_up = (df['ma185'].iloc[-1] > df['ma185'].iloc[-5]) and (df['ma185'].iloc[-10] <= df['ma185'].iloc[-5])

        if len(df) >= 281:
            descending_count = 0
            for i in range(len(df)-96, len(df)-10):
                p_v, c_v = df['ma185'].iloc[i-1], df['ma185'].iloc[i]
                if ((c_v - p_v) / p_v) * 100 <= 0: descending_count += 1
            strict_descending = True if descending_count >= 60 else False 
        else: strict_descending = False

        # [수정] GC가 0회이고 변곡점이거나 안정화되었을 때 통과
        strict_stabilized = (is_turning_up or (stabilized_count >= 0)) and (gc_count_t1 == 0)
        
        # 전수 조사를 통과한 깨끗한 밥그릇만 아래 등급 판정 진행
        if strict_descending and strict_stabilized:
            if is_high_pos_185:
                logger.info(f"DEBUG: {symbol} | [TYPE1] 탈락 이유-진입 실패 | 185선 고점 구간({pos_185*100:.1f}%)")
                return False, f"🚫 [TYPE1-제외] 185선 고점 구간({pos_185*100:.1f}%)", "B", data_dict
            
            data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
            
            if slope_rate >= -0.01:
                ###### [정밀화] 5/40 격돌(-0.8~0.1%) 시점에 S/S+ 등급 부여 (파란 동그라미 타점) ######
                if disparity_gold <= 0.005 and is_converging_5b and (-0.8 <= disparity_5_40 <= 0.1):
                    if is_40_90_gc:
                        # [S+급] 밥그릇 수렴 + 5/40 격돌 + 40/90 골든크로스 확정
                        data_dict['grade'] = 'S+'
                        return True, "💎 [TYPE1-S+] 밥그릇 수렴 및 40/90 골든크로스 확정", "S+", data_dict
                    else:
                        # [S급] 격돌 중이지만 40/90 골크는 아직인 최선행 타점
                        data_dict['grade'] = 'S'
                        return True, "💎 [TYPE1-S] 밥그릇 바닥 수렴 및 5/40선 안착 확인", "S", data_dict
                else:
                    logger.info(f"DEBUG: {symbol} | [TYPE1-S/S+] 격돌 실패 | 5/40이격: {disparity_5_40:.2f}% | 수렴도: {is_converging_5b}")

                # [A+급] 바닥 탈출 + 40/90 정배열 가속 (추세 가속 확인)
                # S+가 '골든크로스 순간'이라면, A+는 '정배열 유지 + 수급' 타점으로 차별화
                if is_40_above_90 and disparity_gold <= 0.015:
                    # 현재가가 40일선 위에서 안착하고 거래량이 전봉보다 실렸을 때
                    if curr_price > ma40_val and has_volume_surge:
                        data_dict['grade'] = 'A+'
                        return True, "⭐ [TYPE1-A+] 밥그릇 탈출 및 40/90 정배열 가속", "A+", data_dict

                # [A급] 밥그릇 바닥 탈출 (기존 A+)
                if disparity_gold <= 0.010:
                    data_dict['grade'] = 'A'
                    return True, "⭐ [TYPE1-A] 밥그릇 바닥 탈출(변곡점 확인)", "A", data_dict

                # [B+급] 185선 평행/우상향 전환 초기
                data_dict['grade'] = 'B+'
                return True, "🚀 [TYPE1-B+] 185선 평행/우상향 전환", "B+", data_dict
            else:
                logger.info(f"DEBUG: {symbol} | [TYPE1] 탈락 이유-진입 실패 | 185선 기울기 ({slope_rate} >= -0.01)")
            # [B급] 상승 대기 (골드 안착)
            data_dict['grade'] = 'B'
            return True, "🚀 [TYPE1-B] 상승대기(골드안착)", "B", data_dict
        else:
            logger.info(f"DEBUG: {symbol} TYPE1 탈락 이유 | 185선 96봉~10봉 내리막: :{strict_descending}({descending_count}개 하락), 185선 최근 10봉 우상향:{strict_stabilized}({stabilized_count} >= 0), gc횟수: {gc_count_t1}회")
    # ==========================================================================
    # [TYPE2: 눌림목 및 40선 지지 (에너지 응축)]
    # 집중: 골든크로스 이후 40선(노란선) 밀착 및 지지 확인
    # ==========================================================================
    if 4 <= bars_since_gold <= 144:  # 4봉전부터 144봉(3일)
        # 1. 역사적 순결성 (최근 150봉 내 40/185 골든크로스 딱 1번 & 185선 10봉 연속 유지 
        gc_count_150 = 0
        for i in range(1, 150):
            if i+1 < len(df):
                if df['ma40'].iloc[-i-1] <= df['ma185'].iloc[-i-1] and df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                    gc_count_150 += 1
        
        # 2. 185선 10봉 안정성 (최근 10봉 중 80% 이상 ma185가 유지/상승)
        # 변수명은 기존 그대로 유지하여 NameError 방지
        is_185_5bar_stable = sum(1 for i in range(1, 11) if df['ma185'].iloc[-i] >= df['ma185'].iloc[-i-1]) >= 8
        
        ###### [교정] 144봉 전수 조사 대신 '골든크로스 발생 지점'만 정밀 타격 ######
        has_t1_history = False

        if gc_count_150 == 1:
            gc_idx = -1
            # 1. 최근 150봉 이내에서 실제 골든크로스가 일어난 정확한 위치(Index)를 찾음
            for i in range(1, 150):
                if i+1 < len(df):
                    if df['ma40'].iloc[-i-1] <= df['ma185'].iloc[-i-1] and df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                        gc_idx = len(df) - i
                        break
            
            # gc_idx >= 281 제약을 삭제하고, GC 이전에 충분한 데이터가 있는지만 확인
            if gc_idx != -1 and gc_idx > 150:
                # [추가] 높이 필터: 185선 바닥 대비 현재가가 8% 이상 높으면 상투(B급)로 강등
                height_pct = (curr_price - ma185_val) / ma185_val * 100
                is_low_altitude = height_pct <= 8.0
                
                d_cnt = sum(1 for k in range(gc_idx-96, gc_idx-10) if df['ma185'].iloc[k] <= df['ma185'].iloc[k-1])
                
                # [수정] 기존 s_cnt 폐기 -> 하락 밀도(Drop Density) 5틱 이하 기준 도입
                # [수정] 틱 사이즈는 현재 185일선 가격 기준으로 산출
                t1_v2_tick = get_bithumb_tick_size(ma185_val)

###### [수정 시작] 185선 자체의 하락폭 계산 (가격 노이즈 제거) ######
                # 가격(Close)의 변동이 아니라, 185일선의 순수 하락폭(11봉전 vs 현재)을 계산
                # 185선은 매우 무거워서 10봉(5시간) 동안 몇 틱 이상 변하기 힘듭니다.
                t1_v2_drop = max(0, df['ma185'].iloc[-11] - ma185_val)
                
                # 185선이 5시간 동안 '3틱' 이하로만 하락했다면 '안착(Stable)'으로 판정
                is_t1_v2_drop_stable = (t1_v2_drop / t1_v2_tick <= 3) if t1_v2_tick > 0 else False
                
                if d_cnt >= 60 and is_t1_v2_drop_stable and is_low_altitude:
                    has_t1_history = True
                else:
                    logger.info(f"DEBUG: {symbol} | [TYPE2] 최근10봉 185 미하락 탈락 이유 | 조건1(틱 크기) : {t1_v2_tick} and 조건2(최근10봉 하락 밀도): {t1_v2_drop} and 밀도 계산:{t1_v2_drop / t1_v2_tick} <= 5")
                    logger.info(f"DEBUG: {symbol} | [TYPE2] T1존재여부 탈락 이유 | 조건1(185선 내리막 비중) : {d_cnt} >= 60 and 조건2(최근10봉 185 미하락): {is_t1_v2_drop_stable} and 185선 바닥 대비 현재가:{height_pct}% <= 8")

        # 기존 변수명(is_185_5bar_stable)을 사용하여 조건문 구성
        if gc_count_150 == 1 and has_t1_history:
            # 40일선 기울기 가속도 및 이격도 계산
            prev_ma40_2 = df['ma40'].iloc[-3]

            prev_slope_40 = ((prev_ma40 - prev_ma40_2) / prev_ma40_2) * 100 if prev_ma40_2 > 0 else 0
            curr_slope_40 = ((ma40_val - prev_ma40) / prev_ma40) * 100 if prev_ma40 > 0 else 0
            dis_gold_pct = (ma40_val - ma185_val) / ma185_val * 100 if ma185_val > 0 else 0
            ###### [수정 시작: TYPE2 전용 설정 변수 및 안전 가드 계산] ######
            # 1. 설정값 관리 (불기둥/윗꼬리/신선도 기준)
            TYPE2_FRESH_LIMIT = 64     # 신선도 유효 기간 (32시간 = 64봉)
            TYPE2_VOL_LIMIT  = 10.0    # 구간 내 허용 최대 변동폭 (%)

            # 2. 64봉 구간 변동성(과열) 계산
            window_data = df.iloc[-TYPE2_FRESH_LIMIT:]
            vol_sectional = ((window_data['high'].max() - window_data['low'].min()) / window_data['low'].min()) * 100 if window_data['low'].min() > 0 else 0
            
            # 3. 90선 추세 및 지지/신선도 확인
            # 90선 기울기 밀도 계산 (최근 10봉 중 상승 횟수)
            ma90_up_count = sum(1 for i in range(1, 11) if df['ma90'].iloc[-i] > df['ma90'].iloc[-i-1])
            is_fresh = (bars_since_gold <= TYPE2_FRESH_LIMIT)  # 36시간 이내 (신선도)

            # 4. 최종 안전 가드 결합 (과열 여부, 신선도)
            is_type2_safe = (vol_sectional <= TYPE2_VOL_LIMIT) and is_fresh
            ###### [수정 끝] ######
            ###### 185선 상태 및 이격도 수렴 여부 (핵심 전제)
            is_185_stable = ma185_val >= df['ma185'].iloc[-2]
            
            # 조건: 이격도 ±1.0% 이내 + 5일선 우상향 + 현재가 양봉 필수
            # (과열 + 신선도) + 185, 40 이격도 + 5 기울기 상승 + (도지, 양봉)
            if is_type2_safe and abs(dis_gold_pct) <= 1.0 and ma5_slope > 0 and curr_price >= curr['open']:
                # 90선 기울기 (평행+상향) 4개 이상
                if ma90_up_count >= 4:
                    grade = "S+" if curr_price >= ma185_val else "S"
                    data_dict['grade'] = grade
                    return True, f"💎 [TYPE2-{grade}] 90선 기울기 유지 우상향 전환", grade, data_dict
                else:
                    logger.info(f"DEBUG: {symbol} | [TYPE2] S급 진입 탈락 이유 | 조건1(90선 기울기 횟수) : {ma90_up_count} >= 4 and 조건2(185선 대비 종가 위치(양수)): {curr_price - ma185_val} ")

                grade = "A+" if is_40_90_gc else "A"
                data_dict['grade'] = grade
                return True, f"🚀 [TYPE2-{grade}] 5일선 반등 시작", grade, data_dict

        else: 
            logger.info(f"DEBUG: {symbol} | [TYPE2] 초기 진입 탈락 이유 | 조건1(150봉동안 골크 횟수) : {gc_count_150} == 1 and 조건2(최근5봉 185 미하락): {is_185_5bar_stable} and 3일(144봉)동안 T1존재여부:{has_t1_history} ")
    # else: 
    #     logger.info(f"DEBUG: {symbol} | TYPE2 탈락 이유-진입 실패: bars_since_gold={bars_since_gold}")
    # [B등급] 급등 후 거래량이 줄어들며 20일선에서 지지받는 눌림목: 현재가가 ma20 근처이고 거래량 감소 시 B
    # if ma20_val and base_avg_vol and curr_vol < base_avg_vol * 0.9 and abs(curr_price - ma20_val) / ma20_val <= 0.03 and data_dict.get('dynamic_rise_YN') != 'Y':
    #     data_dict['grade'] = 'B'
    #     data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
    #     return True, "📌 [B] 눌림목(20일선 지지)", "B", data_dict

    # 최종 탈락 사유 판단 (모든 수치·패턴 라벨 기록 후 반환)
    data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
    # S+급 등이 확정되었으나 현재가가 40선 밑에 있어 하락세가 우려되는 경우 보완
    if curr_price <= curr['ma40'] and data_dict.get('grade') in ['S+', 'S', 'A+']:
         data_dict['grade'] = 'A' # 등급 하향
         # 기존 reason 뒤에 하락세 경고 문구 추가
    if 'grade' in data_dict and data_dict['grade'] in ['S', 'S+', 'A', 'A+', 'B']:
        print(f"🎯 [추천성공] {symbol} | 등급:{data_dict['grade']} | 구간Low:{win_low:,.0f} | 구간High:{win_high:,.0f} | 변동:{dynamic_rise:.2f}%")    
    if curr_price <= curr['ma40']:
        reason = f"현재가({curr_price:,.0f}) ≤ 40일선({ma40_val:,.0f}, 이격도:{disparity_40_pct:.2f}%)"
        return False, reason, "", data_dict
    
    if disparity_40 > 0.07:
        reason = f"40일선 이격도 과다({disparity_40_pct:.2f}% > 7%, 현재가:{curr_price:,.0f}, 40일선:{ma40_val:,.0f})"
        return False, reason, "", data_dict
    
    reason = f"기타 조건 불만족(현재가:{curr_price:,.0f}, 40일선:{ma40_val:,.0f}, 이격도:{disparity_40_pct:.2f}%)"
    return False, reason, "", data_dict


def check_3_2_negative_candles(target_df):
    if len(target_df) < 4: return False, ""
    recent_3 = target_df.tail(3)
    # 최근 10봉 중 최대 거래량의 10%를 이탈 기준으로 설정
    high_vol_threshold = target_df['vol'].tail(10).max() * 0.1
    
    neg_count = 0
    reasons = []
    for i in range(3):
        candle = recent_3.iloc[i]
        # 음봉이면서 기준 거래량 이상 터졌을 때만 '찐 음봉' 인정
        if candle['close'] < candle['open'] and candle['vol'] > high_vol_threshold:
            neg_count += 1
            reasons.append(f"{3-i}번전음봉")
    
    if neg_count >= 2:
        return True, ", ".join(reasons)
    return False, ""



# ---------------------------------------------------------
# [복구 및 추가] 매도 감시 메인 함수 (ERROR 방지 핵심)
# ---------------------------------------------------------
async def check_sell_signal(exchange, df, symbol, purchase_price, max_price=0, grade='A', symbol_inventory_age=99, status=None, realtime_p=None, buy_type=1):
    global emergency_mode
    
        # 1. 실시간 현재가 확정
    temp_curr = df.iloc[-1]
    curr_p = realtime_p if realtime_p is not None else float(temp_curr['close'])

    # 2. [핵심] 실시간 가격 주입 및 모든 지표 재계산
    df.loc[df.index[-1], 'close'] = curr_p
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma40'] = df['close'].rolling(40).mean()
    df['ma90'] = df['close'].rolling(90).mean()
    df['ma185'] = df['close'].rolling(185).mean()
    rsi_series = calculate_rsi(df)

    # 3. [중요] 지표가 반영된 최신 행(row) 재정의 (이걸 안 하면 지표가 과거값임)
    curr = df.iloc[-1] 
    prev = df.iloc[-2]
    
    # 4. 주요 수치 변수 할당 (중복 제거)
    ma5_val = float(curr['ma5'])
    prev_ma5 = float(prev['ma5'])
    ma40_val = float(curr['ma40'])
    high_price = max(max_price, df['high'].tail(6).max())
    
    # 5. 수익률 및 RSI 스파이크 계산 (딱 한 번만 실행)
    has_rsi_spike = (rsi_series.tail(14) >= 80).any()
    profit_rate = (curr_p - purchase_price) / purchase_price if purchase_price > 0 else 0
    profit_rate_pct = profit_rate * 100
    max_profit_rate_pct = ((high_price - purchase_price) / purchase_price * 100) if purchase_price > 0 else 0

    # 6. 본절방어 기준점(틱 사이즈 반영) 계산
    one_tick_pct = (get_bithumb_tick_size(purchase_price) / purchase_price * 100) if purchase_price > 0 else 0
    profit_threshold = max(0.5, 2 * one_tick_pct)
    
    # [1단계] 최우선 생존: 패닉 여부 상관없이 -3% 도달 시 즉시 탈출 (Emergency=True)
    if profit_rate_pct <= -3.0:
        return True, f"🚨 [절대손절] 진입가 대비 -3% 도달 (즉시)", True

    # [2단계] 패닉 대응: 상승 시 보류, 횡보/하락 시 유예 매도 (Emergency=False)
    if is_buy_locked:
        if curr_p > float(prev['close']):
            # 상승 중이면 패닉이라도 수익을 위해 일단 홀딩 (유예)
            return False, "🚀 [패닉유예] 상승 흐름 유지 중 (매도 보류)", False
        else:
            # 횡보/하락 시 팔겠다는 의사(True)를 주고, 즉시 탈출
            return True, f"⚠️ [패닉매도발생] 시장 폭락 및 반등 실패로 인한 정리", True

    ###### [출력] 유예 로직 통과 여부 확인 ######
    # print(f"DEBUG: {symbol} | age: {symbol_inventory_age} | profit: {profit_rate_pct:.2f}%")
    logger.info(f"DEBUG: {symbol} | age: {symbol_inventory_age} | profit: {profit_rate_pct:.2f}%")
    ###### [신규 추가] 매수 후 6캔들 유예: 진입 초기 휩소 방지 (단, -3% 손절은 즉시 집행)
    try:
        current_age = int(symbol_inventory_age)
    except (ValueError, TypeError):
        current_age = 99  # 변환 실패 시 유예 기간을 통과하도록 안전값 설정
    # [신규] 인벤토리에 없거나 99인 경우, 빗썸 API에서 실제 매수 시점을 찾아 age 복구
    if current_age >= 90:
        try:
            # 최근 거래 내역 중 마지막 'buy' 기록의 시간을 가져옴
            trades = await asyncio.to_thread(exchange.fetch_my_trades, symbol, limit=10)
            buy_trades = [t for t in trades if t['side'] == 'buy']
            if buy_trades:
                last_buy_time = datetime.fromtimestamp(buy_trades[-1]['timestamp'] / 1000)
                diff_min = (datetime.now() - last_buy_time).total_seconds() / 60
                current_age = int(diff_min // 30) # 30분봉 기준 age 변환
                logger.info(f"🔍 [수동매수감지] {symbol} 매수 이력 확인: {current_age}봉 경과")
        except Exception as e:
            logger.error(f"⚠️ {symbol} 매수 이력 조회 실패: {e}")    
    if current_age < 6 and profit_rate_pct < 1.2:
        return False, f"진입 초기 유예({current_age}봉)", False

        # 1. 본절 방어 (수정된 동적 기준 적용)
    if profit_threshold <= max_profit_rate_pct < 1.2 and purchase_price <= curr_p <= purchase_price * 1.005:
        cooldown_dict[symbol] = datetime.now() + timedelta(hours=6)
        return True, f"🛡️ [S-TS-0.5] 본절방어(즉시)", True

    ma40_val = curr['ma40']
    ma185_val = curr['ma185'] if not pd.isna(curr['ma185']) else 0
    # 최근 20봉 중 ma40의 기울기가 가장 완만했던 구간의 가격을 지지선으로 설정
    parallel_window = df.iloc[-20:]
    support_idx = (parallel_window['ma40'].diff().abs()).idxmin()
    support_price = df.loc[support_idx, 'ma40']


    ###### [수정] 통합 비상 레벨 판정 및 고성능 3분봉 엔진 (기존 로직 100% 보존) ######
    ma5_val, ma40_val, ma185_val = float(curr['ma5']), float(curr['ma40']), float(curr['ma185'])
    is_type3_stable = (str(buy_type) == '3' and ma5_val > ma40_val and ma5_val >= prev_ma5)
    prev_ma5, prev_ma40, prev_ma185 = float(prev['ma5']), float(prev['ma40']), float(prev['ma185'])
    soaring_rate = (curr_p - curr['open']) / curr['open'] * 100
    rsi_val = rsi_series.iloc[-1]
    
    # 레벨 상태 로드 및 갱신
    old_lvl = emergency_mode.get(symbol, 0)
    if old_lvl is True: old_lvl = 2 # 하위 호환성 보정
    
    # 타입별 판정용 변수 계산
    df_3m = None
    is_3m_below_ma40 = False

        # Level 1 이상일 때만 실시간 3분봉 체크
    if old_lvl >= 1 or profit_rate_pct >= 10.0:
        try:
            ohlcv_3m = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '3m', limit=50)
            df_3m = pd.DataFrame(ohlcv_3m, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            df_3m['ma40'] = df_3m['close'].rolling(40).mean()
            if df_3m['close'].iloc[-1] < df_3m['ma40'].iloc[-1]:
                is_3m_below_ma40 = True
        except Exception as e:
            logger.error(f"3분봉 데이터 조회 실패: {e}")

    ma40_slope = (ma40_val - prev_ma40) / prev_ma40 if prev_ma40 > 0 else 0
    is_converging = abs(ma40_val - ma185_val) < abs(prev_ma40 - prev_ma185)
    
    new_lvl = get_updated_emergency_level(
        symbol, old_lvl, buy_type, rsi_val, is_3m_below_ma40, 
        ma40_slope, is_converging, profit_rate_pct, soaring_rate, 
        has_rsi_spike, max_profit_rate_pct, is_type3_stable
    )
    emergency_mode[symbol] = new_lvl

    # Level 1, 2 모두 3분봉 엔진 가동 (사용자 요청: 하락 시 즉시 대응)
    if new_lvl >= 1 and df_3m is not None:
        logger.info(f"DEBUG: 🔥 [L{new_lvl} 엔진 가동] {symbol} | 수익: {profit_rate_pct:.2f}%")
        try:
            # [기존 ATOM 사례 대응 로직 유지]
            vol_30m = (df['high'].tail(3).max() - df['low'].tail(3).min()) / df['low'].tail(3).min() * 100
            if vol_30m < 1.2 and profit_rate_pct > 7.0:
                if new_lvl == 2: emergency_mode[symbol] = 1 # 안정적 우상향 시 레벨 다운 유도
            else:
                high_10_3m = df_3m['high'].tail(10).max()
                curr_3m_p, curr_3m_data = df_3m['close'].iloc[-1], df_3m.iloc[-1]

                # (1) [기존] 고점 직전봉 허리(50%) 실시간 이탈 체크
                abs_peak_idx = len(df_3m) - 10 + df_3m['vol'].tail(10).argmax()
                peak_candle = df_3m.iloc[abs_peak_idx]
                if peak_candle['close'] >= peak_candle['open'] and abs_peak_idx > 0:
                    prev_mid_p = (df_3m.iloc[abs_peak_idx-1]['open'] + df_3m.iloc[abs_peak_idx-1]['close']) / 2
                    if curr_3m_data['low'] < prev_mid_p and curr_3m_data['vol'] > peak_candle['vol']:
                        if curr_3m_data['close'] < curr_3m_data['open']:
                            return True, f"🚨[50%긴급-L{new_lvl}] 실시간 허리 이탈", True

                # (2) [기존] 세력 이탈 감지 (2음봉 + 거래량)
                is_2_neg_3m, reason_2_neg_3m = check_3_2_negative_candles(df_3m)
                if is_2_neg_3m:
                    high_idx_3m = df_3m['high'].tail(10).argmax()
                    high_vol_3m = df_3m['vol'].iloc[len(df_3m) - 10 + high_idx_3m]
                    if (df_3m['vol'].iloc[-1] + df_3m['vol'].iloc[-2]) > (high_vol_3m * 0.1):
                        return True, f"🚨[세력이탈-L{new_lvl}] 2음봉 & 거래량포착", True

                # (3) [기존 로직 100% 유지] 위치별 차등 낙폭
                if soaring_rate >= 10.0:
                    if curr_3m_p < high_10_3m * 0.97:
                        return True, f"🚨[급등-강력] L{new_lvl} 고점대비 3% 하락 (위치:{soaring_rate:.1f}%)", True
                else:
                    if curr_3m_p < high_10_3m * 0.98:
                        return True, f"🚨[급등-초기] L{new_lvl} 고점대비 2% 하락 (위치:{soaring_rate:.1f}%)", True

                # (4) [기존] 수익률 13% 마지노선 사수
                if profit_rate_pct >= 13.0 and curr_3m_p <= purchase_price * 1.13:
                    return True, f"🚨[13%사수-L{new_lvl}] 마지노선 매도", True

            return False, f"🚀 비상 감시(L{new_lvl}) 유지 중", False
        except Exception as e:
            logger.error(f"비상 엔진 에러 (L{new_lvl}): {e}")


    if new_lvl == 0:   # 일반 모드(Level 0)인 경우에만 30분봉 로직 수행
        logger.info(f"DEBUG: {symbol} | 일반 매도 모드")
        current_type = str(buy_type)

        if current_type in ['1', '2', '3']:
        ####### [추가] TYPE3 예외 처리: 30분봉 지표(90선/지지선) 로직 진입 차단 #######
            logger.info(f"DEBUG: {symbol} | TYPE: {buy_type} | 매도 조건 탐지 시작")

            drop_from_peak = ((high_price - curr_p) / high_price * 100) if high_price > 0 else 0

            ########## [수정 시작] 최고 수익률을 기준으로 감시 구간을 고정하여 급락 시 이탈 방지 ##########
            max_profit_rate_pct = ((high_price - purchase_price) / purchase_price * 100) if purchase_price > 0 else 0

            # 1. 본절 방어 (1.2% 미만)
            # if 0.5 <= max_profit_rate_pct < 1.2:
            #     if curr_p <= purchase_price * 1.005:
            #         return True, f"🛡️ [S-TS-0.5] 본절방어", False

            # 2. 익절 구간 A (1.2% ~ 2.0% 미만): 고점 대비 1% 하락 시 매도
            if 1.2 <= max_profit_rate_pct < 2.0:
                if drop_from_peak >= 1.0:
                    return True, f"💰 [S-TS-1.2] 익절 A (낙폭 1.0%)", True

            # 3. 익절 구간 B (2.0% ~ 3.5% 미만): 고점 대비 1.5% 하락 시 매도
            elif 2.0 <= max_profit_rate_pct < 3.5:
                if drop_from_peak >= 1.5:
                    return True, f"💰 [S-TS-2.0] 익절 B (낙폭 1.5%)", True

            # 4. 익절 구간 C (3.5% 이상): 고점 대비 3.0% 하락 시 즉시 매도
            elif max_profit_rate_pct >= 3.5:
                if drop_from_peak >= 3.0:
                    return True, f"🚀 [S-TS-3.5] 익절 C (낙폭 3.0%)", True

            # ---------------------------------------------------------
            # [정비 2] 40 지지선 및 S+급 보호 (상향->평행->상향 로직)
            # ---------------------------------------------------------
            # 40 지지선 이탈 로직 (수익이 안정화된 후 큰 추세를 먹기 위한 용도)
            if 'support_price' in locals() and curr_p < support_price and drop_from_peak >= 2:
                if profit_rate_pct > 0: # 수익권일 때만 지지선 이탈 적용 등 조건 추가 가능
                    return True, f"📉 [지지선 매도] 40지지선 이탈", False

            # 90선 독립 마지노선 (40선 지지선 부재 시) ######
            # 40선 지지선이 현재가보다 5% 이상 멀리 있다면 신뢰할 수 없으므로 90선을 즉시 체크
            disparity_40 = (curr_p - support_price) / support_price * 100 if support_price > 0 else -99
            if (support_price == 0 or disparity_40 < -5.0) and curr_p < curr['ma90'] and drop_from_peak >= 2:
                return True, f"📉 [지지선 매도] 40지지선 부재 또는 이격 과다로 90선 최종 이탈", False

            # S+ 상승 초입(-2% ~ +2%) 보호
            is_early_stage = -2.0 < profit_rate_pct < 2.0
            
            # 40선 지지선 매도 판정
            if curr_p < support_price and drop_from_peak >= 1.5:
                # 상승 초입 눌림목(지지선의 98%)은 유예해줌
                if not (is_early_stage and curr_p >= support_price * 0.98):
                    return True, f"📉 40선 지지선({support_price:,.2f}) 이탈", False
    # ---------------------------------------------------------
    ######### [급등 모드 : 수익 10% 이상이거나 RSI 80 이상이면 3분봉 정밀 감시 가동] #########
    # ---------------------------------------------------------
    if (profit_rate_pct >= 10.0 or emergency_mode.get(symbol, False)) and df_3m is not None:
        try:
            ###### [수정] 엔진 가동 사유 레이블링 정교화 ######
            if profit_rate_pct >= 10.0:
                mode_reason = "수익률 10%↑"
            elif str(buy_type) == '3':
                mode_reason = "TYPE3 정밀감시" # ###### [변경] 더 이상 RSI 과열로 오해하지 않음 ######
            else:
                mode_reason = "RSI 과열"
            # print(f"🔥 [3분봉 엔진 가동] {symbol} | 사유: {mode_reason} | 현재가: {curr_p:,.0f}")
            logger.info(f"DEBUG: 🔥 [3분봉 엔진 가동] {symbol} | 사유: {mode_reason} | 현재가: {curr_p:,.0f}")
            curr_3m = df_3m.iloc[-1]

            # A. 3분봉 기준 2음봉 세력 이탈 감지
            is_2_neg, reason_2_neg = check_3_2_negative_candles(df_3m)
            if is_2_neg:
                return True, f"🚀 [비상-3m] 세력 이탈: {reason_2_neg}", True
            
            # B. 3분봉 기준 40선 이탈 (천장 대책 - 익절)
            if curr_3m['close'] < curr_3m['ma40']:
                return True, f"💰 [비상-3m] 3분봉 40선 이탈 (익절)", True

            # C. 3분봉 기준 고점 대비 3% 하락 (수익 보전)
            high_3m = df_3m['high'].tail(15).max()
            if curr_3m['close'] < high_3m * 0.97:
                return True, f"🚨 [비상-3m] 고점 대비 3% 하락", True

            # 급등 상황이면 아래 30분봉 일반 로직은 건너뛰고 홀딩
            return False, "🚀 급등 모드 유지 (3분봉 추적 중)", False
        except Exception as e:
            logger.error(f"3분봉 분석 에러: {e}")
            # 에러 시에는 안전하게 기존 30분봉 로직으로 흐르게 둡니다.

   

    # ---------------------------------------------------------
    # [정비 4] 기존 유예 로직 및 기타 매도
    # ---------------------------------------------------------
    # [S급 털림 방지] 급등 진행 중 매도 유예 (수익 10% 이상 & 정배열 시)
    if ma185_val > 0:
        is_ma40_above_ma185 = ma40_val > ma185_val
        if curr_p > ma40_val and is_ma40_above_ma185 and profit_rate_pct >= 10.0:
            return False, "급등 진행 중(매도 유예)", False

    # 상태 유지(KEEP) 중일 때 긴급 매도 외 일반 매도 차단
    if status == 'KEEP':
        return False, "유지 중", False

    # [매도 2순위] 지지선 이탈 (사용자님 제안: support_price 기준)
    # 현재가가 계산된 지지선(기울기 완만했던 ma40)을 하향 돌파할 때
    # if curr_p < support_price:
    #     # 지지선이 현재가 대비 너무 아래(-5% 이하)에 있다면 신뢰도가 낮음
    #     disparity_support = (curr_p - support_price) / support_price * 100 if support_price > 0 else 0
        
    #     if disparity_support < -5.0:
    #         # 지지선이 너무 멀면 90일선을 최종 마지노선으로 확인 (중복 제거 통합)
    #         if curr_p < curr['ma90']:
    #             return
    #             , "📉 [최종이탈] 지지선 이격 과다로 90선 최종 이탈 매도", False
    #     else:
    #         # 일반적인 지지선 하향 이탈
    #         return True, f"📉 [지지선이탈] 현재가({curr_p:.2f})가 설정 지지선({support_price:.2f})을 하향 이탈", False

    # [매도 3순위] 수익 보전 (수익률 3% 이상 시 지지선 근접하면 미리 익절)
    # support_price의 1.01배(1% 위)까지 내려오면 수익을 지키기 위해 매도
    if profit_rate_pct >= 3.0 and curr_p < support_price * 1.01:
        return True, "✅ [익절보전] 3% 수익권 내 지지선 근접 매도", False

    return False, "안전", False


def get_report_visuals(this_profit, is_sell_signal, this_curr_p, ma40_val, sell_reason, symbol, pending_approvals):
    from datetime import datetime
    wait_data = pending_approvals.get(symbol)
    
    # [1] 유예 및 긴급 상태 (파랑/🚨)
    if wait_data and wait_data.get('status') in ['WAITING', 'NOTIFIED']:
        elapsed = (datetime.now() - wait_data['start_time']).total_seconds() / 60
        limit = wait_data.get('wait_limit', 30)
        remains = max(0, int(limit - elapsed))
        
        # 긴급 판단(2음봉, 급락 등)은 사이렌(🚨) 고정, 일반은 파랑(🔵)
        is_urgent = ("🚨" in wait_data.get('last_icon', '') or "급등" in sell_reason or "2음봉" in sell_reason)
        icon = "🚨" if is_urgent else "🔵"
        msg = "긴급매도유예" if is_urgent else "일반매도유예"
        return icon, f"⏳ {remains}m 후 {msg}"

    # [2] 매도 신호 발생 (빨강 - 위험 신호)
    if is_sell_signal:
        return "🔴", f"⚠️ 매도신호({sell_reason})"

    # [3] 40선 하단 (노랑 - 주의 단계)
    # 현재가가 40선 아래여도 수익률이 -0.5% 이상(본절 근처)이면 초록색 유지
    if this_curr_p < ma40_val:
        if this_profit > -0.5:
             return "🟢", f"✅ 차트양호({symbol})"
        return "🟡", f"⚠️ 40선 하단({symbol})"

    # [4] 차트 양호 (초록 - 홀딩/안전 신호)
    # 매도 신호가 없고 40선 위라면 수익률과 관계없이 초록색으로 표시
    return "🟢", f"✅ 차트양호(홀딩)"
