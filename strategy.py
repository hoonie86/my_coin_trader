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
def check_buy_signal(df, symbol, warning_list, df_1m=None):
    """
    매수 신호 판단 함수 (4개 값 리턴)
    
    df_1m: optional. 1분봉 DataFrame (columns: time, open, high, low, close, vol).
           수급 돌파(1분봉 거래량 300% + 3분 내 3% 급등) 판별 시 사용. 없으면 30분봉 기준으로만 판별.
    
    Returns:
        tuple: (is_buy: bool, reason: str, grade: str, data_dict: dict)
    """
    # 기본 data_dict 초기화 (조건 탈락 여부와 관계없이 끝까지 계산해 빈칸 채움)
    data_dict = {}
    
    if len(df) < 185:
        return False, "데이터부족", "", data_dict

    # [기존 유지] 40/185일선 + RSI
    df['ma40'] = df['close'].rolling(40).mean()
    df['ma185'] = df['close'].rolling(185).mean()
    df['rsi'] = calculate_rsi(df)
    # [신규] 단기 정배열/골든크로스용 5일·20일 이평선 (30분봉 기준 5봉/20봉)
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    # [단기 정배열 전환] 40일×90일 골든크로스용
    df['ma90'] = df['close'].rolling(90).mean()

    curr = df.iloc[-1]
    prev = df.iloc[-2]
    curr_price = float(curr['close'])

    
    # [가격 필터] 10원 미만 또는 10,000원 이상 → BTC 마켓 동전주/비정상 차단
    if curr_price < 10 or curr_price >= 10000:
        return False, "가격필터(BTC마켓)", "", data_dict

    # [유의 종목] 수급 돌파(S/S+) 포함 모든 매수 신호에서 투자유의 종목 제외 (먼저 검사)
    if symbol.split('/')[0] in warning_list:
        return False, "투자유의", "F", data_dict

    ###### [신규 추가] 스테이블 코인 및 185일선 고점(상위 30%) 원천 차단 ######
    # 1. 스테이블 코인 필터링 (USDC, USDT, DAI 등 차트 왜곡 종목)
    exclude_symbols = ['USDC', 'USDT', 'DAI', 'BUSD']
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
        if pos_185 >= 0.7:
            return False, f"🚫 [제외] 185선 고점 구간({pos_185*100:.1f}%)", "B", data_dict

    # ---------- [개선] 수급 돌파: 1분봉 기준 (RSI 과열 및 고점 추격 방지 추가) ----------
    if df_1m is not None and len(df_1m) >= 21:
        # 유의종목이면 수급 로직 타기 전에 즉시 차단
        if symbol.split('/')[0] in warning_list:
            return False, "유의종목차단(S)", "", data_dict

        vol_avg_20 = df_1m['vol'].tail(20).mean()
        vol_cur = float(df_1m.iloc[-1]['vol'])
        price_3bars_ago_1m = float(df_1m.iloc[-4]['close']) if len(df_1m) >= 4 else 0
        surge_3pct_1m = (price_3bars_ago_1m > 0 and (curr_price - price_3bars_ago_1m) / price_3bars_ago_1m >= 0.03)
        
        # [핵심 필터 추가]
        rsi_1m = calculate_rsi(df_1m).iloc[-1] # 1분봉 RSI 계산
        day_low = df['low'].min() # 당일 저점
        up_from_low = (curr_price - day_low) / day_low if day_low > 0 else 0

        # 조건: 거래량 300% + 3분 내 3% + RSI 70미만 + 당일 저점대비 7%이내 상승
        if vol_avg_20 > 0 and vol_cur >= vol_avg_20 * 3 and surge_3pct_1m:
            if rsi_1m < 70 and up_from_low < 0.07:
                data_dict = _fill_data_dict_full(df, curr, prev, curr_price, symbol)
                data_dict['grade'] = 'S'
                data_dict['pattern_labels'] = _get_pattern_labels(
                    df, curr, curr_price, data_dict.get('rsi'), float(curr['ma5']) if not pd.isna(curr.get('ma5')) else None,
                    float(curr['ma20']) if not pd.isna(curr.get('ma20')) else None, float(curr['ma185']) if not pd.isna(curr.get('ma185')) else None)
                return True, f"💎 [S] 수급 돌파(RSI:{int(rsi_1m)}/상승:{up_from_low*100:.1f}%)", "S", data_dict
            else:
                # 조건은 맞지만 과열인 경우 로그만 남기고 패스하도록 설계 가능
                pass

    # ---------- [기존 유지 및 보강] 30분봉 기준 S+ 수급 ----------
