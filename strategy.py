import asyncio
import pandas as pd
import numpy as np
import requests
from datetime import datetime
from config import logger


def get_bithumb_tick_size(price):
    if price < 10: return 0.001
    if price < 100: return 0.01
    if price < 1000: return 0.1
    if price < 5000: return 1
    if price < 10000: return 5
    if price < 50000: return 10
    if price < 100000: return 50
    return 100


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
        return [coin for coin, info in data.items() if info.get('halt_status', 0) != 0]
    except Exception as e:
        logger.error(f"Warning List Fetch Error: {e}")
        return []


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
        # 이후 골든크로스(gold_index) 체크 로직으로 자연스럽게 이어짐


        gold_index = -1
        for i in range(1, 97):
            if df['ma40'].iloc[-i - 1] < df['ma185'].iloc[-i - 1] and \
                    df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
                gold_index = len(df) - i
                break

        if gold_index == -1: return False, ""
        bars_since_gold = len(df) - gold_index
        if bars_since_gold < 4: return False, ""

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


# [사용자 원본 버전 2 - 메인 사용 중인 로직]
def check_buy_signal(df, symbol, warning_list):
    """
    매수 신호 판단 함수 (4개 값 리턴)
    
    Returns:
        tuple: (is_buy: bool, reason: str, grade: str, data_dict: dict)
    """
    # 기본 data_dict 초기화
    data_dict = {}
    
    if len(df) < 185:
        return False, "데이터부족", "", data_dict

    df['ma40'] = df['close'].rolling(40).mean()
    df['ma185'] = df['close'].rolling(185).mean()
    df['rsi'] = calculate_rsi(df)

    curr = df.iloc[-1]
    prev = df.iloc[-2]
    curr_price = float(curr['close'])
    
    # 기본 수치 데이터 수집
    ma40_val = float(curr['ma40']) if not pd.isna(curr['ma40']) else 0
    ma185_val = float(curr['ma185']) if not pd.isna(curr['ma185']) else 0
    rsi_val = float(curr['rsi']) if not pd.isna(curr['rsi']) else 50
    
    data_dict = {
        'rsi': rsi_val,
        'ma40_val': ma40_val,
        'ma185_val': ma185_val,
        'current_price': curr_price,
        'grade': ''
    }

    if symbol.split('/')[0] in warning_list:
        data_dict['grade'] = 'F'
        return False, "투자유의", "F", data_dict

    # 1. 2일 전 대비 5시간 전 하락 여부 확인 (밥그릇 바닥 확인)
    ma185_p_2d = df['ma185'].iloc[-96] if len(df) >= 96 else df['ma185'].iloc[0]
    ma185_r_5h = df['ma185'].iloc[-10] if len(df) >= 10 else df['ma185'].iloc[0]
    is_was_descending = ma185_r_5h <= ma185_p_2d

    # 2. 현재 기울기 수치 (기존 계산식 유지)
    diff_185 = (curr['ma185'] - prev['ma185']) / get_bithumb_tick_size(curr['ma185'])
    slope_rate = ((curr['ma185'] - prev['ma185']) / prev['ma185']) * 100
    data_dict['slope_rate'] = slope_rate

    # [수정 핵심] ZRO(-0.0384)나 STG(0.0217)처럼 고개 든 놈을 살려주는 OR 로직
    if not (slope_rate >= -0.06 or is_was_descending):
        reason = f"185일선 하락 조건 불만족(기울기:{slope_rate:.4f}%)"
        return False, reason, "", data_dict

    # 3. 안전장치: 급격한 수직 낙하만 방어
    if diff_185 < -1.2:
        reason = f"185일선 급락(diff:{diff_185:.2f} < -1.2)"
        return False, reason, "", data_dict

    if diff_185 < -1.2:
        reason = f"185일선 급락(diff:{diff_185:.2f} < -1.2, 기울기:{slope_rate:.4f}%)"
        return False, reason, "", data_dict

    gold_index = -1
    for i in range(1, 97):
        if df['ma40'].iloc[-i - 1] < df['ma185'].iloc[-i - 1] and \
                df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
            gold_index = len(df) - i
            break

    bars_since_gold = len(df) - gold_index if gold_index != -1 else -1
    data_dict['bars_since_gold'] = bars_since_gold
    
    if gold_index == -1:
        reason = "골든크로스 미발생"
        return False, reason, "", data_dict
    
    if bars_since_gold < 4:
        reason = f"골든크로스 후 {bars_since_gold}봉(4봉 미만, 필요:4봉 이상)"
        return False, reason, "", data_dict

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
        reason = f"RSI 과열({rsi_val:.1f} > 65, 현재가:{curr_price:,.0f})"
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
    
    if curr_price > curr['ma40']:
        if disparity_40 <= 0.07:
            if curr['close'] >= curr['open'] or has_volume_surge:
                if slope_rate >= -0.01 and disparity_gold <= 0.005:
                    data_dict['grade'] = 'S+'
                    return True, "💎 [S+] 밥그릇 바닥 완전 수렴", "S+", data_dict
                if slope_rate >= -0.01:
                    data_dict['grade'] = 'A+'
                    return True, "🚀 [A+] 185선 평행/우상향 전환", "A+", data_dict
                data_dict['grade'] = 'A'
                return True, "🚀 A급 상승대기(골드안착)", "A", data_dict
            else:
                reason = f"거래량 부족(현재:{curr_vol:.0f} vs 기준평균:{base_avg_vol:.0f}, 최대비율:{max_vol_ratio:.3f} < 1.1)"
                return False, reason, "", data_dict

    if disparity_40 <= 0.025:
        if abs(diff_185) < 1.0:
            if slope_rate >= -0.01 and disparity_gold <= 0.015:
                data_dict['grade'] = 'S'
                return True, "⭐ [S급] 밥그릇 바닥 탈출(변곡점)", "S", data_dict
            data_dict['grade'] = 'S'
            return True, "S급 에너지응축(40선밀착)", "S", data_dict

    # 최종 탈락 사유 판단
    if curr_price <= curr['ma40']:
        reason = f"현재가({curr_price:,.0f}) ≤ 40일선({ma40_val:,.0f}, 이격도:{disparity_40_pct:.2f}%)"
        return False, reason, "", data_dict
    
    if disparity_40 > 0.07:
        reason = f"40일선 이격도 과다({disparity_40_pct:.2f}% > 7%, 현재가:{curr_price:,.0f}, 40일선:{ma40_val:,.0f})"
        return False, reason, "", data_dict
    
    reason = f"기타 조건 불만족(현재가:{curr_price:,.0f}, 40일선:{ma40_val:,.0f}, 이격도:{disparity_40_pct:.2f}%)"
    return False, reason, "", data_dict


