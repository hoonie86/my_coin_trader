import asyncio
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime
from datetime import datetime, timedelta
from config import logger
import os
import json

MARKET_STATUS_FILE = '/home/rocky/my_coin_trader/market_status.json'

# ###### 수정 시작: 파일 로드/저장 누적 변수 추가 ######
def load_market_status():
    if os.path.exists(MARKET_STATUS_FILE):
        try:
            with open(MARKET_STATUS_FILE, 'r') as f:
                data = json.load(f)
                cleared_time = data.get('panic_cleared_time')
                if cleared_time:
                    cleared_time = datetime.fromisoformat(cleared_time)
                return data.get('is_buy_locked', False), data.get('market_ref_rate', 0.0), cleared_time, data.get('prev_day_offset', 0.0), data.get('last_date', datetime.now().strftime('%Y-%m-%d')), data.get('last_current_avg', 0.0)
        except Exception as e:
            logger.error(f"시장 상태 파일 로드 오류: {e}")
    return False, 0.0, None, 0.0, datetime.now().strftime('%Y-%m-%d'), 0.0

def save_market_status():
    try:
        data = {
            'is_buy_locked': is_buy_locked,
            'market_ref_rate': market_ref_rate,
            'panic_cleared_time': panic_cleared_time.isoformat() if panic_cleared_time else None,
            'prev_day_offset': prev_day_offset,
            'last_date': last_date,
            'last_current_avg': last_current_avg
        }
        with open(MARKET_STATUS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"시장 상태 파일 저장 오류: {e}")

is_buy_locked, market_ref_rate, panic_cleared_time, prev_day_offset, last_date, last_current_avg = load_market_status()
# ###### 수정 끝 ######

# --- [수정/추가 끝] ---

panic_msg_sent = False
cooldown_dict = {}

def is_in_cooldown(symbol):
    if symbol in cooldown_dict:
        if datetime.now() < cooldown_dict[symbol]:
            return True
    return False
    
# ###### 수정 시작: 시장 락 누적 합산 및 리턴 신호 복구 ######
async def update_market_panic_status(current_avg, symbol_count=0):
    global market_ref_rate, is_buy_locked, panic_cleared_time, prev_day_offset, last_date, last_current_avg

    if symbol_count < 10:
        return False, None

    current_date_str = datetime.now().strftime('%Y-%m-%d')
    
    if current_date_str != last_date:
        prev_day_offset += last_current_avg
        last_date = current_date_str
    
    last_current_avg = current_avg
    total_avg = current_avg + prev_day_offset
    
    if not is_buy_locked and total_avg <= -3.0:
        is_buy_locked = True
        market_ref_rate = total_avg
        save_market_status()
        logger.info(f"🚨 [시장잠금] 패닉 감지 (기준점: {market_ref_rate:.2f}%)")
        return True, f"🚨 [시장잠금] 패닉 상태 감지\n기준점: {market_ref_rate:.2f}%\n현재 모든 매수가 중단됩니다."

    elif is_buy_locked:
        if market_ref_rate <= -5.0:
            threshold = 2.0
        elif market_ref_rate <= -3.0:
            threshold = 1.5
        else:
            threshold = 1.0

        if total_avg >= market_ref_rate + threshold:
            is_buy_locked = False
            panic_cleared_time = datetime.now()
            market_ref_rate = total_avg
            prev_day_offset = 0.0
            last_current_avg = current_avg
            save_market_status()
            logger.info(f"✅ 시장 반등 확인({threshold}%): 매수 잠금 해제 (기준점: {market_ref_rate:.2f}%)")
            return True, f"✅ 시장 반등 확인({threshold}%): 매수 잠금 해제 (기준점: {market_ref_rate:.2f}%)"
        
        elif total_avg < market_ref_rate:
            market_ref_rate = total_avg
            save_market_status()
            logger.info(f"📉 시장 바닥 갱신: 기준점 하향 조정 ({market_ref_rate:.2f}%)")
            return True, f"📉 시장 바닥 갱신: 기준점 하향 조정 ({market_ref_rate:.2f}%)"

    else:
        if total_avg <= market_ref_rate - 2.0:
            is_buy_locked = True
            market_ref_rate = total_avg
            save_market_status()
            logger.info(f"🚨 [재잠금] 데드캣 방지 필터 작동 (기준점: {market_ref_rate:.2f}%)")
            return True, f"🚨 [재잠금] 데드캣 방지 필터 작동\n기준점: {market_ref_rate:.2f}%"

    save_market_status()
    return False, None
# ###### 수정 끝 ######

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
        if not isinstance(res, dict):
            logger.warning("⚠️ [입구 컷] get_warning_list: 비정상 응답 (정기점검 의심)")
            return []
            
        data = res.get('data', {})
        if not isinstance(data, dict):
             return []
        
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
    buy_type = int(buy_type)
    
    # [교정] 타입별 하향(2->1) 조건 정의
    is_recovering_general = (buy_type != 3 and is_3m_below_ma40)
    is_recovering_type3 = (buy_type == 3 and is_type3_stable)

    # 1. 트리거 (0 -> 2): 진입 조건 (기존 유지)
    if current_level == 0:
        if (profit_pct >= 1.5 or has_rsi_spike or soaring_rate >= 1.0 or 
            (buy_type == 3 and not is_type3_stable) or max_profit_pct >= 5.0):
            return 2
    
    # 2. 하향 (Level 2 -> 1): 타입별로 분기된 변수에 따라 전환
    if current_level == 2:
        if is_recovering_general or is_recovering_type3:
            logger.info(f"✅ {symbol} 지표 안정화 시작 -> Level 1(CAUTION) 전환")
            return 1
                    
    # ////////// [수정] 섹션 3을 섹션 2(if == 2) 블록 밖으로 독립시킴 //////////
    # 3. 해제 (Level 1 -> 0): 주석 처리하여 긴급 모드 해제 방지
    if current_level == 1:
        # if buy_type == 3:
        #     if ma40_slope > 0 and is_converging and (not is_3m_below_ma40):
        #         return 0
        # elif rsi < 50:
        #     return 0
        pass
    # ////////////////////////////////////////////////////////////////////
                
    # [중요] 반드시 모든 if 블록 밖에서 현재 레벨을 최종 반환해야 NoneType 에러가 나지 않습니다.
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


# ////////// [상태 저장 로직 추가 시작] //////////
EMERGENCY_STATUS_FILE = '/home/rocky/my_coin_trader/emergency_status.json'

def load_emergency_mode():
    if os.path.exists(EMERGENCY_STATUS_FILE):
        try:
            with open(EMERGENCY_STATUS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"비상 상태 로드 오류: {e}")
    return {}