# ---------- [기존 유지 및 보강] 30분봉 기준 S+ 수급 ----------
    if len(df) >= 5:
        # 유의종목 차단
        if symbol.split('/')[0] in warning_list:
            return False, "유의종목차단(S+)", "", data_dict

        # [추가] 골크 전조 10봉 포함, 최근 15봉 내 최고가 계산 (설거지 방지용 기준점)
        max_peak_price = df['high'].iloc[-15:].max()

        avg_vol_5 = df['vol'].tail(5).mean()
        volume_300 = (avg_vol_5 > 0 and float(curr['vol']) >= avg_vol_5 * 3)
        
        price_3bars_ago = float(df.iloc[-4]['close']) if len(df) >= 4 else 0
        price_surge_3pct = (price_3bars_ago > 0 and (curr_price - price_3bars_ago) / price_3bars_ago >= 0.02)
        
        # 30분봉 기준 과열 판단
        rsi_val = data_dict.get('rsi', 50) if data_dict else calculate_rsi(df).iloc[-1]

        if volume_300 and price_surge_3pct:
            # [수정] RSI 조건에 '고점 대비 5% 이탈 방지' 필터 결합
            if rsi_val < 70 and curr_price >= max_peak_price * 0.95:
                data_dict = _fill_data_dict_full(df, curr, prev, curr_price, symbol)
                data_dict['grade'] = 'S+'
                return True, f"🔥 [S+급] 수급 급등(안전권 진입) - 세력 매집 의심", "S+", data_dict
                
    # ---------- [공통] data_dict 전체 수치 채우기 (조건 탈락 여부와 관계없이) ----------
    ma40_val = float(curr['ma40']) if not pd.isna(curr['ma40']) else 0
    ma185_val = float(curr['ma185']) if not pd.isna(curr['ma185']) else 0
    rsi_val = float(curr['rsi']) if not pd.isna(curr['rsi']) else 50
    ma5_val = float(curr['ma5']) if not pd.isna(curr['ma5']) else None
    ma20_val = float(curr['ma20']) if not pd.isna(curr['ma20']) else None

    data_dict = _fill_data_dict_full(df, curr, prev, curr_price, symbol)

    # (투자유의 검사는 가격 필터 직후에 이미 수행됨. 수급 돌파 포함 모든 경로에서 유의 종목 제외)

    # 1. [기존 유지] 2일 전 대비 5시간 전 하락 여부 확인 (밥그릇 바닥 확인)
    ma185_p_2d = df['ma185'].iloc[-96] if len(df) >= 96 else df['ma185'].iloc[0]
    ma185_r_5h = df['ma185'].iloc[-10] if len(df) >= 10 else df['ma185'].iloc[0]
    is_was_descending = ma185_r_5h <= ma185_p_2d

    # 2. [기존 유지] 현재 기울기 수치
    diff_185 = (curr['ma185'] - prev['ma185']) / get_bithumb_tick_size(curr['ma185']) if get_bithumb_tick_size(curr['ma185']) else 0
    slope_rate = ((curr['ma185'] - prev['ma185']) / prev['ma185']) * 100 if prev['ma185'] and prev['ma185'] != 0 else 0
    data_dict['slope_rate'] = slope_rate
    # 185일선 대비 이격도(%): -5% 이하면 역추세 과매도 후보
    disparity_185_pct = (curr_price - ma185_val) / ma185_val * 100 if ma185_val and ma185_val != 0 else 0
    data_dict['disparity_185_pct'] = disparity_185_pct

    # ---------- [신규] 역추세 과매도: 185일선 하락 중이라도 RSI≤20 또는 185일선 이격도≤-10% 이고 현재가>40일선 이면 매수 후보 ----------
    is_185_falling = slope_rate < -0.06 and not is_was_descending
    if is_185_falling and (rsi_val <= 20 or disparity_185_pct <= -10.0) and curr_price > curr['ma40']:
        # 등급 A: 하락 중 과매도 구간
        data_dict['grade'] = 'A'
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
        reason = "✅ [A] 역추세 과매도(RSI≤20 또는 185이격≤-10%이고 현재가>40일선)"
        return True, reason, "A", data_dict

    # ---------- [신규] 단기 정배열 전환: 40일선 골든크로스 90일선 + 현재가>40일선 (과도한 5/20 조건 대체) ----------
    if len(df) >= 90:
        ma90_curr = curr.get('ma90')
        ma90_prev = df['ma90'].iloc[-2]
        if not (pd.isna(ma90_curr) or pd.isna(ma90_prev)) and ma40_val and ma90_curr:
            prev_40, prev_90 = df['ma40'].iloc[-2], ma90_prev
            if prev_40 <= prev_90 and ma40_val > float(ma90_curr) and curr_price > ma40_val:
                data_dict['grade'] = 'A'
                data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
                return True, "✅ [A] 단기 정배열 전환(40일×90일 골든크로스, 현재가>40일선)", "A", data_dict

    # [기존 유지] ZRO/STG처럼 고개 든 놈을 살려주는 OR 로직
    if not (slope_rate >= -0.06 or is_was_descending):
        reason = f"185일선 하락 조건 불만족(기울기:{slope_rate:.4f}%)"
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
        return False, reason, "", data_dict

    # 3. [기존 유지] 안전장치: 급격한 수직 낙하만 방어 (중복 블록 제거: 아래 한 번만 유지)
    if diff_185 < -1.2:
        reason = f"185일선 급락(diff:{diff_185:.2f} < -1.2)"
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
        return False, reason, "", data_dict

    ###### [수정안] 5일선 이격도 중심의 바닥 낚시 (TYPE3) ######
    # 1. 5일선과 185일선의 이격도 계산 (평균 -8.5% 이하 타겟)
    ma5_val = float(curr['ma5']) if not pd.isna(curr['ma5']) else curr_price
    disparity_5_185 = (ma5_val - ma185_val) / ma185_val * 100 if ma185_val > 0 else 0
    
    # 2. 새끼 양봉 계산 (시가 대비 +0.5% ~ +2.0% 사이만 허용)
    candle_body_pct = ((curr_price - curr['open']) / curr['open'] * 100) if curr['open'] > 0 else 0

    # [수정] 40선이 185선 아래여야 하며, 5일선이 확실히 바닥(-8.5%)에 처박혔을 때만 진입
    if ma40_val < ma185_val and rsi_val <= 40:
        # 가드 A: 5일선 이격도가 -8.5% 이하일 것 (고점 매수 원천 차단)
        # 가드 B: 너무 급등하지 않은 '새끼 양봉'일 것 (0.5% ~ 2.0%)
        if disparity_5_185 <= -8.5 and 0.5 <= candle_body_pct <= 2.0:
            if slope_rate > -0.05: # 185선이 투매 수준으로 꺾이지 않았을 때
                data_dict = _fill_data_dict_full(df, curr, prev, curr_price, symbol)
                return True, f"💎 [Type 3] 바닥낚시(이격:{disparity_5_185:.1f}% / 양봉:{candle_body_pct:.1f}%)", "A", data_dict

    gold_index = -1
    for i in range(1, 97):
        if df['ma40'].iloc[-i - 1] < df['ma185'].iloc[-i - 1] and \
                df['ma40'].iloc[-i] > df['ma185'].iloc[-i]:
            gold_index = len(df) - i
            break

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
        
        # 5% 이상 급등락이 있었으면 탈락
        if dynamic_rise >= 5.0:
             return False, f"🚫 [제외] 골크 전후 변동성 과다({dynamic_rise:.1f}% >= 5%)", "B", data_dict

    bars_since_gold = len(df) - gold_index if gold_index != -1 else -1
    data_dict['bars_since_gold'] = bars_since_gold
    
    if gold_index == -1:
        reason = "골든크로스 미발생"
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
        return False, reason, "", data_dict
    
    if bars_since_gold < 4:
        reason = f"골든크로스 후 {bars_since_gold}봉(4봉 미만, 필요:4봉 이상)"
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
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
    
    # [등급 산출 정리] S: 185우상향+RSI40~60 or 수급폭증 | A: 역추세과매도 or 5/20골든크로스 | B: 눌림목
    if curr_price > curr['ma40']:
        if disparity_40 <= 0.07:
            if curr['close'] >= curr['open'] or has_volume_surge:
                data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
                if slope_rate >= -0.01 and disparity_gold <= 0.005:
                    # 최근 50개 캔들의 최고점 대비 낙폭을 계산하여 가짜 바닥 필터링
                    recent_max = df['high'].rolling(window=50).max().iloc[-1]
                    drop_rate = ((recent_max - curr_price) / recent_max) * 100
                    data_dict['grade'] = 'S+'
                    return True, "💎 [S+] 밥그릇 바닥 완전 수렴", "S+", data_dict
                if slope_rate >= -0.01:
                    data_dict['grade'] = 'A+'
                    return True, "🚀 [A+] 185선 평행/우상향 전환", "A+", data_dict
                data_dict['grade'] = 'A'
                return True, "🚀 A급 상승대기(골드안착)", "A", data_dict
            else:
                reason = f"거래량 부족(현재:{curr_vol:.0f} vs 기준평균:{base_avg_vol:.0f}, 최대비율:{max_vol_ratio:.3f} < 1.1)"
                data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
                return False, reason, "", data_dict

    if disparity_40 <= 0.025:
        if abs(diff_185) < 1.0:
            data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
            # --- [신규 필터 추가] 폭락 중인 칼날 잡기 방지 ---
            # 1. 현재 캔들이 음봉이면서 시가 대비 2% 이상 하락 중인지 확인
            is_falling_now = (curr['close'] < curr['open']) and ((curr['open'] - curr['close']) / curr['open'] >= 0.02)
            # 2. 최근 3봉 중 음봉이 2개 이상인지 확인 (하락 관성)
            recent_3_candles = df.iloc[-3:]
            negative_candles = len(recent_3_candles[recent_3_candles['close'] < recent_3_candles['open']])
            
            if is_falling_now or negative_candles >= 2:
                # 폭락 중이면 S급 부여를 취소하고 하단으로 흘려보내거나 탈락시킴
                reason = "📉 [탈락] 40선 밀착했으나 하락 관성 강함 (폭락 주의)"
                return False, reason, "", data_dict
            # --- [신규 필터 끝] ---
            if slope_rate >= -0.01 and disparity_gold <= 0.015:
                data_dict['grade'] = 'S'
                return True, "⭐ [S급] 밥그릇 바닥 탈출(변곡점)", "S", data_dict
            data_dict['grade'] = 'S'
            return True, "S급 에너지응축(40선밀착)", "S", data_dict

    # [B등급] 급등 후 거래량이 줄어들며 20일선에서 지지받는 눌림목: 현재가가 ma20 근처이고 거래량 감소 시 B
    if ma20_val and base_avg_vol and curr_vol < base_avg_vol * 0.9 and abs(curr_price - ma20_val) / ma20_val <= 0.03:
        data_dict['grade'] = 'B'
        data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
        return True, "📌 [B] 눌림목(20일선 지지)", "B", data_dict

    # 최종 탈락 사유 판단 (모든 수치·패턴 라벨 기록 후 반환)
    data_dict['pattern_labels'] = _get_pattern_labels(df, curr, curr_price, rsi_val, ma5_val, ma20_val, ma185_val)
    # S+급 등이 확정되었으나 현재가가 40선 밑에 있어 하락세가 우려되는 경우 보완
    if curr_price <= curr['ma40'] and data_dict.get('grade') in ['S+', 'S', 'A+']:
         data_dict['grade'] = 'A' # 등급 하향
         # 기존 reason 뒤에 하락세 경고 문구 추가
    if 'grade' in data_dict and data_dict['grade'] in ['S', 'S+', 'A', 'A+', 'B']:
        print(f"🎯 [추천성공] {symbol} | 등급:{data_dict['grade']} | 구간Low:{window_low:,.0f} | 구간High:{window_high:,.0f} | 변동:{dynamic_rise:.2f}%")
    
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
async def check_sell_signal(exchange, df, symbol, purchase_price, symbol_inventory_age=99, status=None, realtime_p=None):
    global emergency_mode
    
    # [유지] 지표 계산
    df['ma40'] = df['close'].rolling(40).mean()
    df['ma90'] = df['close'].rolling(90).mean()
    df['ma185'] = df['close'].rolling(185).mean()

    curr = df.iloc[-1]
    prev = df.iloc[-2] # [추가] 급등 감지용
    curr_p = realtime_p if realtime_p is not None else curr['close']

    # [보정] RSI 및 수익률 계산
    rsi_series = calculate_rsi(df)
    has_rsi_spike = (rsi_series.tail(14) >= 80).any()
    
    profit_rate = (curr_p - purchase_price) / purchase_price if purchase_price > 0 else 0
    profit_rate_pct = profit_rate * 100
    
    ###### [출력] 유예 로직 통과 여부 확인 ######
    print(f"DEBUG: {symbol} | age: {symbol_inventory_age} | profit: {profit_rate_pct:.2f}%")
    ###### [신규 추가] 매수 후 6캔들 유예: 진입 초기 휩소 방지 (단, -3% 손절은 즉시 집행)
    if symbol_inventory_age < 6:
        if profit_rate_pct <= -3.0:
            return True, f"🚨 [긴급손절] 진입초기 -3% 도달", True
        return False, f"진입 초기 유예({symbol_inventory_age}봉)", False

    ma40_val = curr['ma40']
    ma185_val = curr['ma185'] if not pd.isna(curr['ma185']) else 0
    ####### [추가] TYPE3 예외 처리: 30분봉 지표(90선/지지선) 로직 진입 차단 #######
    if status == 'TYPE3':
        # -3% 손절선만 공통으로 체크하고, 나머지는 3분봉 로직으로 넘깁니다.
        if profit_rate_pct <= -3.0:
            return True, f"🚨 [TYPE3-긴급손절] -3% 도달 ({profit_rate_pct:.2f}%)", True

        ######## [추가 라인] TYPE3 전용 3분봉 날카로운 대응 ########
        try:
            ohlcv_3m = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '3m', limit=50)
            df_3m = pd.DataFrame(ohlcv_3m, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            is_3_2_neg_3m, r_3m = check_3_2_negative_candles(df_3m)
            if is_3_2_neg_3m:
                return True, f"🚨 [TYPE3-3m] 3중 2음봉 이탈: {r_3m}", True
        except Exception as e:
            logger.error(f"TYPE3 3분봉 분석 에러: {e}")
    ################################################################################
    ######### [신규: 30분봉 시가 기준 급등 즉시 처단 시스템] #########
    ################################################################################
    # 변수를 건드리지 않고, 30분봉 시가 대비 상승폭만 따로 계산합니다.
    soaring_rate = (curr_p - curr['open']) / curr['open'] * 100

    if soaring_rate >= 3.0:
        try:
            # 3분봉 데이터 호출 (정밀 분석용)
            ohlcv_3m = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '3m', limit=50)
            df_3m = pd.DataFrame(ohlcv_3m, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            
            # 최근 10개 3분봉 고점 추적
            high_10_3m = df_3m['high'].tail(10).max()
            curr_3m_p = df_3m['close'].iloc[-1]
            curr_3m_data = df_3m.iloc[-1] # [추가] 현재 봉 상세 데이터

            # 최근 10개 봉 중 거래량이 가장 컸던 고점 봉을 찾음
            abs_peak_idx = len(df_3m) - 10 + df_3m['vol'].tail(10).argmax()
            peak_candle = df_3m.iloc[abs_peak_idx]

            # 고점(peak)이 음봉이 아니고, 그 직전 봉(index-1)이 존재할 때만 가동
            if peak_candle['close'] >= peak_candle['open'] and abs_peak_idx > 0:
                prev_peak_candle = df_3m.iloc[abs_peak_idx - 1]
                # 고점의 '직전 봉' 몸통 50% 선 계산
                prev_mid_point = (prev_peak_candle['open'] + prev_peak_candle['close']) / 2
                
                # [실시간 긴급 조건]
                # 1. 현재 저가(low)가 직전봉 허리를 깼는가?
                # 2. 현재 거래량이 고점 거래량을 이미 넘어섰는가?
                # 3. 현재 캔들이 음봉 상태(시가 > 현재가)인가?
                if curr_3m_data['low'] < prev_mid_point and curr_3m_data['vol'] > peak_candle['vol']:
                    if curr_3m_data['close'] < curr_3m_data['open']: # 실시간 음봉 상태면 즉시 리턴
                        return True, f"🚨[50%긴급] 실시간 음봉 전환 및 고점직전봉 허리({prev_mid_point:,.0f}) 이탈", True

            # (A) 세력 이탈 감지: 2음봉 + 고점 거래량 10% 이상 (3분봉 기준)
            is_2_neg_3m, reason_2_neg_3m = check_3_2_negative_candles(df_3m)
            if is_2_neg_3m:
                high_idx_3m = df_3m['high'].tail(10).argmax()
                high_vol_3m = df_3m['vol'].iloc[len(df_3m) - 10 + high_idx_3m]
                curr_vol_3m = df_3m['vol'].iloc[-1] + df_3m['vol'].iloc[-2]
                if curr_vol_3m > (high_vol_3m * 0.1):
                    return True, f"🚨[급등-세력이탈] 2음봉 & 거래량폭발", True

            # (B) 캔들 위치별 차등 낙폭
            if soaring_rate >= 10.0:
                if curr_3m_p < high_10_3m * 0.97:
                    return True, f"🚨[급등-강력] 고점대비 3% 하락 (위치:{soaring_rate:.1f}%)", True
            else:
                if curr_3m_p < high_10_3m * 0.98:
                    return True, f"🚨[급등-초기] 고점대비 2% 하락 (위치:{soaring_rate:.1f}%)", True

            # (C) 수익률 마지노선 사수 (14% -> 13% 사수)
            if profit_rate_pct >= 13.0:
                if curr_3m_p < purchase_price * 1.13:
                    return True, "🚨[마지노선] 수익률 13% 사수 매도", True

        except Exception as e:
            logger.error(f"급등 정밀 분석 에러: {e}")
    ################################################################################
    # 0순위: 긴급 감시 스위치 작동
    if has_rsi_spike:
        if not emergency_mode.get(symbol, False):
            emergency_mode[symbol] = True
            logger.info(f"🔥 [비상] {symbol} 최근 14봉 내 RSI 80 돌파 이력 포착! 비상 모드 진입")

    # ---------------------------------------------------------
    ######### [신규: 급등 시 3분봉 비상 체제 전환] #########
    # ---------------------------------------------------------
    # 수익 10% 이상이거나 RSI 80 이상이면 3분봉 정밀 감시 가동
    if profit_rate_pct >= 10.0 or emergency_mode.get(symbol, False):
        try:
            ###### [ADD START] 3분봉 모드 가동 콘솔 출력 ######
            mode_reason = "수익률 10%↑" if profit_rate_pct >= 10.0 else "RSI 과열"
            print(f"🔥 [3분봉 엔진 가동] {symbol} | 사유: {mode_reason} | 현재가: {curr_p:,.0f}")

            # 3분봉 데이터 호출 (노이즈 방지를 위해 여기서 직접 fetch)
            ohlcv_3m = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '3m', limit=50)
            df_3m = pd.DataFrame(ohlcv_3m, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            df_3m['ma40'] = df_3m['close'].rolling(40).mean()
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

        # ######## [신규 추가] 평시 상황: 30분봉 기준 3중 2음봉 체크 ########
        if soaring_rate >= 3.0:
            is_3_2_neg_30m, reason_30m = check_3_2_negative_candles(df)
            if is_3_2_neg_30m:
                return True, f"📉 [급등후-30m] 30분봉 3중 2음봉 포착: {reason_30m}", False
    # ---------------------------------------------------------
    # [정비 1 & 3] 급등 제어 및 2음봉 감시 (3분/5분 내 5% 폭등 시에만)
    # ---------------------------------------------------------
    # 30분봉 데이터이므로 봉 하나가 5% 이상 솟구치면 급등으로 판정
    is_surging = (curr_p - prev['open']) / prev['open'] >= 0.05
    
    if is_surging:
        is_2_neg, reason_2_neg = check_3_2_negative_candles(df)
        if is_2_neg:
            return True, f"🚀 단기 급등 후 세력 이탈: {reason_2_neg}", True

    # ---------------------------------------------------------
    # [정비 2] 40 지지선 및 S+급 보호 (상향->평행->상향 로직)
    # ---------------------------------------------------------
    # 최근 20봉 중 ma40의 기울기가 가장 완만했던 구간의 가격을 지지선으로 설정
    parallel_window = df.iloc[-20:]
    support_idx = (parallel_window['ma40'].diff().abs()).idxmin()
    support_price = df.loc[support_idx, 'ma40']

    ###### [ADD START] 90선 독립 마지노선 (40선 지지선 부재 시) ######
    # 40선 지지선이 현재가보다 5% 이상 멀리 있다면 신뢰할 수 없으므로 90선을 즉시 체크
    disparity_40 = (curr_p - support_price) / support_price * 100 if support_price > 0 else -99
    if (support_price == 0 or disparity_40 < -5.0) and curr_p < curr['ma90']:
        return True, f"📉 [90선이탈] 지지선 부재 또는 이격 과다로 90선 최종 이탈", False

    # S+ 상승 초입(-2% ~ +5%) 보호
    is_early_stage = -2.0 < profit_rate_pct < 5.0
    
    # 40선 지지선 매도 판정
    if curr_p < support_price:
        # 상승 초입 눌림목(지지선의 98%)은 유예해줌
        if not (is_early_stage and curr_p >= support_price * 0.98):
            return True, f"📉 40선 지지선({support_price:,.0f}) 이탈", False

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

    # [변수 체크 1] 고점 추적 (최근 20봉)
    try:
        recent_df = df.iloc[-20:]
        high_price = recent_df['high'].max()
    except Exception:
        high_price = curr_p  # 데이터 부족 시 현재가를 고점으로 간주

    # [매도 1순위] 고점 대비 3% 하락 (Trailing Stop)
    # 20봉 이내 최고가 대비 3% 밀리면 수익 구간 상관없이 즉시 매도
    if high_price > 0:
        drop_from_peak = (high_price - curr_p) / high_price * 100
        if drop_from_peak >= 3.0:
            return True, f"📉 [고점하락] 최근 20봉 고점({high_price:,.0f}) 대비 3% 하락 매도", False

    # [매도 2순위] 지지선 이탈 (사용자님 제안: support_price 기준)
    # 현재가가 계산된 지지선(기울기 완만했던 ma40)을 하향 돌파할 때
    if curr_p < support_price:
        # 지지선이 현재가 대비 너무 아래(-5% 이하)에 있다면 신뢰도가 낮음
        disparity_support = (curr_p - support_price) / support_price * 100 if support_price > 0 else 0
        
        if disparity_support < -5.0:
            # 지지선이 너무 멀면 90일선을 최종 마지노선으로 확인 (중복 제거 통합)
            if curr_p < curr['ma90']:
                return True, "📉 [최종이탈] 지지선 이격 과다로 90선 최종 이탈 매도", False
        else:
            # 일반적인 지지선 하향 이탈
            return True, f"📉 [지지선이탈] 설정 지지선({support_price:,.0f}) 하향 이탈", False

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
    if this_curr_p < ma40_val:
        return "🟡", f"⚠️ 40선 하단(주의)"

    # [4] 차트 양호 (초록 - 홀딩/안전 신호)
    # 매도 신호가 없고 40선 위라면 수익률과 관계없이 초록색으로 표시
    return "🟢", f"✅ 차트양호(홀딩)"