# [사용자 원본] 정밀 2음봉 로직
def check_2_negative_candles(df):
    if len(df) < 15: return False, ""
    window = df.iloc[-15:-3]
    high_idx = window['vol'].idxmax()
    high_candle = window.loc[high_idx]
    if high_candle['close'] <= high_candle['open']: return False, ""
    high_volume = high_candle['vol']
    threshold_vol = high_volume * 0.10
    curr_p = df.iloc[-1]['close']
    is_high_price_zone = curr_p >= (high_candle['high'] * 0.97)
    post_candles = df.iloc[-3:]
    negative_count = 0
    for _, candle in post_candles.iterrows():
        if (candle['close'] < candle['open']) and (candle['vol'] >= threshold_vol):
            negative_count += 1
    if negative_count >= 2 and is_high_price_zone:
        return True, f"🚨 고점({high_candle['high']:,.0f}) 부근 10% 이상 실린 정밀 2음봉"
    return False, ""


# ---------------------------------------------------------
# [복구 및 추가] 매도 감시 메인 함수 (ERROR 방지 핵심)
# ---------------------------------------------------------
async def check_sell_signal(exchange, df, symbol, purchase_price, symbol_inventory_age=99, status=None):
    global emergency_mode
    df['ma40'] = df['close'].rolling(40).mean()
    df['ma90'] = df['close'].rolling(90).mean()
    df['ma185'] = df['close'].rolling(185).mean()

    curr = df.iloc[-1]
    curr_p = curr['close']

    # [보정] RSI 계산 시 NaN 방어 로직 추가
    rsi_val = calculate_rsi(df)
    curr_rsi = rsi_val.iloc[-1] if not rsi_val.empty else 50

    # [보정] 수익률 계산 시 purchase_price가 0일 때 -100% 뜨는 것 방지
    profit_rate = (curr_p - purchase_price) / purchase_price if purchase_price > 0 else 0
    profit_rate_pct = profit_rate * 100

    # [S급 털림 방지 로직] 급등 진행 중 판단 및 매도 유예
    ma40_val = curr['ma40']
    ma185_val = curr['ma185'] if not pd.isna(curr['ma185']) else 0
    
    if ma185_val > 0:
        is_price_above_ma40 = curr_p > ma40_val
        is_ma40_above_ma185 = ma40_val > ma185_val
        is_profit_above_10 = profit_rate_pct >= 10.0
        
        if is_price_above_ma40 and is_ma40_above_ma185 and is_profit_above_10:
            return False, "급등 진행 중(매도 유예)"

    # 0순위: 긴급 감시 (RSI 80 이상)
    if curr_rsi >= 80:
        if not emergency_mode.get(symbol, False):
            emergency_mode[symbol] = True

    # [수정 불가 원칙] 정밀 2음봉 매도 체크
    is_2_neg, reason_2_neg = check_2_negative_candles(df)
    if is_2_neg:
        return True, reason_2_neg

    # 상태 유지(KEEP) 중일 때 긴급 매도 외 일반 매도 차단
    if status == 'KEEP':
        return False, "유지 중"

    # 일반 매도 로직 (40선/90선 이탈)
    if curr_p < curr['ma90']:
        return True, "📉 90선 최종 이탈 매도"

    if curr_p < curr['ma40'] and profit_rate < 0:
        return True, "📉 40선 하단 손절"

    return False, "안전"