def save_emergency_mode():
    try:
        with open(EMERGENCY_STATUS_FILE, 'w') as f:
            json.dump(emergency_mode, f)
    except Exception as e:
        logger.error(f"비상 상태 저장 오류: {e}")

# 긴급 감시 상태 저장 변수 (기존 빈 딕셔너리 대체)
emergency_mode = load_emergency_mode()
# ////////// [상태 저장 로직 추가 끝] //////////


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
    
    # ###### 수정 시작: 실시간 파일 참조 언패킹 변수 확대 ######
    global is_buy_locked, market_ref_rate, panic_cleared_time, prev_day_offset, last_date, last_current_avg
    file_locked, file_ref, file_cleared, file_offset, file_ldate, file_lavg = load_market_status()
    is_buy_locked = file_locked
    market_ref_rate = file_ref
    panic_cleared_time = file_cleared
    prev_day_offset = file_offset
    last_date = file_ldate
    last_current_avg = file_lavg
    # ###### 수정 끝 ######
    
    # global is_buy_locked, market_ref_rate # 기존 코드는 주석 처리
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
    reasons = []
    if len(df) < 285:
        return False, "데이터부족", "", data_dict

    # [기존 유지] 40/185일선 + RSI
    df.loc[df.index[-1], 'close'] = curr_price
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma14'] = df['close'].rolling(14).mean()
    df['ma40'] = df['close'].rolling(40).mean()
    df['ma90'] = df['close'].rolling(90).mean()
    df['ma185'] = df['close'].rolling(185).mean()
    df['rsi'] = calculate_rsi(df)

    curr = df.iloc[-1]
    prev = df.iloc[-2]
    curr_price = float(curr['close'])

    ###### 전수조사 반영: 모든 TYPE에서 사용하는 공통 변수 사전 정의 ######
    ma5_val = float(curr['ma5']) if not pd.isna(curr['ma5']) else curr_price
    ma14_val = float(curr['ma14']) if not pd.isna(curr['ma14']) else 0
    prev_ma14 = float(prev['ma14'])
    ma40_val = float(curr['ma40']) if not pd.isna(curr['ma40']) else 0
    ma5_above_14_count = (df['ma5'].iloc[-15:] > df['ma14'].iloc[-15:]).sum()
    is_trend_stable = (ma5_above_14_count >= 11)
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
    
    slope_rate = ((ma185_val - prev_ma185) / prev_ma185) * 100 if prev_ma185 > 0 else 0
    ma5_slope = ((ma5_val - prev_ma5) / prev_ma5) * 100 if prev_ma5 > 0 else 0
    ##########################################################################
    # [1단계: TYPE 1, 2 공통 지표 사전 계산]
    # 1. 90선 세기: 양수이거나, 음수일 때 직전보다 완만해져야 True
    ma90_v = df['ma90'].diff() # 현재 90선의 기울기(속도)
    # 논리: (현재 기울기가 0 이상인가?) OR (음수라면 현재 기울기가 이전보다 크거나 같은가?)
    # 5 -> 3 (True), -5 -> -3 (True), -3 -> -5 (False)
    ma90_intensity_ok = (ma90_v > 0) | (ma90_v > ma90_v.shift(1))
    # 최근 10봉 중 위 조건을 만족하는 횟수가 6번(60%) 이상인지 계산 (기존 로직 주석 처리)
    actual_up_count_90 = (ma90_v.tail(10) > 0).sum()
    ma90_up_count = ma90_intensity_ok.tail(10).sum() if actual_up_count_90 >= 2 else 0

    ma40_v = df['ma40'].diff()
    # 양수 프리패스 OR 음수 구간 내 변곡점(현재 기울기 >= 이전 기울기) 체크
    ma40_intensity_ok = (ma40_v > 0) | (ma40_v > ma40_v.shift(1))
    ma40_up_count = ma40_intensity_ok.tail(10).sum()
    # ////////// [수정: MA185 하락 강도 완화 및 변곡점 60% 로직] //////////
    ma185_v = df['ma185'].diff()
    # 1. 0 이상(평행/상향)이거나 2. 음수 구간에서 직전보다 수치상 커져야(완만해져야) True
    # -3 > -3 (False), -3 > -5 (True)
    ma185_intensity_ok = (ma185_v > 0) | (ma185_v > ma185_v.shift(1))
    # 2. 새로운 안전장치: 최근 10개 중 '실제로 수치가 상승(>0)'한 횟수 계산
    actual_up_count = (ma185_v.tail(10) > 0).sum()
    ma185_up_count = ma185_intensity_ok.tail(10).sum() if actual_up_count >= 2 else 0
    # 2. 5-40선 이격도 및 수렴 여부
    gap_5_40_pct = (ma5_val - ma40_val) / ma40_val * 100 if ma40_val > 0 else 0
    # 7봉의 데이터를 수집하여 6번의 인접 비교 구간을 생성
    disps_common = [abs(df['ma5'].iloc[-i] - df['ma40'].iloc[-i]) / df['ma40'].iloc[-i] * 100 if df['ma40'].iloc[-i] > 0 else 999 for i in range(1, 8)]
    # 6회의 비교 중 2회 이상 수렴하거나 평행하면 통과 (일시적 발산 노이즈 무시)
    is_converging_5_40 = (sum(1 for i in range(6) if disps_common[i] <= disps_common[i+1]) >= 2)
    # 3. 185일선 안착 안정성 (최근 10봉 하락 틱 수)
    t1_v2_tick = get_bithumb_tick_size(ma185_val)
    total_drop_ticks = ((df['ma185'].diff().tail(8) * -1) / t1_v2_tick).clip(lower=0).sum() if t1_v2_tick > 0 else 999
    is_185_landing_stable = total_drop_ticks <= 10    

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
    
    is_recovery_window = False
    if not is_buy_locked and panic_cleared_time is not None:
        time_since_clear = (datetime.now() - panic_cleared_time).total_seconds()
        if time_since_clear < 6 * 3600:  # 6시간(3600초 * 6) 이내
            is_recovery_window = True
            
    if is_buy_locked or is_recovery_window:
        dynamic_vol_limit = 35.0
    else:
        temp_disparity_40_5 = abs(ma5_val - ma40_val) / ma40_val * 100 if ma40_val > 0 else 999
        recent_185 = df['ma185'].iloc[-200:] if len(df) >= 200 else df['ma185']
        min_185, max_185 = recent_185.min(), recent_185.max()
        temp_pos_185 = (ma185_val - min_185) / (max_185 - min_185) if (max_185 - min_185) > 0 else 1.0

        if ma40_up_count >= 8 or temp_pos_185 <= 0.3:
            ma5_slope_positive = (df['ma5'].iloc[-1] - df['ma5'].iloc[-2]) > 0 if len(df) >= 2 else False
            is_squeeze = (ma5_val <= ma40_val) and (temp_disparity_40_5 <= 1.5)
            is_breakout = (ma5_val > ma40_val) and (temp_disparity_40_5 >= 0.5 or ma5_slope_positive)
            
            if is_squeeze or is_breakout:
                dynamic_vol_limit = 55.0  # [Case A] 필수 조건 충족 + (초밀착 수렴 OR 돌파 발산)
            else: 
                dynamic_vol_limit = 40.0  # [Case B] 필수 조건 충족 + 일반 구간
        else:
            dynamic_vol_limit = 20.0      # [Case C] 추세 미달 및 고점 구간
    
    if volatility >= dynamic_vol_limit:
        print(f"DEBUG: {symbol} 매수 탈락 - 변동성 과다({volatility:.1f}% >= {dynamic_vol_limit}%)")
        return False, f"🚫 [상단방어] 구간 변동성({volatility:.1f}%) > 허용치({dynamic_vol_limit}%)", "F", {}
        
    if curr_price < curr['high'] * 0.95:
        return False, f"🚫 [설거지방어] 고가대비 이탈(-5.0%↑)", "F", {}

    if upper_wick >= 5.0:
        print(f"DEBUG: {symbol} 매수 탈락 - 윗꼬리 과다({upper_wick:.1f}%)")
        return False, f"🚫 [윗꼬리 방어] 윗꼬리 과다({upper_wick:.1f}%) 설거지 포착", "F", {}    
    # [가격 필터] 10원 미만 또는 10,000원 이상 → BTC 마켓 동전주/비정상 차단
    if curr_price < 1 or curr_price >= 10000:
        return False, "가격필터(BTC마켓)", "", data_dict

    # [유의 종목] 수급 돌파(S/S+) 포함 모든 매수 신호에서 투자유의 종목 제외 (먼저 검사)
    if symbol.split('/')[0] in warning_list:
        return False, "투자유의", "F", data_dict
    # 현재가(close) 대비 고가(high)의 순수 물리적 거리를 계산 (양봉 기준)
    upper_wick_dist_pct = (curr['high'] - curr_price) / curr_price * 100
    
    if upper_wick_dist_pct >= 5.0:
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
        if dynamic_rise >= dynamic_vol_limit:
            data_dict['dynamic_rise_YN'] = 'Y'
            print(f"DEBUG: {symbol} | 골크 발생. 골크점 : {199-gold_index} | 시작점 : {199-check_start_idx} | 저가: {win_low} | 고가: {win_high} | 급등락 크기: {dynamic_rise:.2f}% | 급등락YN: {data_dict.get('dynamic_rise_YN')}")
            return False, f"🚫 [제외] 골크 전후 변동성 과다({dynamic_rise:.1f}% >= {dynamic_vol_limit}%)", "B", data_dict
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
        if dynamic_rise >= dynamic_vol_limit:
            data_dict['dynamic_rise_YN'] = 'Y'
            return False, f"🚫 [제외] 골크 미발생 변동성 과다({dynamic_rise:.1f}% >= 20%)", "B", data_dict
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
    tick_185 = get_bithumb_tick_size(ma185_val)
    ma185_slopes = [(df['ma185'].iloc[-i] - df['ma185'].iloc[-i-1]) / tick_185 if tick_185 > 0 else 0 for i in range(1, 8)]
    improve_cnt = sum(1 for i in range(6) if ma185_slopes[i] > ma185_slopes[i+1])

    if diff_185 < -1.2:
        if improve_cnt >= 4:
            logger.info(f"✨ [추세개선통과] {symbol} | 현재diff:{diff_185:.2f} < -1.2 이나 30분봉 6개 중 {improve_cnt}회 개선 확인")
        else:
            reason = f"185일선 급락(diff:{diff_185:.2f} < -1.2, 개선:{improve_cnt}/6)"
            data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
            return False, reason, "", data_dict
        
    ma40_slope_common = ((ma40_val - prev_ma40) / prev_ma40) * 100 if prev_ma40 > 0 else 0
    prev_close_val = float(prev['close'])
    ###### [수정 제안: 찐 트리거의 범용성 확대] ######
    # 1. 기울기 완화: -0.005 -> -0.015 (완만하게 하락 중인 밥그릇도 수용)
    # 2. 찰나의 순간 완화: 무조건 '이번에 뚫어야 함' 대신 '40선 위에서 안착 중'인 상태 포함
    # 3. 이격 조절: 40선 대비 0.5% ~ 3% 이내 (너무 선에 붙어있지 않아도 됨)

    is_true_trigger = (
        (ma40_up_count >= 6) and                # 40선 흐름이 50% 이상 개선/평행인가
            (curr_price > ma40_val) and             # 40선 위에 있는가
                (curr_price <= ma40_val * 1.03)         # 너무 뜬 건 아닌가 (3% 가드)
                )
    
    base_avg_vol_t3 = df['vol'].tail(20).mean()
    vol_ratio_t3 = (float(curr['vol']) / base_avg_vol_t3) if base_avg_vol_t3 > 0 else 0

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
            logger.info(f"DEBUG: {symbol} | [TYPE3-제외] 185선 고점 구간({pos_185*100:.1f}%)")
        else:
            # 가드 A: 5일선 이격도가 바닥권(-7%/-4%)이면서, 직전보다 수렴하고, 새끼 양봉일 때
            if disparity_5_185 <= target_disparity and disparity_5_185 > prev_disparity and -2.0 <= candle_body_pct <= 2.0:
                
                ###### [유지] 185선이 투매 수준으로 꺾이지 않았을 때만 진입 (slope_rate >= -0.06)
                if slope_rate >= -0.06:
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
                        if (-0.5 <= disparity_5_40 < 0) and vol_ratio_t3 < 0.7 and is_true_trigger:
                            # [S+급] 40선 아래에서 격돌 중 (골크 전) + 찐 트리거
                            data_dict['grade'] = 'S+'
                            data_dict['multiplier'] = 2.0  
                            return True, f"💎 [TYPE3-S+] 골크 전 바닥 격돌 및 찐 트리거 ({disparity_5_40:.2f}%)", "S+", data_dict
                        elif vol_ratio_t3 < 0.7 and is_true_trigger:
                            # [S급] 찐 트리거 및 거래량 조건 만족 시 S급 확정 (격격 이격도 완화)
                            data_dict['grade'] = 'S'
                            return True, f"💎 [TYPE3-S] 바닥 40선 찐 트리거 안착 및 수렴 ({disparity_5_40:.2f}%)", "S", data_dict
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
    
    if rsi_val >= 65:
        logger.info(f"DEBUG: {symbol} RSI 과열({rsi_val:.1f} >= 65, 현재가:{curr_price:,.0f})")
        reason = f"RSI 과열({rsi_val:.1f} >= 65, 현재가:{curr_price:,.0f})"
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

    ###### [수정 1: 모든 TYPE 공용 변수 사전 계산 - NameError 방지] ######
    # 1. 족보(GC) 횟수 및 구간 변동성
    gc_count_t1 = 0
    diff_40_90_pct = abs(df['ma40'].iloc[-1] - df['ma90'].iloc[-1]) / df['ma90'].iloc[-1] * 100 if df['ma90'].iloc[-1] > 0 else 999
    is_40_90_close = diff_40_90_pct <= 1.0
    for i in range(1, 150):
        if i+1 < len(df):
            if df['ma40'].iloc[-i-1] < df['ma185'].iloc[-i-1] and df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                gc_count_t1 += 1
    vol_sectional = ((df['high'].tail(64).max() - df['low'].tail(64).min()) / df['low'].tail(64).min()) * 100
    has_down_touch = (df['close'].iloc[-11:-1] < df['ma40'].iloc[-11:-1]).any() if len(df) >= 11 else False

    # 2. 반등(Rebound) 엔진 (T2/T4 공유)
    ma14_v = df['ma14'].diff()
    ma14_intensity_ok = (ma14_v > 0) | (ma14_v > ma14_v.shift(1))
    ma14_up_count = ma14_intensity_ok.tail(6).sum()
    is_ma14_strong = ma14_up_count >= 3
    gap_14_40_pct = ((ma14_val - ma40_val) / ma40_val) * 100 if ma40_val > 0 else 0
    t2_fail_reason = ""
    if gap_14_40_pct <= 0:
        is_valid_convergence = is_converging_5_40 if ma5_val < ma40_val else (ma5_slope > 0)
        slopes_14 = [df['ma14'].iloc[-i] - df['ma14'].iloc[-(i+1)] for i in range(1, 8)]
        has_positive_ma14 = any(s >= 0 for s in slopes_14[:6])
        is_bullish_breakout = (curr_price >= float(curr['open'])) and ((curr_price > ma14_val) or (float(curr['open']) > ma14_val))
        if not is_bullish_breakout:
            # 봇이 계산한 실제 숫자를 로그에 찍어서 차트와 비교합니다.
            logger.debug(f"🔍 [양봉판정로그] {symbol} | 현재가:{curr_price} | 시가:{curr['open']} | MA14:{ma14_val:.2f} | 결과:FAIL")
        is_t2_rebound = (-3.0 <= gap_14_40_pct <= 0) and (ma14_up_count >= 1) and has_positive_ma14 and is_bullish_breakout and is_valid_convergence
        
        if not is_t2_rebound:
            if not (-3.0 <= gap_14_40_pct <= 0): t2_fail_reason = f"이격범위이탈({gap_14_40_pct:.2f}%)"
            elif ma14_up_count < 1: t2_fail_reason = f"14선상승부족({ma14_up_count}/1)"
            elif not has_positive_ma14: t2_fail_reason = "14선실체없음"
            elif not is_bullish_breakout: t2_fail_reason = "양봉돌파실패"
            elif not is_valid_convergence: t2_fail_reason = "수렴/5선발산실패"
    else:
        is_valid_convergence = is_converging_5_40 if ma5_val > ma40_val else (ma5_slope > 0)
        slopes = [df['ma5'].iloc[-i] - df['ma5'].iloc[-(i+1)] for i in range(1, 8)]
        slopes_185 = [df['ma185'].iloc[-i] - df['ma185'].iloc[-(i+1)] for i in range(1, 8)]
        slope_improvements = sum(1 for i in range(5) if slopes[i] > slopes[i+1])
        has_positive_ma5 = any(s > -0.03 for s in slopes[:6])
        is_185_trend_ok = (slopes_185[0] > 0) or (slopes_185[0] > slopes_185[1])
        
        # //======== [수정 사항: 14선이 40선 위에 있을 때 고점 추격 매수 방지 로직 보강] ========//
        is_bullish_breakout_high = (curr_price >= float(curr['open'])) and ((curr_price > ma14_val) or (float(curr['open']) > ma14_val))
        is_gap_40_safe = gap_14_40_pct <= 3.0 # 40선 대비 이격도 3% 이내 강제
        
        is_t2_rebound = (slope_improvements >= 2) and has_positive_ma5 and is_185_trend_ok and is_valid_convergence and is_bullish_breakout_high and is_gap_40_safe
        if not is_t2_rebound:
            if slope_improvements < 2: t2_fail_reason = f"5선가속도부족({slope_improvements}/2)"
            elif not has_positive_ma5: t2_fail_reason = "5선문턱(-0.03)미달"
            elif not is_185_trend_ok: t2_fail_reason = "185선대추세하락"
            elif not is_valid_convergence: t2_fail_reason = "수렴/5선발산실패"
            elif not is_bullish_breakout_high: t2_fail_reason = "고공양봉돌파실패"
            elif not is_gap_40_safe: t2_fail_reason = f"14/40이격과다({gap_14_40_pct:.2f}%)"
    ma14_slope_v = ((ma14_val - prev_ma14) / prev_ma14) * 100 if prev_ma14 > 0 else 0
    is_true_trigger_t2 = (curr_price > ma14_val) and (curr_price <= ma14_val * 1.03) and (ma14_slope_v >= -0.02) and (vol_ratio >= 0.8)
    
    gap_90_185 = abs(ma90_val - ma185_val) / ma185_val * 100 if ma185_val > 0 else 999
    prev_gap_90_185 = abs(prev_ma90 - prev_ma185) / prev_ma185 * 100 if prev_ma185 > 0 else 999
    is_death_conv = False
    if ma90_val < ma185_val:
        # 90선이 아래일 때: 거리가 벌어지는(발산) '나쁜 상황' + 1.5% 이내면 차단
        if (gap_90_185 > prev_gap_90_185) and (gap_90_185 <= 1.5):
            is_death_conv = True
    elif ma90_val > ma185_val:
        # 90선이 위일 때: 거리가 좁혀지는(수렴) '나쁜 상황' + 1.5% 이내면 차단
        if (gap_90_185 < prev_gap_90_185) and (gap_90_185 <= 1.5):
            is_death_conv = True
    is_t4_volume_surge = (data_dict.get('vol_ratio', 0) >= 1.5)

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
        is_t1_structure_ready = is_40_90_close and is_ma14_strong
        # 전수 조사를 통과한 깨끗한 밥그릇만 아래 등급 판정 진행
        if strict_descending and strict_stabilized and is_t1_structure_ready:
            if is_high_pos_185:
                logger.info(f"DEBUG: {symbol} | [TYPE1] 탈락 이유-진입 실패 | 185선 고점 구간({pos_185*100:.1f}%)")
                return False, f"🚫 [TYPE1-제외] 185선 고점 구간({pos_185*100:.1f}%)", "B", data_dict
            
            data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
            
            # 기준 A: 185일선이 우상향(>0)하면서, 최근 10봉 중 6봉 이상 안정화되었을 때 (가짜 반등 방지)
            is_slope_strong = (slope_rate > 0) and (stabilized_count >= 6)
            
            # 기준 B: -0.06 ~ 0 사이의 하락 구간이지만, 기울기 개선세가 10봉 중 2봉 이상일 때 (XTER 등 구제)
            is_slope_dense = (-0.06 <= slope_rate <= 0) and (ma185_up_count >= 2)
            
            # 둘 중 하나라도 만족하면 '바닥 안착'으로 인정하고 진입 타점 계산 시작
            if is_slope_strong or is_slope_dense:
                
                # ==== [TYPE1 수정 시작: 185-40선 수렴 추세(6/3) 확인 및 이격 1.5% 복구] ====
                # 최근 6봉의 185-40 이격도를 구하여 전봉 대비 감소(수렴)한 횟수 계산
                hist_disp = [abs(df['ma40'].iloc[-i] - df['ma185'].iloc[-i]) / df['ma185'].iloc[-i] if df['ma185'].iloc[-i] > 0 else 999 for i in range(1, 8)]
                conv_cnt = sum(1 for i in range(6) if hist_disp[i] < hist_disp[i+1])
                # 1.5% 이격 이내 + 3회 이상 수렴 + 기존 조건 필수 만족
                if disparity_gold <= 0.015 and conv_cnt >= 3 and is_converging_5_40 and (-0.8 <= gap_5_40_pct <= 0.3) and (ma90_up_count >= 5) and is_true_trigger:
                    if is_40_90_gc:
                        data_dict['grade'] = 'S+'
                        return True, "💎 [TYPE1-S+] 밥그릇 수렴 및 40/90 골크 + 찐 트리거", "S+", data_dict
                    else:
                        if disparity_gold <= 0.010:
                            data_dict['grade'] = 'S'
                            return True, "💎 [TYPE1-S] 밥그릇 바닥 수렴 및 40선 찐 트리거 안착", "S", data_dict
                else:
                    logger.info(f"DEBUG: {symbol} | [TYPE1-S/S+] 격돌 실패 | 골크 : {disparity_gold} <= 0.005 | 5/40수렴: {is_converging_5_40} | 5/40 gap pct: -0.8 <= {gap_5_40_pct:.2f}% <= 0.3 | 90선 상승: {ma90_up_count} >= 5")

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
                logger.info(f"DEBUG: {symbol} | [TYPE1] 탈락 이유-진입 실패 | 185선 기울기 ({slope_rate} >= -0.02)")
            # [B급] 상승 대기 (골드 안착)
            data_dict['grade'] = 'B'
            return True, "🚀 [TYPE1-B] 상승대기(골드안착)", "B", data_dict
        else:
            logger.info(f"DEBUG: {symbol} TYPE1 탈락 이유 | 185선 96봉~10봉 내리막:{strict_descending}({descending_count}개 하락), 185선 우상향:{strict_stabilized}, gc횟수: {gc_count_t1}회, 40/90수렴:{is_40_90_close}({diff_40_90_pct:.2f}%), 14선강도:{is_ma14_strong}({ma14_up_count}/6)")

    # // [수정: TYPE 4 로직 독립화 - 기존 엔진 활용 버전] //
    is_t4_safe = (vol_sectional <= dynamic_vol_limit)
    
    # --- [통행증 업그레이드] ---
    # 기존: ma40_val > ma185_val (정배열만)
    # 수정: 정배열이거나, 혹은 역배열이라도 사용자님이 말한 '4대 수렴 기준' 만족 시 통과
    is_t4_base_alignment = (ma40_val > ma185_val)
    disparity_185_40 = abs(ma40_val - ma185_val) / ma185_val if ma185_val > 0 else 0
    
    is_t4_hybrid_alignment = (
        (not is_t4_base_alignment) and 
        (disparity_185_40 <= 0.015) and        # [기준1: 1.5% 이격]
        (ma40_up_count >= 5) and                 # [기준2,3: 기울기 개선]
        (ma40_slope_common >= -0.02) and           # [기준4: 기울기 마지노선]
        ###### [추가] 타입4 독립 방어: 185일선 품질 공통 변수 강제 결합 ######
        (ma185_up_count >= 5) and
        (ma185_intensity_ok.iloc[-1]) and
        (slope_rate >= -0.01)
    )
    is_t4_alignment = is_t4_base_alignment or is_t4_hybrid_alignment
    # ---------------------------

    reasons_t4 = []
    if not is_t4_safe: reasons_t4.append(f"변동성초과({vol_sectional:.1f}%)")
    if not is_t4_alignment: reasons_t4.append(f"배열/수렴미달")
    if gap_5_40_pct > 4.0: reasons_t4.append(f"5/40이격과다({gap_5_40_pct:.1f}%)")
    if not (gc_count_t1 == 0): reasons_t4.append(f"GC이력({gc_count_t1}회)")
    
    # [수정] 수급 문턱만 3.0으로 낮추기
    if not is_trend_stable:
        reasons_t4.append(f"추세지속부족({ma5_above_14_count}/15)")
    if vol_ratio < 1.5: 
        reasons_t4.append(f"수급미달({vol_ratio:.1f}x<1.5x)")
    
    # [핵심] 기존 엔진은 그대로 사용! (단, REI를 위해 has_down_touch만 선택적 제거)
    ###### [수정/추가] 격돌 로직 이식 및 대추세(185선) 급락 가드 결합 ######
    if not is_t2_rebound: reasons_t4.append(f"격돌/수렴실패({t2_fail_reason})")
    if slope_rate < -0.01: reasons_t4.append(f"185선급락({slope_rate:.4f})")
    
    # REI 같은 강한 종목을 위해 TYPE 4에서만 '터치' 조건 주석 처리하거나 완화
    # if not has_down_touch: reasons_t4.append("40선터치없음") 
    
    if not is_true_trigger_t2: reasons_t4.append("찐트리거미달")

    # [가드] 사용자님이 강조하신 양봉 및 윗꼬리 가드
    is_bullish = curr_price > float(curr['open'])
    upper_shadow_pct = (float(curr['high']) - curr_price) / curr_price * 100 if curr_price > 0 else 0
    
    if not is_bullish: reasons_t4.append("음봉탈락")
    if upper_shadow_pct > 2.0: reasons_t4.append(f"윗꼬리과다({upper_shadow_pct:.1f}%)")

    if not reasons_t4:
        grade = "S+" if curr_price >= ma185_val else "S"
        data_dict['grade'] = grade
        return True, f"🚀 [TYPE4-{grade}] 정배열(수렴) 돌파 (기존엔진+수급완화)", grade, data_dict
    else:
        logger.info(f"DEBUG: {symbol} | [TYPE4] 탈락 이유: {', '.join(reasons_t4)}")
    # ==========================================================================
    # [TYPE2: 눌림목 및 40선 지지 (에너지 응축)]
    # ==========================================================================
    if 4 <= bars_since_gold <= 144:
        ###### [3단계 최종 이식: 로그 접두어 '탈락 이유' 및 참조 오류 방지] ######
        # 0. 초기화 (이 블록 안에서만 사용되는 지역 변수)
        gc_count_150, dc_count_after_gc, valid_gc_idx = 0, 0, -1
        
        # [A] 역사적 순결성 검증 (150봉 루프) - 중복 계산 없이 한 번만 수행
        for i in range(149, 0, -1):
            idx = len(df) - i
            if idx <= 30: continue
            
            # 골든크로스 & 데드존 검증 (진짜 밥그릇인지 확인)
            # [수정] 골든크로스 시점의 185선 기울기 질적 검증 추가
            if df['ma40'].iloc[idx-1] < df['ma185'].iloc[idx-1] and df['ma40'].iloc[idx] > df['ma185'].iloc[idx]:
                v185_at_gc = df['ma185'].iloc[idx] - df['ma185'].iloc[idx-1]
                v185_prev_at_gc = df['ma185'].iloc[idx-1] - df['ma185'].iloc[idx-2]
                # 185선이 평행 이상이거나 하락세가 둔화되었을 때만 인정
                if (v185_at_gc >= 0) or (v185_at_gc > v185_prev_at_gc):
                    if (df.iloc[idx-30:idx]['ma40'] < df.iloc[idx-30:idx]['ma185']).sum() >= 20:
                        gc_count_150 += 1
                        if valid_gc_idx == -1: valid_gc_idx = idx
            
            # 오염 감시 (유효 골크 이후 단 1원이라도 하회(데드크로스) 시 즉시 오염 처리)
            if valid_gc_idx != -1 and idx > valid_gc_idx:
                if df['ma40'].iloc[idx-1] > df['ma185'].iloc[idx-1] and df['ma40'].iloc[idx] < df['ma185'].iloc[idx]:
                    dc_count_after_gc += 1

        has_t1_history_clean = (1 <= gc_count_150 <= 2) and (dc_count_after_gc <= 1)

        # [B] T2 진입 가드 및 타점 (1, 2단계에서 계산한 공통 변수 활용)
        is_fresh = (bars_since_gold <= 64)
        vol_sectional = ((df['high'].tail(64).max() - df['low'].tail(64).min()) / df['low'].tail(64).min()) * 100
        is_type2_safe = (vol_sectional <= dynamic_vol_limit) and is_fresh

        # [C] 최종 판정 및 요청하신 키워드 로그 반영
        # ======== [수정 시작: VANA 필터 조건 추가 및 185선 지하실 추락 가드] ========
        if is_type2_safe and is_t2_rebound and is_trend_stable and has_t1_history_clean and (ma90_up_count >= 5) and (ma185_up_count >= 5) and is_185_landing_stable and has_down_touch and is_true_trigger_t2 and not is_death_conv:
            gc_idx = valid_gc_idx
            d_cnt = sum(1 for k in range(gc_idx-96, gc_idx-10) if df['ma185'].iloc[k] <= df['ma185'].iloc[k-1]) if gc_idx != -1 else 0
            height_pct = (curr_price - ma185_val) / ma185_val * 100
            
            if d_cnt >= 50 and (-3.0 <= height_pct <= 8.0):
                grade = "S+" if curr_price >= ma185_val else "S"
                data_dict['grade'] = grade
                return True, f"💎 [TYPE2-{grade}] 40선 내려앉음 후 찐 트리거 안착 ({grade})", grade, data_dict
            else:
                logger.info(f"DEBUG: {symbol} | [TYPE2] T1질적지표 탈락 이유: d_cnt={d_cnt}, height={height_pct:.1f}%")
        else:
            if not is_type2_safe:
                safe_detail = []
                if vol_sectional > dynamic_vol_limit: safe_detail.append(f"변동성 과다({vol_sectional:.1f}% > 기준:{dynamic_vol_limit:.1f}%)")
                if not is_fresh: safe_detail.append("T1이력없음")
                reasons.append(f"안전가드({', '.join(safe_detail)})")
            if gap_5_40_pct > 4.0: reasons.append(f"5/40이격과다({gap_5_40_pct:.1f}%)")
            if not is_t2_rebound: reasons.append(f"수렴실패({t2_fail_reason})")            
            if not has_t1_history_clean: reasons.append(f"역사오염(GC:{gc_count_150}, DC:{dc_count_after_gc})")
            if ma90_up_count < 5: reasons.append(f"90선추세({ma90_up_count})")
            if not is_185_landing_stable: reasons.append(f"185선불안정")
            if is_death_conv: reasons.append("중장기죽음의수렴")
            
            if reasons:
                logger.info(f"DEBUG: {symbol} | [TYPE2] 탈락 이유: {', '.join(reasons)}")

    ###### [수정 시작: 구버전 TYPE4 삭제 및 최종 반환값 간소화 (T2/T4 실패사유 명시적 통합)] ######
    data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)

    t2_fail_str = f"T2({', '.join(reasons)})" if ('reasons' in locals() and reasons) else "T2(미달)"
    t4_fail_str = f"T4({', '.join(reasons_t4)})" if ('reasons_t4' in locals() and reasons_t4) else "T4(미달)"
    fail_reason_str = f"{t2_fail_str} | {t4_fail_str}"
            
    return False, f"🚫 탈락사유: {fail_reason_str}", "F", data_dict
    ###### [수정 끝] ######

###### [수정] L2 익절: 최고가 양봉/도지 기준 2음봉 이탈 로직 ######
def check_3_2_negative_candles(target_df):
    if len(target_df) < 10: return False, ""
    
    # 1. 최근 10봉 중 '양봉(Close >= Open)' 또는 '도지'만 필터링
    recent_10 = target_df.tail(10)
    bullish_or_doji = recent_10[recent_10['close'] >= recent_10['open']]
    
    if bullish_or_doji.empty:
        return False, ""
        
    # 2. 그중에서 '최고가(high)'를 찍은 봉을 기준점(Peak)으로 선정
    peak_idx = bullish_or_doji['high'].idxmax()
    peak_candle = target_df.loc[peak_idx]
    peak_vol = peak_candle['vol']
    peak_iloc = target_df.index.get_loc(peak_idx)
    
    # 3. [자물쇠] 현재 진행 중인 봉이 양봉(빨간색)이면 절대 팔지 않음
    curr_candle = target_df.iloc[-1]
    if curr_candle['close'] >= curr_candle['open']:
        return False, ""
        
    # ======== [수정 시작: 2연속 직계 음봉 강제 로직] ========
    # 고점 이후 정확히 2개의 봉이 더 진행되었는지 확인 (현재 봉이 peak_iloc + 2 인지)
    if len(target_df) - 1 != peak_iloc + 2:
        return False, ""
        
    prev_candle = target_df.iloc[-2] # 고점 바로 다음 봉 (C3_1)
    
    # 직전 봉과 현재 봉이 모두 음봉인지 확인
    prev_is_neg = prev_candle['close'] < prev_candle['open']
    curr_is_neg = curr_candle['close'] < curr_candle['open']
    
    if prev_is_neg and curr_is_neg:
        # 두 음봉 모두 고점 거래량의 10% 이상인지 확인
        if prev_candle['vol'] >= peak_vol * 0.1 and curr_candle['vol'] >= peak_vol * 0.1:
            return True, f"🚨 [세력이탈] 고점 직후 10%↑ 2연속 음봉 감지"
            
    return False, ""