def get_report_visuals(this_profit, is_sell_signal, this_curr_p, ma40_val, sell_reason, symbol, pending_approvals):
    from datetime import datetime
    is_trend_up = this_curr_p >= ma40_val
    wait_data = pending_approvals.get(symbol)
    remains = 0
    if wait_data and 'start_time' in wait_data:
        elapsed = (datetime.now() - wait_data['start_time']).total_seconds() / 60
        limit = wait_data.get('wait_limit', 30)
        remains = max(0, int(limit - elapsed))

    if wait_data and wait_data.get('status') == 'WAITING':
        return "🟡", f"⏳ {remains}분 남음 ({wait_data.get('wait_limit')}m)"

    if wait_data and wait_data.get('status') == 'NOTIFIED':
        is_urgent = ("1순위" in sell_reason or "2음봉" in sell_reason or "급락" in sell_reason)
        icon = "🚨" if is_urgent else "🔵"
        status_msg = "긴급매도" if is_urgent else "일반매도"
        return icon, f"⏳ {remains}분 후 {status_msg} (신호:{sell_reason})"

    # [흰색 박멸 보정] ⚪이 나오지 않도록 조건문 순서 조정 및 강제 색상 부여
    if is_sell_signal:
        return "🔴", f"⚠️ 매도신호({sell_reason})"

    if not is_trend_up:
        return "🟡", "⚠️ 40선 하단(주의)"

    if is_trend_up:
        # 추세 위일 때 수익권이면 빨강, 손실권이면 초록 (사용자 원본 로직)
        return ("🔴", "✅ 추세Best") if this_profit > 0 else ("🟢", "✅ 차트양호")

    # 마지막 리턴에서 절대 ⚪이 안 나오도록 🟢로 마무리
    return "🟢", "차트양호"