###### 수정 끝 ######

# ---------------------------------------------------------
# [복구 및 추가] 매도 감시 메인 함수 (ERROR 방지 핵심)
# ---------------------------------------------------------
async def check_sell_signal(exchange, df, symbol, purchase_price, max_price=0, grade='A', symbol_inventory_age=99, status=None, realtime_p=None, buy_type=1):
    global emergency_mode
    
    buy_type = int(buy_type) # 타입 비교 에러 방지용 강제 형변환
    support_price = 0.0      # NameError 원천 차단 방어막

        # 1. 실시간 현재가 확정
    temp_curr = df.iloc[-1]
    curr_p = realtime_p if realtime_p is not None else float(temp_curr['close'])

    if curr_p <= 0:
        return False, "데이터오류(0원)", False
        
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
    high_price = max(max_price, df['high'].tail(2).max())
    
    # 5. 수익률 및 RSI 스파이크 계산 (딱 한 번만 실행)
    has_rsi_spike = (rsi_series.tail(14) >= 80).any()
    profit_rate = (curr_p - purchase_price) / purchase_price if purchase_price > 0 else 0
    profit_rate_pct = profit_rate * 100
    max_profit_rate_pct = ((high_price - purchase_price) / purchase_price * 100) if purchase_price > 0 else 0

    # 6. 본절방어 기준점(틱 사이즈 반영) 계산
    one_tick_pct = (get_bithumb_tick_size(purchase_price) / purchase_price * 100) if purchase_price > 0 else 0
    profit_threshold = max(0.7, 3 * one_tick_pct)
    
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
    if current_age == 99:
        try:
            # 빗썸 API로 매수 이력 조회 시도
            trades = await asyncio.to_thread(exchange.fetch_my_trades, symbol)
            if trades:
                # 가장 최근 매수 시점을 찾아 현재 시간과의 차이로 age 계산
                last_buy_time = trades[-1]['timestamp']
                current_age = int((time.time() * 1000 - last_buy_time) / (30 * 60 * 1000))
            else:
                # 이력이 없으면 수동 매수 직후로 간주하여 0으로 설정
                current_age = 0
        except Exception:
            # [핵심] 빗썸처럼 API를 지원하지 않아 에러가 나면 99가 아닌 0으로 강제 설정
            # 이렇게 해야 하단의 'if current_age < 6' 보호막이 작동함
            current_age = 0
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
    if current_age < 6 and profit_rate_pct < profit_threshold:
        return False, f"진입 초기 유예({current_age}봉)", False

        # 1. 본절 방어 (수정된 동적 기준 적용)
    if profit_threshold <= max_profit_rate_pct < 1.5 and purchase_price * 0.999 <= curr_p <= purchase_price * 1.001:
        cooldown_dict[symbol] = datetime.now() + timedelta(hours=6)
        return True, f"🛡️ [S-TS-0.7] 본절방어(즉시)", True

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
    if old_lvl >= 1:
        new_lvl = max(old_lvl, new_lvl)
        
    if emergency_mode.get(symbol) != new_lvl:
        emergency_mode[symbol] = new_lvl
        save_emergency_mode()

    if current_age == 0 and max_profit_rate_pct >= 1.0 and new_lvl == 0:
        new_lvl = 1
        emergency_mode[symbol] = new_lvl
        save_emergency_mode()
        logger.info(f"⚡ [하이패스] {symbol} 30분 내 1.0% 이상 급등, L1 강제 전환")
        return False, f"🚨 [비상감시] {symbol} 1% 돌파 L1 전환", False
    # Level 1, 2 모두 3분봉 엔진 가동 (사용자 요청: 하락 시 즉시 대응)
    if new_lvl >= 1 and df_3m is not None:
        logger.info(f"DEBUG: 🔥 [L{new_lvl} 엔진 가동] {symbol} | 수익: {profit_rate_pct:.2f}%")
        try:
            # [기존 ATOM 사례 대응 로직 유지]
            vol_30m = (df['high'].tail(3).max() - df['low'].tail(3).min()) / df['low'].tail(3).min() * 100
            if vol_30m < 1.2 and profit_rate_pct > 7.0:
                # if new_lvl == 2: emergency_mode[symbol] = 1 # 안정적 우상향 시 레벨 다운 유도
                pass  ###### [수정 1-2] 레벨 다운 금지 (들여쓰기 유지용 pass)
            else:
                high_10_3m = df_3m['high'].tail(10).max()
                curr_3m_p, curr_3m_data = curr_p, df_3m.iloc[-1]

                # ======== [수정 시작: C1-C2-C3 50% 허리 이탈 논리 적용] ========
                # (1) [수정] 고점 직전봉(C1) 허리(50%) 실시간 이탈 체크
                # 거래량이 아닌 '최고가(High)' 기준으로 고점(C2) 인덱스 찾기
                c2_idx_relative = df_3m['high'].tail(10).argmax()
                abs_peak_idx = len(df_3m) - 10 + c2_idx_relative
                c2_candle = df_3m.iloc[abs_peak_idx]
                
                # C2가 양봉/도지이고, 이전 봉(C1)이 존재할 때
                if c2_candle['close'] >= c2_candle['open'] and abs_peak_idx > 0:
                    c1_candle = df_3m.iloc[abs_peak_idx - 1]
                    
                    # C1이 양봉일 때만 허리값 계산
                    if c1_candle['close'] > c1_candle['open']:
                        c1_mid_p = (c1_candle['open'] + c1_candle['close']) / 2
                        
                        # 현재 봉(C3)이 음봉이면서 현재가(curr_3m_p)가 C1 허리값을 이탈했을 때 매도 (거래량 조건 삭제)
                        if curr_3m_p < c1_mid_p and curr_3m_data['close'] < curr_3m_data['open']:
                            return True, f"🚨[50%긴급-L{new_lvl}] C1 양봉 허리 이탈", True

                # (2) [기존] 세력 이탈 감지 (2음봉 + 거래량)
                is_2_neg_3m, reason_2_neg_3m = check_3_2_negative_candles(df_3m)
                if is_2_neg_3m:
                    high_idx_3m = df_3m['high'].tail(10).argmax()
                    high_vol_3m = df_3m['vol'].iloc[len(df_3m) - 10 + high_idx_3m]
                    if is_2_neg_3m: return True, reason_2_neg_3m, True

                max_yield_3m = (high_10_3m - purchase_price) / purchase_price * 100
                if current_age == 0 and max_yield_3m >= 1.0:
                    if curr_3m_p < (purchase_price * 1.001):
                        return True, f"🛡️[본절가드] 30분내 급등({max_yield_3m:.1f}%) 후 이탈, 0.1% 수익 보존", True

                # (3) [기존 로직 100% 유지] 위치별 차등 낙폭
                if soaring_rate >= 8.0:
                    if curr_3m_p < high_10_3m * 0.98:
                        return True, f"🚨[급등-강력] L{new_lvl} 고점대비 2% 하락 (위치:{soaring_rate:.1f}%)", True
                elif 8.0 > soaring_rate >= 4.0:
                    if curr_3m_p < high_10_3m * 0.984:
                        return True, f"🚨[급등-강력] L{new_lvl} 고점대비 1.6% 하락 (위치:{soaring_rate:.1f}%)", True
                else:
                    if curr_3m_p < high_10_3m * 0.988:
                        return True, f"🚨[급등-초기] L{new_lvl} 고점대비 1.2% 하락 (위치:{soaring_rate:.1f}%)", True

                # (4) [기존] 수익률 13% 마지노선 사수
                if profit_rate_pct >= 13.0 and curr_3m_p <= purchase_price * 1.13:
                    return True, f"🚨[13%사수-L{new_lvl}] 마지노선 매도", True

            return False, f"🚀 비상 감시(L{new_lvl}) 유지 중", False
        except Exception as e:
            logger.error(f"비상 엔진 에러 (L{new_lvl}): {e}")


    elif new_lvl == 0:   # 일반 모드(Level 0)인 경우에만 30분봉 로직 수행
        logger.info(f"DEBUG: {symbol} | 일반 매도 모드 (5분 유예 감시)")
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

            # 2. 익절 구간 A (1.5% ~ 3.0% 미만): 고점 대비 0.7% 하락 시 매도
            if 1.5 <= max_profit_rate_pct < 3.0:
                if drop_from_peak > profit_threshold:
                    return True, f"💰 [S-TS-1.5] 익절 A (낙폭 {profit_threshold:,.1f}% 초과)", True

            # 3. 익절 구간 B (2.0% ~ 3.5% 미만): 고점 대비 1.0% 하락 시 매도
            elif 3.0 <= max_profit_rate_pct < 4.5:
                if drop_from_peak > 1.0:
                    return True, f"💰 [S-TS-3.0] 익절 B (낙폭 1.0% 초과)", True

            # 4. 익절 구간 C (4.5% 이상): 고점 대비 1.5% 하락 시 즉시 매도
            elif max_profit_rate_pct >= 4.5:
                if drop_from_peak > 1.5:
                    return True, f"🚀 [S-TS-4.5] 익절 C (낙폭 1.5% 초과)", True

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
    """
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
    """
   

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
    lvl = emergency_mode.get(symbol, 0)
    
    if lvl >= 1:
        # 레벨 1(Caution)이나 2(Emergency)일 때는 무조건 사이렌 아이콘 반환
        return "🚨", f"🔥 긴급감시(L{lvl})"
    wait_data = pending_approvals.get(symbol)
    
    # [추가] 상세 사유에서 중복 종목명(예: DBR/KRW) 제거
    clean_reason = sell_reason.split('(')[0].strip()
    
    # [1] 유예 및 긴급 상태 (파랑/🚨)
    if wait_data and wait_data.get('status') in ['WAITING', 'NOTIFIED']:
        elapsed = (datetime.now() - wait_data['start_time']).total_seconds() / 60
        limit = wait_data.get('wait_limit', 30)
        remains = max(0, int(limit - elapsed))
        
        # 긴급 판단(2음봉, 급락 등)은 사이렌(🚨) 고정, 일반은 파랑(🔵)
        is_urgent = ("🚨" in wait_data.get('last_icon', '') or "급등" in sell_reason or "2음봉" in sell_reason or "🚨" in sell_reason)
        icon = "🚨" if is_urgent else "🔵"
        msg = "긴급매도유예" if is_urgent else "일반매도유예"
        return icon, f"⏳ {remains}m 후 {msg}"

    # [2] 매도 신호 발생 (빨강 - 위험 신호)
    if is_sell_signal:
        return "🔴", f"⚠️ 매도신호({clean_reason})"

    # [3] 40선 하단 (노랑 - 주의 단계)
    if this_curr_p < ma40_val:
        if this_profit > -0.5:
             return "🟢", f"✅ 차트양호(홀딩)"
        return "🟡", f"⚠️ 40선 하단"

    # [4] 차트 양호 (초록 - 홀딩/안전 신호)
    return "🟢", f"✅ 차트양호(홀딩)"
